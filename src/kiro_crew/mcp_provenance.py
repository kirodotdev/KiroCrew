"""Authorship marker for MCP entries Kiro Crew writes into shared config files.

Kiro Crew writes MCP server entries into two files it does not own -- the
kiro-global ``~/.kiro/settings/mcp.json`` and the Claude Code sidecar
``~/.mcp.json`` -- and users hand-edit both. Every write needs an answer to one
question: did we write this entry?

Name presence in the dashboard store cannot answer it. A minimal ``{"url": ...}``
entry is byte-identical whether this emitter produced it or a user typed it, so
"our managed server moved url" and "a different server the user named the same"
reach the write as the same input. This module records authorship instead of
inferring it: an entry is ours iff it carries :data:`MARKER_KEY`.

The marker is a declaration of who may write, NOT a security boundary -- it
defends a shared file against our own writer, not against the file's owner. A
user can strip it (reclaiming the entry, so we stop rewriting) or add it
(volunteering the entry for management). Both directions are fail-safe; see
``docs/architecture/design-notes/mcp-entry-provenance.md``.

Reclamation is why a present unmarked entry is NEVER written, not even when its
bytes already match what this sync would emit. Those bytes are exactly what
stripping the marker off one of our own entries leaves behind, so "written
before the marker existed" and "deliberately reclaimed" are the same disk state
and no content test separates them. Stamping on the match would migrate the
first and silently undo the second, so the trade is made the other way:
reclamation is durable, and an entry written before the marker existed stays
unmanaged until it is authored again. Re-establishing management is a Disconnect
then Connect -- the delete removes the name from the shared file, and the next
sync resolves ABSENT to a stamped create.

The marker only ever appears on entries for names the store manages, and only in
files we do not own: the store itself is ours by definition, and the emitted
agent spec is rendered rather than owned, so both stay unmarked.
"""

from __future__ import annotations

import logging
from typing import Any, Final

logger = logging.getLogger(__name__)

# Passed as ``on_disk`` when the shared file holds NO entry under the name.
#
# Absence needs its own signal because ``None`` is a value a user can type: a
# hand-edited file can carry ``"notion": null``, and ``mapping.get(name)`` answers
# ``None`` for both that and a missing key. Collapsing them would make the one
# shape that occupies a name while carrying no marker look like a free slot, and
# the create branch would write over it. Every caller therefore reads the mapping
# with ``get(name, ABSENT)``.
ABSENT: Final = object()

# One reserved key. The ``x-`` extension namespace cannot collide with a kiro-cli
# field: its config structs derive ``rename_all = "camelCase"``, which can never
# produce a hyphen. Unknown keys are tolerated -- ``McpServerConfig`` is an
# untagged enum whose variants do not set ``deny_unknown_fields``, and the one
# JSON-schema validation runs against the re-serialized struct, after
# deserialization has already dropped anything unknown.
MARKER_KEY = "x-kirocrew"

# Inside the marker object, so the record can gain fields later without burning a
# second reserved key.
_MANAGED_FIELD = "managed"

# A SECOND reserved key, deliberately not a field inside :data:`MARKER_KEY`.
#
# The two records answer different questions about different files. The marker
# says "we authored this ENTRY in a file we do not own", and its invariant is
# that it appears only in such files -- the rendered agent spec stays unmarked,
# so a reader of a shared file cannot mistake output for authorship. This record
# says "we COMPUTED these FIELDS of this rendered entry, and here is what they
# were derived from", and it belongs only in the rendered agent spec, which is
# the one file that is simultaneously our output and an input to our next
# rebuild. Folding it into the marker object would put the marker into the very
# file whose exclusion is the marker's invariant, and would expose it to
# :func:`without_marker`, which the shared-entry copy path applies.
#
# The ``x-`` namespace argument for :data:`MARKER_KEY` covers this key too: a
# kiro-cli field can never contain a hyphen, and unknown keys are dropped at
# deserialization rather than rejected.
DERIVED_KEY = "x-kirocrew-derived"

# Inside the record: what the field was derived FROM, and what we EMITTED. The
# second is the ownership proof -- see :func:`source_view`.
_FROM_FIELD = "from"
_EMITTED_FIELD = "emitted"

# The record describes the ONE field the rebuild computes from a source value and
# therefore must not re-consume as if the user had written it: the resolved absolute
# ``command``. It is stored FLAT -- the two keys above sit directly under
# :data:`DERIVED_KEY` -- rather than nested under a field name. There is one field,
# and the reader already fails open, so a later second field can widen the shape
# without a migration; nesting now would be structure for a variant that does not
# exist.
#
# The rebuild's other computed field is the expanded ``env.PATH``, and for the
# servers this record serves -- ones no other source declares -- it needs no record:
# the declaration IS the stored value, so a user narrowing it edits the field
# directly, the ownership guard reads that as theirs, and the next emit expands what
# they wrote. ``env.PATH`` becomes irrevocable only when the declaration lives in a
# DIFFERENT file from the expansion, which is the scope-owned case this record
# deliberately excludes.
_COMMAND: Final = "command"


