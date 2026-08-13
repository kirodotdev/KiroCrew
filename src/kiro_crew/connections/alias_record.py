"""Persisted record of the ``toolAliases`` pairs the alias pass itself emitted.

WHY A RECORD EXISTS
===================
The alias pass must clean up its OWN stale emissions: a renamed or withdrawn
registry declaration leaves a superseded ``@slug/tool -> alias`` pair behind in
the spec, and a rename that outlives its collision recreates the shadowing the
feature exists to remove. So cleanup needs to answer "did I write this pair?".

Answering it from the pair's SHAPE was tried three ways and each failed, because
shape asks about the present while ownership is a fact about the PAST:

* ``alias.startswith(f"{slug}_")`` claims a user's hand-written
  ``@linear/list_issues -> linear_issues``, so a rebuild deletes a deliberate
  edit.
* ``alias == f"{slug}_{tool}"`` (re-derivation) claims a user's hand-written
  ``@notion/search -> notion_search`` even though ``notion`` declares no aliases
  at all and this pass has never emitted anything for it. The shape of a name the
  pass WOULD emit is no evidence that it DID.
* Narrowing re-derivation to slugs that currently declare aliases reintroduces
  the first failure from the other side: a withdrawn declaration takes its slug
  out of the test, so the pair that declaration stranded stops being recognised
  as ours and becomes permanently unclearable.

Shape cannot decide history. This module persists the history instead: exactly
the ``(slug, tool, alias)`` triples the LAST pass emitted.

WHY THE RECORD IS GENERATION-BOUND
==================================
The record and the spec are two files, and kiro-cli validates specs with
``deny_unknown_fields``, so an ownership marker cannot live inside the spec -- an
extra key makes kiro-cli reject the whole file and the user loses every tool.
Two files means two writes, and no ORDERING of two writes closes both failure
directions: record-after-spec leaves an emitted-but-unrecorded pair, and
record-before-spec leaves a record claiming a pair the spec never carried, which
is what deletes a name the user has since hand-written.

Ordering is therefore not the variable. The record instead carries a
**fingerprint of the spec generation it describes**, and a write is a two-phase
transaction, so a record can never be silently mistaken for a description of a
spec it does not match:

    PENDING(previous=(fp(old map), old claim), target=(fp(new map), new emission))
    -> spec write
    -> COMMITTED(fp(new map), new emission)

A ``COMMITTED`` record describes a spec write that is already durable, and it is
consulted only as a description of THAT generation: its stored fingerprint must
equal the map on disk. On the normal path it always does -- the record is written
right after its spec write, and the next pass replaces it with a ``PENDING``
record before touching the spec -- so the gate is inert and the happy path, and a
user's hand-edit, behave exactly as they would with no transaction at all
(invariant 3 still decides each pair). The gate earns its keep when the record
OUTLIVES its spec: delete or replace the spec out of band and an ungated
committed record would still name triples, authorizing the deletion of an alias
that is by then the user's only copy.

A ``PENDING`` record is the interrupted case, and the fingerprint resolves it:
whichever of its two candidate generations matches the map on disk is the one
that is really there.

THE STATE TABLE (every reachable crash boundary, both directions)
=================================================================
``M`` is the spec's ``toolAliases`` map; ``C`` the claim the pass was authorised
to strip; ``E`` what it emitted. Read "resolves to" as what the NEXT pass treats
as its own and may therefore delete.

===  ==============================  ================  ========  ==================================
 #   interrupted at                  record on disk    map       resolves to
===  ==============================  ================  ========  ==================================
 0   before the pending write        unchanged         M_old     unchanged -- pass is a no-op retry
 1   pending write FAILS             unchanged         M_old     unchanged; spec MUST NOT advance
 2   kill after pending write        PENDING           M_old     previous candidate -> C
 3   spec write FAILS                PENDING           M_old     previous candidate -> C
 4   kill after spec write           PENDING           M_new     target candidate -> E
 5   commit write FAILS              PENDING           M_new     target candidate -> E
 6   nothing (success)               COMMITTED         M_new     E
 7   record absent/unusable/v1       --                any       nothing (invariant 4)
 8   map hand-edited under PENDING   PENDING           M_other   nothing -- neither candidate matches
 9   spec replaced/removed out of    COMMITTED         M_other   nothing -- fingerprint gate rejects it
     band under COMMITTED
===  ==============================  ================  ========  ==================================

Row 7 covers a record that is absent, unreadable, malformed, written by an older
version, or carrying no syntactically valid fingerprint: none of those describes a
generation, so none may authorize a deletion.

Row 9 is the orphaned committed record: the transaction completed, then the spec
it described was deleted or rewritten by something that is not this pass. The
record is intact and internally consistent, so only the EQUALITY gate can tell
that it no longer describes anything on disk. Without that gate it would claim
triples the current map may hold from another source -- a user's own alias -- and
strip them.

Both directions are safe on every row, simultaneously:

* **Never deletes a user's exact hand-written alias.** Rows 2/3 resolve to the
  claim that was already valid for the map still on disk, so nothing new is
  claimed. Rows 4/5 resolve to ``E``, which by construction contains only pairs
  this pass wrote. Rows 7/8/9 claim nothing. And on every row invariant 3 still
  requires the whole triple to match, so an edited alias is never claimed.
* **Never permanently strands a generated alias.** Rows 4/5 -- the boundary that
  previously left an alias emitted but unrecorded forever -- now resolve to
  ``E``, so the interrupted transaction is RECONCILED by the next pass rather
  than abandoned. Rows 2/3 leave the spec unchanged, so there is nothing to
  strand. Row 1 fails closed for the same reason.

Rows 7 and 9 are where a generated pair does become permanently the user's:
losing the record file outright, or losing the spec generation the record
described. Both are invariant 4's deliberate degradation (stale aliases linger,
which is the pre-feature shadowing), and neither is reachable by a write failure
or a kill -- only by deleting or replacing one of the two files out of band.

INVARIANTS
==========
1. **The record is the only ownership oracle.** A pair is this pass's own iff the
   resolved record holds it. Absence proves nothing except "not provably ours",
   which is the safe reading -- an unrecorded pair is treated as the user's and
   survives. No shape rule, prefix test or re-derivation participates.

2. **The record AUTHORIZES deletion, so it must never OVERSTATE.** The
   fingerprint is what enforces that across a crash: a record is consulted only
   as a description of a specific spec generation, and a description that cannot
   be matched to the map on disk claims nothing. This holds for BOTH statuses --
   a committed record that no longer matches the map on disk has outlived its
   spec (row 9) and claims nothing either.

3. **Membership IS the byte-equality test.** The alias is part of the key, so a
   pair is claimed only when the spec's CURRENT value matches the recorded form
   byte for byte. A user who edits a generated alias produces a triple the record
   does not hold, so their edit is left alone; no separate comparison is needed
   and none may be added, or the two checks could disagree.

4. **A missing, unreadable or malformed record is EMPTY, and empty claims
   NOTHING.** Losing the record degrades to "every pair is the user's": stale
   aliases linger (shadowing, the pre-feature behaviour) rather than a user's
   entries being deleted on a bad parse. Individual entries that are not three
   strings are dropped for the same reason -- dropping understates. A record
   written by an older version reads as empty too: carrying a v1 record forward
   would inherit exactly the unverifiable claim this version exists to prevent.
   So does a record whose fingerprint is not the shape
   :func:`spec_fingerprint` produces (64 lowercase hex): it names no generation,
   and the committed branch's freedom from an EQUALITY gate is only sound while
   the record is demonstrably this writer's.

5. **The TRANSITION belongs to the writer of the spec, not to the alias pass.**
   :func:`~kiro_crew.agent.rebuild_agent_config` opens and commits it around its own
   spec write, for EVERY write that changes the map -- including a clean rebuild and
   one where the alias pass never runs. A clean write replaces the map wholesale, so
   a claim left describing the replaced generation is the same deletion hazard a
   crash would produce. When the map does not change, the transition is skipped
   entirely: rewriting the record empty there would forget a real emission and
   strand every pair it named.

6. **Both record writes are LOUD, and the pending write FAILS CLOSED.**
   :func:`~kiro_crew.atomic_write.atomic_write` publishes through a temp file and
   a rename and leaves the destination alone on failure, so a failed write means
   the record still describes the PREVIOUS generation. That is safe only while
   the spec also still is that generation -- so a failed pending write must stop
   the pass before it touches the spec (row 1), and a failed commit write leaves
   the pending record, which row 5 resolves correctly. Both raise rather than
   swallow: the spec is durable and correct, but an unwritable data home is
   reported when it happens instead of surfacing later as aliases nobody can
   explain.

7. **The fingerprint is taken over the AUTHORITATIVE on-disk map.** Resolution
   compares against the spec generation that is really on disk, read inside the
   same critical section that writes it. A fingerprint taken over a stale
   in-memory snapshot would match a generation that is no longer there, which is
   the one way the fingerprint could certify a claim it should have rejected.

The record is process-wide rather than per-spec because
:func:`~kiro_crew.agent.rebuild_agent_config` writes one canonical spec, and that
pass is the only emitter.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import NamedTuple

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

# A single emitted pair: the provider slug, the declared source tool, and the
# alias that was written for it.
EmittedAlias = tuple[str, str, str]

# Sidecar under the Kiro Crew data home. kiro-cli validates agent specs with
# ``deny_unknown_fields``, so an in-spec ownership marker is impossible -- the
# record lives out of band in a directory Kiro Crew owns outright, mirroring the
# ``owned-mcp-keys.json`` manifest that answers the same question for MCP server
# keys (see :mod:`kiro_crew.browser.setup`).
_RECORD_FILENAME = "connections-tool-aliases.json"

# 2 adds the generation fingerprint and the pending/committed phase. An
# unrecognised version reads as empty (invariant 4), which is also why no
# migration from 1 exists: a v1 record cannot say which spec generation it
# describes, so honouring it would reintroduce the unverifiable claim.
_RECORD_VERSION = 2

_STATUS_COMMITTED = "committed"
_STATUS_PENDING = "pending"

# Fingerprint of "the spec has no toolAliases key at all", kept distinct from an
# empty map so a generation that removed the key cannot be mistaken for one that
# emptied it.
_ABSENT_MAP = "\x00absent"


class AliasGeneration(NamedTuple):
    """A spec generation: the pairs emitted into it, and its map's fingerprint."""

    fingerprint: str
    emitted: frozenset[EmittedAlias]


