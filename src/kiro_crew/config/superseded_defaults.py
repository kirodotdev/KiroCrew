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

Why the report is acknowledgeable
---------------------------------
Value equality alone cannot falsify a report, so an operator who deliberately
chose a value that happens to equal a superseded default was told about it on
every load, forever, with no way to answer (issue #7559). Worse, the registry is
append-only: each new entry adds another permanent line, and a section that is
mostly unanswerable noise is a section operators learn to skip -- which costs
exactly the genuine drift the mechanism exists to surface.

So the report is now falsifiable. :func:`acked_superseded` reads an
acknowledgment file recording ``<dotted key> -> the value that was acked``, and a
key whose STORED value still equals its acked value is not reported. The value is
recorded rather than a bare key name so the acknowledgment covers the choice, not
the key: change the value later and the report returns.

The acknowledgment lives in its own file, NOT in ``config.json``, because a full
``to_dict()`` rewrite carries only schema fields -- the same materialization
behaviour that created this whole problem would silently drop an ack stored
anywhere in the config document. Losing the file is harmless by construction: it
changes no runtime behaviour, only whether one line is printed.

Scope discipline
-----------------
Nothing here writes to CONFIGURATION. Detection is pure; :func:`drop_drifted_keys`
mutates a dict handed to it by a caller that already owns the config lock;
:func:`record_acks` opens ``config.json`` only to READ it, under that same lock,
because acknowledging a value it did not just read would record a stale one. The
one file this module writes is the acknowledgment file, which holds no settings.

Rendering splits by who asked. ``doctor``'s section lives HERE, reading
``config.json`` directly, so the config package stays the one place that knows what
the stored document means. ``kirocrew config defaults`` lives in ``cli_config``,
because deciding what to DO about drift -- adopt, affirm, or just look -- is the
CLI's job, and it calls into the helpers above rather than reimplementing them.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

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
    new_default_display: str | None = None


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
    # 0.5.0 changed stt.streaming from False to True: every provider now produces
    # partial results, so the reason the default was off (two of the six providers
    # could stream) no longer exists. An install materialized before that keeps
    # resolving False and sees text only after it stops speaking, which reads as
    # the feature being missing rather than switched off.
    SupersededDefault(
        dotted_key="stt.streaming",
        old_default=False,
        new_default=True,
        changed_in="0.5.0",
    ),
    # 0.5.0 changed stt.model from turbo to base. The stored name is still honoured
    # (it resolves onto large-v3-turbo), so this is not a broken value -- it is a
    # 1.6 GB first-use download where the current default is 148 MB, on an install
    # that materialized the name back when the model was fetched by a separate
    # whisper CLI the user had already installed themselves.
    #
    # stt.provider is deliberately NOT registered even though its default moved to
    # ``local``: a stored retired provider is coerced at parse time, so the stored
    # value does not win and there is no drift to report.
    SupersededDefault(
        dotted_key="stt.model",
        old_default="turbo",
        new_default="base",
        changed_in="0.5.0",
    ),
    # #6651 changed the watchdog budget from a materialized 25 seconds to a
    # nullable, launch-class default: 25 seconds for desktop/foreground and 90
    # seconds for managed services. A stored 25 may be either the old default or
    # a deliberate operator pin, so report it instead of rewriting it.
    SupersededDefault(
        dotted_key="dashboard.loop_stall_exit_after_secs",
        old_default=25,
        new_default=None,
        changed_in="#6651",
        new_default_display="unset (automatic: 25s desktop / 90s managed service)",
    ),
    # instances.warm_set_cap moved from a materialized 5 to 0 (automatic: as many
    # panes as there are crews registered). A stored 5 keeps evicting the 6th crew,
    # and eviction is indistinguishable from a disconnect at the pane -- so the
    # symptom of holding the old default is a connection that looks like it flaps
    # on tab switch. A stored 5 may equally be a deliberate budget on a
    # memory-tight machine, so report it rather than rewriting it.
    SupersededDefault(
        dotted_key="instances.warm_set_cap",
        old_default=5,
        new_default=0,
        changed_in="#7248",
        new_default_display="0 (automatic: as many as are registered)",
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


#: Name of the acknowledgment file inside the data home. Its own file rather than
#: a block in ``config.json``: a ``to_dict()`` rewrite carries only schema fields,
#: so an ack stored in the config document would be dropped by the very
#: materialization behaviour this module exists to report on.
ACK_FILE_NAME = "superseded_acked.json"


class AckPathRefused(OSError):
    """The acknowledgment path is not a plain file we may read or replace.

    An ``OSError`` subclass on purpose: every caller already has to handle a failed
    write on a read-only or full data home, and this is the same class of refusal.
    """


#: Ceiling on the acknowledgment file. The map holds one small entry per registry
#: row, so anything larger is not this file; capping the read keeps a single
#: ``os.read`` bounded, which is what lets it run on the config-load path.
ACK_MAX_BYTES = 64 * 1024


def ack_file_path() -> Path:
    """Return the acknowledgment file's path inside the data home.

    ``config_dir`` is imported lazily for the same reason ``config_path`` is: the
    loader imports this module, so a module-level import would be a cycle.
    """
    from kiro_crew.config.loader import config_dir  # circular import

    return config_dir() / ACK_FILE_NAME


def _read_ack_document() -> object:
    """Read and parse the acknowledgment file, or return ``None``.

    **This runs on the config-load path, which is an event-loop path**, so the read
    must not be able to block indefinitely. The file lives at a path the agent can
    name, and ``open()`` on a FIFO waits for a writer forever -- that would wedge
    the gateway, not merely delay it. Three things keep the read bounded:

    * ``lstat`` refuses anything that is not a REGULAR file, links included;
    * the open carries ``O_NONBLOCK`` and ``O_NOFOLLOW`` where the platform has
      them, so a leaf swapped after the ``lstat`` fails or returns immediately
      instead of waiting;
    * ``fstat`` re-checks the OPENED object and the size, then a single capped
      ``os.read`` finishes it.

    Returns ``None`` for every refusal and every read error. An ack suppresses one
    report line and changes nothing about how config resolves, so the worst
    consequence of ignoring an unreadable file is that the operator is told again.
    """
    path = ack_file_path()
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > ACK_MAX_BYTES:
        logger.debug("Ignoring superseded-default acknowledgments: not a plain small file")
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        logger.debug("Ignoring unreadable superseded-default acknowledgments: %s", e)
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > ACK_MAX_BYTES:
            return None
        raw = os.read(fd, ACK_MAX_BYTES)
    except OSError as e:
        logger.debug("Ignoring unreadable superseded-default acknowledgments: %s", e)
        return None
    finally:
        os.close(fd)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        # Covers both failure modes without restating them: UnicodeDecodeError and
        # json.JSONDecodeError are both ValueError subclasses.
        logger.debug("Ignoring malformed superseded-default acknowledgments: %s", e)
        return None


def _acked_from_document(raw: object) -> dict[str, object]:
    """Extract the ack map from a parsed document, tolerating any other shape."""
    if not isinstance(raw, dict):
        return {}
    acked = raw.get("acked")
    if not isinstance(acked, dict):
        return {}
    return {k: v for k, v in acked.items() if isinstance(k, str)}


def acked_superseded() -> dict[str, object]:
    """Return ``{dotted key: acked value}`` from the acknowledgment file.

    Fails SOFT on every problem -- see :func:`_read_ack_document`. That soft read is
    also why the file carries no schema version: any shape it cannot understand is
    already handled, so a version field would have no reader.
    """
    return _acked_from_document(_read_ack_document())


def _update_acked(mutate: Callable[[dict[str, object]], dict[str, object]]) -> dict[str, object]:
    """Read-modify-write the acknowledgment file under its own lock.

    The whole transaction runs inside one lock hold, so two concurrent ``--keep``
    calls cannot both read the same map and have the second replacement drop the
    first operator's acknowledgment.

    **The write never RESOLVES the leaf.** ``write_config_atomically`` deliberately
    follows a link, because symlinking ``config.json`` into a dotfiles repo is a
    supported setup; here that would let a link planted at this path redirect the
    write onto an arbitrary file. ``atomic_write`` renames a fresh temp file OVER
    the leaf instead, so even a link swapped in after the check below is replaced
    rather than followed -- the check reports the condition, the rename is what
    makes it unexploitable.

    Raises ``OSError`` (including :class:`AckPathRefused`) on any filesystem
    refusal; callers turn that into a controlled CLI error rather than a traceback.
    """
    path = ack_file_path()
    if platform_compat.is_link_or_junction(path):
        raise AckPathRefused(f"refusing to write through a link at {ACK_FILE_NAME}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / (path.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with platform_compat.file_lock(fd, exclusive=True):
            current = _acked_from_document(_read_ack_document())
            updated = dict(mutate(current))
            atomic_write(path, json.dumps({"acked": updated}, indent=2) + "\n", mode=0o600)
            return updated
    finally:
        os.close(fd)


def write_acked_superseded(acked: dict[str, object]) -> None:
    """Replace the acknowledgment file's map with *acked*, under its lock."""
    _update_acked(lambda _existing: acked)


def record_acks(dotted_keys: list[str]) -> list[str]:
    """Acknowledge the CURRENTLY stored values for *dotted_keys*; return the keys.

    The config document is re-read and re-checked for drift **under its own lock**,
    not taken from the caller's earlier snapshot: a value changed between the listing
    and this call would otherwise be acknowledged at its superseded snapshot, which
    then silently suppresses the report for a value the operator never affirmed. A
    key that is no longer drifted is skipped rather than acked.

    The ack write happens inside that same config lock hold, so the recorded value
    cannot be stale by the time it lands. Lock order is config-then-ack at the only
    site that nests them.
    """
    from kiro_crew.config.loader import config_path, update_config_locked  # circular import

    recorded: list[str] = []

    def _under_config_lock(config_doc: dict) -> None:
        still_drifted = {e.dotted_key for e in superseded_default_drift(config_doc, acked={})}
        values: dict[str, object] = {}
        for dotted in dotted_keys:
            if dotted not in still_drifted:
                continue
            stored = _stored_value(config_doc, dotted)
            if stored is not _ABSENT:
                values[dotted] = stored
        if values:
            _update_acked(lambda existing: {**existing, **values})
            recorded.extend(values)
        return None  # read-only: mutate returning None writes no config

    update_config_locked(config_path(), mutate=_under_config_lock, stamp_meta=False)
    return recorded


def drop_acks(dotted_keys: list[str]) -> None:
    """Forget the acknowledgments for *dotted_keys*, under the file's lock.

    Called after an adopt: the acked value is no longer stored, so keeping the entry
    would silence a genuinely deliberate choice made later. A no-op when none of the
    keys is acked, so an adopt on a never-acked key does not touch the file at all.
    """
    if not any(k in acked_superseded() for k in dotted_keys):
        return
    drop = set(dotted_keys)
    _update_acked(lambda existing: {k: v for k, v in existing.items() if k not in drop})


_ABSENT = object()


def _stored_value(base_data: dict, dotted_key: str) -> object:
    """Return what *base_data* stores at *dotted_key*, or ``_ABSENT``."""
    section, field = _split_dotted(dotted_key)
    section_data = base_data.get(section)
    if not isinstance(section_data, dict) or field not in section_data:
        return _ABSENT
    return section_data[field]


def _is_acked(entry: SupersededDefault, stored: object, acked: dict[str, object]) -> bool:
    """True when *stored* is the exact value the operator acknowledged for *entry*.

    Type is compared as well as value, for the same reason detection does: ``bool``
    is an ``int`` subclass, so an acked ``0`` must not silence a stored ``False``.
    """
    if entry.dotted_key not in acked:
        return False
    ack = acked[entry.dotted_key]
    return type(stored) is type(ack) and stored == ack


def superseded_default_drift(
    base_data: dict, acked: dict[str, object] | None = None
) -> list[SupersededDefault]:
    """Return the registered entries whose STORED value is the superseded default.

    *base_data* must be the stored base document (``config.json`` alone), never the
    view produced by merging ``config.local.json`` over it. The overlay is a
    separate user-owned file applied at read time; a value it supplies is the
    operator's live choice and says nothing about what the base has materialized,
    so reporting on the merged view would both miss real drift in the base and
    describe a value the base does not hold.

    *acked* is the acknowledgment map; passing ``None`` reads the acknowledgment
    file. Pass ``{}`` to get the unacknowledged truth -- what an operator asking
    "what am I still holding?" wants to see even for values they already affirmed.

    An entry is reported only when the stored value equals ``old_default`` with the
    same type -- ``bool`` is an ``int`` subclass, so requiring the type as well
    keeps a stored ``0`` from being read as ``False``. An absent section, an absent
    key, or any other value is not drift: those already resolve to the current
    dataclass default at parse time, which is the desired outcome.

    Reads *base_data* (and, unless *acked* is supplied, the acknowledgment file)
    and returns a list. Neither is mutated; no config is written.
    """
    if acked is None:
        acked = acked_superseded()
    drifted: list[SupersededDefault] = []
    for entry in SUPERSEDED_DEFAULTS:
        section, field = _split_dotted(entry.dotted_key)
        section_data = base_data.get(section)
        if not isinstance(section_data, dict) or field not in section_data:
            continue
        stored = section_data[field]
        if type(stored) is not type(entry.old_default) or stored != entry.old_default:
            continue
        if _is_acked(entry, stored, acked):
            continue
        drifted.append(entry)
    return drifted


@dataclass(frozen=True)
class CoercedValue:
    """One stored value the loader must REPLACE at parse time, not merely override.

    Distinct from :class:`SupersededDefault`, and the difference decides what an
    operator can do about it. A superseded default is a value that still works and
    still wins, so it may be a deliberate choice and must not be rewritten. A
    coerced value cannot win: the loader replaces it because it names something that
    no longer exists, so there is no choice to preserve -- which makes removing it
    unambiguously safe, and makes affirming it meaningless.

    Left in place it is inert bytes that buy nothing and cost a warning on every
    single load, forever, because a load never writes.

    ``is_coerced`` rides on the ENTRY rather than living in the detector's loop, so
    appending a retirement is genuinely sufficient. A detector that switched on
    ``dotted_key`` instead would leave an appended entry silently unreported -- no
    test goes red, the operator just keeps getting a warning nothing can clear.
    """

    dotted_key: str
    resolves_to: str
    reason: str
    is_coerced: Callable[[object], bool]


def _stt_provider_is_coerced(value: object) -> bool:
    """Ask the section that owns ``stt.provider`` whether *value* is inert.

    The judgment of what is selectable belongs there, so the surface offering to
    remove a value cannot come to disagree with the loader about which providers are
    dispatchable. Imported lazily: the loader pulls this module into its own import
    chain, so a module-level ``sections`` import would be a cycle.
    """
    from kiro_crew.config.sections import stt_provider_is_coerced  # circular import

    return stt_provider_is_coerced(value)


#: Stored values the loader coerces. One entry today; append as retirements land.
COERCED_VALUES: tuple[CoercedValue, ...] = (
    CoercedValue(
        dotted_key="stt.provider",
        resolves_to="local",
        reason=(
            "names a retired or unknown speech provider, so voice input already runs " "on 'local'"
        ),
        is_coerced=_stt_provider_is_coerced,
    ),
)


def coerced_value_drift(base_data: dict) -> list[tuple[CoercedValue, object]]:
    """Return ``(entry, stored value)`` for each registered key the loader coerces."""
    found: list[tuple[CoercedValue, object]] = []
    for entry in COERCED_VALUES:
        section, field = _split_dotted(entry.dotted_key)
        section_data = base_data.get(section)
        if not isinstance(section_data, dict) or field not in section_data:
            continue
        stored = section_data[field]
        if entry.is_coerced(stored):
            found.append((entry, stored))
    return found


def coercion_summary(entry: CoercedValue, stored: object) -> str:
    """One line describing a coerced stored value and the only useful answer to it."""
    return (
        f"{entry.dotted_key} is stored as {stored!r}, which {entry.reason}. "
        f"The stored value cannot take effect, so removing it changes nothing except "
        f"that Kiro Crew stops saying so on every load."
    )


def drop_drifted_keys(base_data: dict, dotted_keys: list[str]) -> list[str]:
    """Remove *dotted_keys* from *base_data* in place; return the ones removed.

    Removal is how a stored value is un-materialized: the loader resolves an absent
    key as ``data.get(key, DEFAULT)``, so the current default applies from the next
    load and the next full rewrite materializes it. Mutates the dict the CALLER
    read under its own lock; this function opens nothing.

    An emptied section is left in place -- an empty object resolves identically to
    an absent one, and removing it would widen the diff for no gain.
    """
    removed: list[str] = []
    for dotted in dotted_keys:
        section, field = _split_dotted(dotted)
        section_data = base_data.get(section)
        if isinstance(section_data, dict) and field in section_data:
            del section_data[field]
            removed.append(dotted)
    return removed


def drift_summary(entry: SupersededDefault) -> str:
    """One line describing *entry*'s drift, shared by the log and ``doctor``.

    Kept in one place so the two surfaces cannot drift into describing the same
    condition differently, and worded as a statement of fact plus the operator's
    options -- this mechanism does not know whether the stored value was chosen
    deliberately, and must not imply the value is wrong.
    """
    new_default = entry.new_default_display or repr(entry.new_default)
    adoption = (
        "removing the key or setting it to JSON null"
        if entry.new_default is None
        else f"removing the key or setting it to {entry.new_default!r}"
    )
    return (
        f"{entry.dotted_key} is stored as {entry.old_default!r}, which was the default "
        f"before {entry.changed_in} changed it to {new_default}. An install that "
        f"predates that change keeps the old value because a stored value beats the "
        f"default. If {entry.old_default!r} was not a deliberate choice, {adoption} "
        f"adopts the current default."
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

    An acknowledged entry is shown here rather than suppressed. An ack answers the
    UNSOLICITED load-path line; ``doctor`` is the question "what does this install
    still hold?", and hiding an affirmed value would make the answer wrong.

    ``config_path`` is imported lazily because ``config.loader`` imports this
    module for the load-path warning, so a module-level import would be a cycle.
    """
    from kiro_crew.config.loader import config_path  # circular import

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

    # Acked entries are LISTED, not hidden: an operator reading doctor is asking
    # what the install still holds, and an affirmed value is part of that answer.
    # Only the load-path line is silenced by an ack, because that one is unsolicited.
    acked = acked_superseded()
    drifted = superseded_default_drift(raw, acked={})
    coerced = coerced_value_drift(raw)
    if not drifted and not coerced:
        print("  drift:       ✅ no stored value holds a superseded default")
        return
    unacked = 0
    for entry in drifted:
        if entry.dotted_key in acked:
            print(f"  drift:       ✅ acknowledged as intentional: {drift_summary(entry)}")
        else:
            unacked += 1
            print(f"  drift:       ℹ️  {drift_summary(entry)}")
    for centry, stored in coerced:
        unacked += 1
        print(f"  coerced:     ℹ️  {coercion_summary(centry, stored)}")
    if unacked:
        print("  fix:         kirocrew config defaults --adopt      (take the current defaults)")
        print("  keep:        kirocrew config defaults --keep       (affirm yours, stop reporting)")