def _command_record(entry: object) -> dict[str, str] | None:
    """The readable command record on ``entry``, or None.

    Fails open in the direction of TODAY's behavior: anything other than the exact
    shape reads as "no record", so the caller keeps consuming the stored value
    exactly as it did before this key existed. That is what makes a config written
    by an older build, or one whose record a user mangled, degrade to the previous
    behavior rather than to a dropped server.
    """
    if not isinstance(entry, dict):
        return None
    record = entry.get(DERIVED_KEY)
    if not isinstance(record, dict):
        return None
    src = record.get(_FROM_FIELD)
    emitted = record.get(_EMITTED_FIELD)
    # BOTH must be non-empty strings. ``emitted`` is the ownership proof, so a
    # record without it cannot establish anything. ``from`` is what a restore writes
    # back, and a blank or absent one would make the restore DELETE or empty the
    # command -- which drops the server on the next resolution. That is the opposite
    # of this reader's contract: an unreadable record must degrade to the behavior
    # that predates the key, never to a lost server. There is no "the source carried
    # no such field" case to represent, because a record is only ever written for a
    # command that resolved, and the candidate it resolved from carried one.
    if not (isinstance(src, str) and src) or not (isinstance(emitted, str) and emitted):
        return None
    return {_FROM_FIELD: src, _EMITTED_FIELD: emitted}


def record_derived(entry: dict[str, Any], command_source: tuple[str, str] | None) -> dict[str, Any]:
    """Copy of ``entry`` recording what its ``command`` was derived FROM.

    ``command_source`` is ``(source, emitted)``: the value the rebuild resolved
    from, and the value it wrote. Pass ``None`` to record nothing, which is how a
    caller declines to claim a field it did not author.

    Both halves are load-bearing and both must be non-empty: the source is what a
    later rebuild re-derives from, and the emitted value is the only proof that the
    field on disk is still ours. A record written with either one blank is not
    readable back (see :func:`_command_record`), so it degrades to pre-record
    behavior rather than to a restore that empties the command.

    Both halves are load-bearing. The source is what a later rebuild re-derives
    from; the emitted value is the only proof that the field on disk is still ours.

    Keeping the source IN the entry is what makes the record safe for a server whose
    only persisted home is this file. Dropping the computed field on readback
    instead would be correct for a server that also lives in a scope config and
    destructive for one that does not.
    """
    out = {k: v for k, v in entry.items() if k != DERIVED_KEY}
    if command_source is None:
        return out
    out[DERIVED_KEY] = {
        _FROM_FIELD: command_source[0],
        _EMITTED_FIELD: command_source[1],
    }
    return out


def recorded_source(entry: object) -> tuple[str, str] | None:
    """The readable ``(source, emitted)`` pair on ``entry``, or None.

    For a caller that decides NOT to act on the record this pass but must not lose
    it: re-recording the pair verbatim keeps the original source available, so a
    later rebuild can still re-derive once whatever blocked it clears. Recording the
    currently-emitted value as its own source instead would quietly retire the
    record's only useful fact.
    """
    record = _command_record(entry)
    if record is None:
        return None
    src = record[_FROM_FIELD]
    emitted = record[_EMITTED_FIELD]
    assert isinstance(src, str) and isinstance(emitted, str)  # _command_record checks
    return (src, emitted)


def command_is_ours(entry: object) -> bool:
    """True when the stored ``command`` is still exactly what we recorded emitting.

    The caller needs this BEFORE :func:`source_view` rewrites anything, to decide
    what it may record on the way out. A field that was not ours on the way in and
    that the caller did not compute must not be recorded, or the next rebuild reads
    our claim as proof and overwrites the user's value -- the reclamation hazard
    this module's entry-level marker documents, one pass later.
    """
    record = _command_record(entry)
    if record is None or not isinstance(entry, dict):
        return False
    return entry.get(_COMMAND) == record[_EMITTED_FIELD]