def record_path() -> Path:
    """Path of the emitted-alias record sidecar."""
    return config_dir() / _RECORD_FILENAME


def spec_fingerprint(aliases: object) -> str:
    """Fingerprint the ``toolAliases`` map exactly as the spec carries it.

    Canonical over key order so an unrelated re-serialisation cannot look like a
    new generation. A missing key, or any non-dict value a hand-edit could leave,
    fingerprints as the absent map: neither carries a pair this pass could own.
    """
    if isinstance(aliases, Mapping):
        payload = json.dumps(
            sorted((str(k), str(v)) for k, v in aliases.items()),
            separators=(",", ":"),
        )
    else:
        payload = _ABSENT_MAP
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def split_tool_ref(ref: object) -> tuple[str, str] | None:
    """Split ``"@slug/tool"`` into ``(slug, tool)``, or None if it is not one.

    Rejects a whole-server ref (``@linear``), a missing ``@`` and an empty half:
    none of those name a single tool, so none can be a recorded emission.
    """
    if not isinstance(ref, str) or not ref.startswith("@") or "/" not in ref:
        return None
    slug, _, tool = ref[1:].partition("/")
    return (slug, tool) if slug and tool else None


def emitted_from_alias_map(aliases: Mapping[str, str]) -> frozenset[EmittedAlias]:
    """Convert a written ``{"@slug/tool": alias}`` map into record triples."""
    triples: set[EmittedAlias] = set()
    for ref, alias in aliases.items():
        parts = split_tool_ref(ref)
        if parts is not None and isinstance(alias, str):
            triples.add((parts[0], parts[1], alias))
    return frozenset(triples)


