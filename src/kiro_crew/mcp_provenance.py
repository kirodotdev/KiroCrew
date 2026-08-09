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