def source_view(entry: object) -> dict[str, Any]:
    """Copy of ``entry`` with a ``command`` STILL ours restored to its source value.

    This is what a rebuild must read instead of its own previous output. Without it,
    a field the rebuild computed reads back as the user's and can never be
    re-derived, so a moved binary or a changed setting cannot take effect.

    **Only valid for an entry no other config source declares.** The caller must not
    apply this to a server that also lives in a scope config: the record holds what
    the PREVIOUS rebuild derived from, and because the entry is the caller's first
    resolution candidate, restoring it would pre-empt a live declaration edited
    since. Choosing between the two correctly means selecting a per-field source
    AFTER resolution -- the merge decides a winner by which command resolves and
    adopts that winner's ``args``/``env`` as a unit -- which is a merge precedence
    question, not a provenance one. The caller therefore restricts this to the entry
    that has no other source to lose to.

    The field is restored only while the stored value is byte-identical to what we
    recorded emitting. Anything else means someone edited it since -- the user by
    hand, or another writer -- and an edited field is theirs, so it is left exactly
    as it is. This is the entry-level marker's reclamation rule at field
    granularity, and it is why the record carries the emitted value as well as the
    source: without that proof, restoring would silently revert hand edits.

    The record itself is removed from the returned copy: it describes the emitted
    entry, and the next emit writes a fresh one.
    """
    if not isinstance(entry, dict):
        return {}
    out = {k: v for k, v in entry.items() if k != DERIVED_KEY}
    if not command_is_ours(entry):
        return out
    record = _command_record(entry)
    assert record is not None  # command_is_ours implies a readable record
    # A readable record's source is a non-empty string by construction, so the
    # restore can only ever REPLACE the command -- never delete or empty it, which
    # would drop the server on the next resolution.
    out[_COMMAND] = record[_FROM_FIELD]
    return out


def is_marked(entry: object) -> bool:
    """True when ``entry`` carries our authorship marker.

    Anything other than the exact shape reads as unmarked -- ``null``, a string, a
    dict whose ``managed`` is the string ``"yes"``. The predicate fails safe in
    the direction of NOT writing: a marker we cannot read is a marker we did not
    write. It says nothing about whether the name is PRESENT -- that is
    :data:`ABSENT`'s job, and :func:`resolve_write` asks it first.
    """
    if not isinstance(entry, dict):
        return False
    marker = entry.get(MARKER_KEY)
    return isinstance(marker, dict) and marker.get(_MANAGED_FIELD) is True


def stamp(entry: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``entry`` carrying the marker."""
    return {**entry, MARKER_KEY: {_MANAGED_FIELD: True}}


def without_marker(entry: object) -> dict[str, Any]:
    """Copy of ``entry`` with the marker removed.

    The marker records who wrote an entry in a file we do not own, so it is
    stripped where a shared entry is copied into the rendered agent spec -- that
    spec is output, and the key would say nothing to the runtime reading it.

    A non-dict answers as an empty dict so callers can strip uniformly without
    first re-checking a shape the marker predicate already tolerates.
    """
    if not isinstance(entry, dict):
        return {}
    return {k: v for k, v in entry.items() if k != MARKER_KEY}


def resolve_write(
    *,
    name: str,
    on_disk: object,
    candidate: dict[str, Any],
    store_managed: bool,
    surface: str,
) -> dict[str, Any] | None:
    """The entry to write for ``name``, or None to leave what is on disk alone.

    ``candidate`` is what this sync would write; ``on_disk`` is the current entry
    in the shared file, if any. ``store_managed`` is the store-side half of the
    predicate (see :func:`kiro_crew.mcp_discovery.kirocrew_managed_names`) and
    stays a necessary precondition -- the marker narrows who may be rewritten, it
    does not widen it.

    Three outcomes:

    * **create** -- the name is ABSENT from the file. We are authoring the entry,
      so it is stamped, but only for a name the store manages: a marker on a name
      we do not manage would claim an entry no later write is allowed to touch
      anyway. Only :data:`ABSENT` reaches this branch; a present value we cannot
      parse (a string, ``null``, a list) is NOT a free slot -- it occupies the
      name and cannot carry a marker, so the invariant reads it as the user's and
      it declines below.
    * **rewrite** -- the entry carries our marker. This is scope propagation,
      now gated on proof rather than on a name.
    * **decline** -- the entry is present and unmarked. Nothing proves we wrote
      it, so it is left exactly as it is and the divergence is logged. There is
      no content test that could widen this: an unmarked entry whose bytes match
      our emit is BOTH a pre-marker entry and a deliberately reclaimed one, so
      stamping it would undo the reclamation the marker promises.
    """
    if on_disk is ABSENT:
        return stamp(candidate) if store_managed else candidate
    if not store_managed:
        return None
    if is_marked(on_disk):
        return stamp(candidate)
    logger.warning(
        "Declining to rewrite unmarked MCP entry %r in %s: the name is managed but "
        "the entry carries no Kiro Crew marker, so it reads as hand-authored and is "
        "left as-is",
        name,
        surface,
    )
    return None