def _triples(entries: object) -> frozenset[EmittedAlias]:
    """Read an ``emitted`` list, dropping anything that is not three strings."""
    if not isinstance(entries, list):
        return frozenset()
    triples: set[EmittedAlias] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug, tool, alias = entry.get("slug"), entry.get("tool"), entry.get("alias")
        if isinstance(slug, str) and isinstance(tool, str) and isinstance(alias, str):
            triples.add((slug, tool, alias))
    return frozenset(triples)


def _payload(entries: Collection[EmittedAlias]) -> list[dict[str, str]]:
    return [
        {"slug": slug, "tool": tool, "alias": alias} for slug, tool, alias in sorted(entries)
    ]


def _valid_fingerprint(value: object) -> bool:
    """True when *value* is the shape :func:`spec_fingerprint` produces.

    A payload that parses as v2 but carries no fingerprint, a truncated one, or
    non-hex text cannot describe ANY generation -- it is malformed, not a
    description of the absent map, so it must read as empty (invariant 4) rather
    than be trusted. Checked before either status branch: a value of this shape is
    what the equality comparison is meaningful against, and a record this writer
    cannot have produced names no generation to compare.
    """
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def load_claimed(fingerprint: str) -> frozenset[EmittedAlias]:
    """Return the triples this pass may strip from the map *fingerprint* describes.

    *fingerprint* must come from the AUTHORITATIVE on-disk map (invariant 7). Both
    statuses are gated on it: a committed record must describe the generation that
    is really on disk, and a pending one is resolved by matching *fingerprint*
    against its target then its previous generation, which is what tells an
    interrupted transaction whether its spec write landed. Empty on every failure --
    absent file, unreadable file, malformed JSON, unexpected shape, older version, a
    committed record whose spec is gone or was replaced out of band, or a pending
    record matching neither generation (invariant 4). Empty claims nothing, so it
    never costs a user's alias.
    """
    try:
        raw = record_path().read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.debug("Ignoring malformed Connections alias record", exc_info=True)
        return frozenset()
    if not isinstance(data, dict) or data.get("version") != _RECORD_VERSION:
        return frozenset()

    status = data.get("status")
    stored = data.get("fingerprint")
    if not _valid_fingerprint(stored):
        # Malformed: parseable, but it names no generation at all. Trusting the
        # emissions beside it would authorize deletion on the word of a record this
        # writer cannot have produced.
        logger.debug("Ignoring Connections alias record with no valid fingerprint")
        return frozenset()

    if status == _STATUS_COMMITTED:
        # EQUALITY GATE. A committed record is written only after its spec write is
        # durable, so on the normal path it describes the map now on disk and the
        # gate is inert. It is NOT inert when the record OUTLIVES its spec: delete
        # or replace the spec out of band and the committed record still names
        # triples, which would authorize stripping an alias that is now the user's
        # only copy (invariant 2 -- a record must never overstate). Requiring the
        # recorded generation to be the generation on disk closes that, and moves
        # the failure direction to a lingering generated alias, which is
        # invariant 4's deliberate degradation rather than data loss.
        if stored != fingerprint:
            logger.debug(
                "Ignoring committed Connections alias record: it describes a spec "
                "generation that is not the one on disk"
            )
            return frozenset()
        return _triples(data.get("emitted"))
    if status != _STATUS_PENDING:
        return frozenset()

    # Interrupted transaction: exactly one of the two generations is on disk.
    # Target first -- if it matches, the spec write landed (rows 4/5), and that is
    # also the right answer when the transaction was a no-op on the map.
    if stored == fingerprint:
        return _triples(data.get("emitted"))
    previous = data.get("previous")
    # No separate shape check on `previous`: *fingerprint* is always a real
    # :func:`spec_fingerprint` value, so an unusable stored one cannot equal it.
    if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
        return _triples(previous.get("emitted"))
    # Neither: the map was changed by something that is not this pass (row 8).
    logger.debug("Connections alias record describes neither generation on disk")
    return frozenset()


def _write(payload: dict[str, object], *, phase: str) -> None:
    path = record_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(payload, indent=2) + "\n", mode=0o600)
    except OSError:
        logger.error(
            "Could not persist the %s Connections alias record at %s. Ownership "
            "bookkeeping for tool aliases is not up to date; the spec itself is "
            "unaffected.",
            phase,
            path,
        )
        raise


def begin_transaction(previous: AliasGeneration, target: AliasGeneration) -> None:
    """Record that the pass is ABOUT to write *target* over *previous*.

    Call BEFORE the spec write, and treat a failure as fatal to the pass: with the
    record still describing *previous*, the spec must stay at *previous* too
    (invariant 6, row 1). Once this lands, either generation is recoverable --
    whichever one the map on disk turns out to be is the one
    :func:`load_claimed` returns.

    Raises:
        OSError: the record could not be written. The caller MUST NOT let the spec
            advance; nothing has been changed, so the next rebuild retries cleanly.
    """
    _write(
        {
            "version": _RECORD_VERSION,
            "status": _STATUS_PENDING,
            "fingerprint": target.fingerprint,
            "emitted": _payload(target.emitted),
            "previous": {
                "fingerprint": previous.fingerprint,
                "emitted": _payload(previous.emitted),
            },
        },
        phase="pending",
    )


def commit_transaction(target: AliasGeneration) -> None:
    """Mark *target* as the durable generation, now that its spec write landed.

    Call only AFTER the spec carrying those aliases is durable. Failing here is
    recoverable rather than harmful: the pending record stays, and because its
    target fingerprint matches the map now on disk, the next pass resolves to
    exactly this emission (row 5) instead of abandoning it. It still raises,
    because an unwritable data home is worth reporting when it happens.

    Raises:
        OSError: the record could not be committed. The spec is already durable and
            the pending record already describes it, so this reports a data home
            that cannot be written -- not a bad spec and not a lost emission.
    """
    _write(
        {
            "version": _RECORD_VERSION,
            "status": _STATUS_COMMITTED,
            "fingerprint": target.fingerprint,
            "emitted": _payload(target.emitted),
        },
        phase="committed",
    )


def is_recorded_emission(
    record: Collection[EmittedAlias], ref: object, alias: object
) -> bool:
    """True when the record proves THIS pass wrote ``(ref, alias)``.

    The whole triple must be present, so the recorded alias doubles as the
    byte-equality test on the spec's current value (invariant 3).
    """
    parts = split_tool_ref(ref)
    if parts is None or not isinstance(alias, str):
        return False
    return (parts[0], parts[1], alias) in record
