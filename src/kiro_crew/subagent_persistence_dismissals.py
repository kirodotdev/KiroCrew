"""Panel dismissals, one file per dismissed run under the data home's
``panel-dismissals`` leaf: recorded, read, deleted with the run, and pruned once the run
is gone; and the ceilings on a panel record's equality keys, which a dismissal's path
honours too.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.subagent_persistence_layout import (
    _agent_dir,
    _subagents_dir,
    logger,
)

# ── panel dismissals ─────────────────────────────────────────────────

# One file per dismissed run, named for the run, so the whole set is one
# directory listing and two dismissals never contend over a shared file.
#
# A top-level leaf of the data home, NOT a subdirectory of ``trust``. The same
# name is registered in ``security/paths_lists.py`` and in three lists in ``sandbox_leaves.py``,
# ``sandbox_precreate.py`` and ``sandbox_seals.py``; a rename here without those is a
# silently unprotected store, which ``test_subagent_panel_durable_replay.py`` pins by
# asserting this constant is present in each.
_PANEL_DISMISSAL_LEAF = "panel-dismissals"

#: What a dismissal write did. Three outcomes rather than a bool, because the two
#: falsy ones need OPPOSITE answers from a caller that is about to publish the
#: dismissal: a run with no folder has nothing durable to bring its card back, so
#: the dismissal stands, while a write that FAILED means the card returns on the
#: next rebuild and the caller must not report success.
DISMISSAL_RECORDED = "recorded"
DISMISSAL_NO_FOLDER = "no_folder"
DISMISSAL_FAILED = "failed"


def _panel_dismissals_dir() -> Path:
    """Gateway-owned home for panel dismissals, outside the run folders.

    A dismissal decides what the panel HIDES, so neither the run it is about nor
    any other sandboxed process may write one. That makes it an OWNER-authority
    record, and this repository already settled where those live: ``crew-panels``,
    ``crew-teams`` and ``tag-grants`` are each their own top-level leaf
    specifically because ``trust`` is a declared sandbox READ-WRITE exception
    (in-sandbox ``verify_session_pid`` reads ``trust/sel_hmac.key`` and the
    in-sandbox MCP servers append to the audit log). A record under ``trust``
    stays writable by a sandboxed command that builds the path at runtime, which
    command matching cannot catch because it has no literal path to match. So
    this is a sibling of those three, not of the cleanup identities that predate
    the rule, and it is registered in all four places one requires:
    ``paths.SENSITIVE_*`` for the file gate, ``sandbox._CREW_HIDDEN_LEAVES`` for
    the bind mask, ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES`` because the mask loop
    SKIPS a name that does not exist yet and this directory is created on the
    first dismissal, and ``_CREW_NO_ALIAS_LEAVES`` because a link would attach
    the mask to the target while leaving the name writable.

    ``tombstone.json`` and ``state.json``, by contrast, sit in the agent-writable
    run folder, where a run could hide its own card from every later rebuild.

    Resolved through ``_subagents_dir().parent`` rather than ``data_home()``
    directly, which is this module's existing way of naming a data-home sibling
    (see the member-memory bindings): in production the two are the same path,
    and only the former follows the ``_SUBAGENTS_DIR`` test hook, so a test's
    registry and its dismissals cannot end up in different homes.
    """
    return _subagents_dir().parent / _PANEL_DISMISSAL_LEAF


def _panel_dismissal_path(agent_id: str) -> Path:
    """The record's path, for an id that is safe to spell as a file name.

    The canonical agent-directory guard settles traversal, and the remaining
    charset rule is about the FILE this id names: a run id reaches here from the
    route's URL, and ``:`` or a trailing dot is a legal folder name on POSIX and
    an illegal file name on Windows. Ids are hex from ``_mint_agent_id``, so the
    rule costs a real dismissal nothing.
    """
    _agent_dir(agent_id)
    if len(agent_id) > _PANEL_ID_CAP or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", agent_id):
        raise ValueError(f"Invalid agent_id for a dismissal record: {agent_id!r}")
    return _panel_dismissals_dir() / agent_id


def record_panel_dismissal(agent_id: str) -> bool:
    """Record that a finished run's panel card was dismissed. True when stored.

    A dismissal has to be durable because the panel's durable half reads run
    FOLDERS: dropping the run from the manager hid the card only until the next
    rebuild, which found the folder the delete left behind and sent the card
    again. This record is what that reader consults.

    The folder itself is deliberately left alone. ``spawn_continue`` reseeds its
    session map from ``state.json`` in there, so deleting a folder to hide a card
    would also destroy a conversation the user can still continue, and would take
    the run's result text and its prune bookkeeping with it.

    False when the run has no folder: nothing durable can resurrect that card, so
    there is nothing to suppress, and a record for it would outlive every path
    that reclaims one.

    A caller that must not publish a dismissal it failed to store wants
    :func:`record_panel_dismissal_outcome` instead: this bool cannot tell "no
    folder, so nothing to suppress" from "the write failed", and those two need
    opposite answers.
    """
    return record_panel_dismissal_outcome(agent_id) == DISMISSAL_RECORDED


def record_panel_dismissal_outcome(agent_id: str) -> str:
    """:func:`record_panel_dismissal`, with its two falsy cases kept apart.

    One of them is success in every sense that matters -- a run with no folder has
    nothing durable to resurrect its card -- and the other means the card WILL come
    back. A caller that pops the run out of the manager on either one reports a
    dismissal that did not happen, so the distinction lives here rather than in
    each caller's reading of a bool.
    """
    try:
        if not _agent_dir(agent_id).is_dir():
            return DISMISSAL_NO_FOLDER
        path = _panel_dismissal_path(agent_id)
    except ValueError:
        # Not an id any run could carry, so no folder can ever claim it: the same
        # answer as a missing folder, not a fault.
        return DISMISSAL_NO_FOLDER
    except OSError:
        # The folder check itself faulted, so whether a folder exists is UNKNOWN.
        # Reporting "no folder" here would let a caller publish a dismissal for a
        # run whose card the next rebuild still finds.
        logger.warning("panel dismissal folder check failed for %s", agent_id, exc_info=True)
        return DISMISSAL_FAILED
    try:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        platform_compat.make_owner_only_dir(directory)
        platform_compat.restrict_dir_to_owner(directory)
        # ``atomic_write``, not a write-then-restrict pair: it applies the
        # owner-only lockdown to the TEMP file before any content reaches it and
        # before the rename, so the record never exists in a world-readable file.
        # ``scripts/check_lockdown_before_publish.py`` enforces that ordering.
        # Every failure it raises -- a lockdown that cannot be applied, and the
        # linked-parent refusal ``restrict_to_owner`` implies -- is an OSError,
        # so the fail-open contract below still covers all of them.
        atomic_write(
            path,
            json.dumps({"id": agent_id, "at": time.time()}),
            restrict_to_owner=True,
        )
    except OSError:
        # The card returns on the next rebuild, and no reader can explain why, so
        # the operator log carries the reason. Raising instead would leave the run
        # gone from the manager and still undismissed -- strictly worse.
        logger.warning("panel dismissal not recorded for %s", agent_id, exc_info=True)
        return DISMISSAL_FAILED
    return DISMISSAL_RECORDED


def panel_dismissal_recorded(agent_id: str) -> bool:
    """Is THIS run's card dismissed? One stat, nothing retained.

    The membership question the panel reader actually asks, per candidate it is
    already scanning. Enumerating every dismissal into a set to answer it would
    retain one entry per record inside a function whose whole contract is a
    bounded working set -- the registry's size would set the memory, which is the
    bound's own reason for existing.

    Fails OPEN, the same direction as :func:`dismissed_panel_ids`: an unreadable
    record answers "not dismissed", so a filesystem fault resurrects a card the
    user can dismiss again rather than hiding a run nobody dismissed.
    """
    try:
        return _panel_dismissal_path(agent_id).is_file()
    except (OSError, ValueError):
        return False


def dismissed_panel_ids() -> frozenset[str]:
    """Run ids whose panel card the user dismissed.

    Fails OPEN. An unreadable record yields no id, so a filesystem fault
    resurrects a dismissed card -- which the user can dismiss again -- rather
    than hiding a run the operator never dismissed. Hiding is the direction that
    must not happen by accident.
    """
    try:
        scan = os.scandir(_panel_dismissals_dir())
    except OSError:
        return frozenset()
    found: set[str] = set()
    with scan:
        for entry in scan:
            try:
                if entry.is_file():
                    found.add(entry.name)
            except OSError:
                continue
    return frozenset(found)


def _delete_panel_dismissal(agent_id: str) -> None:
    """Drop the record once its run folder is gone, so the set stays bounded."""
    try:
        _panel_dismissal_path(agent_id).unlink(missing_ok=True)
    except (OSError, ValueError):
        logger.debug("panel dismissal cleanup failed for %s", agent_id, exc_info=True)


def prune_orphan_panel_dismissals() -> int:
    """Drop records whose run folder is already gone. Returns the count removed.

    The two folder-removal paths delete a run's record with the folder, so this
    answers for a folder that left by some other route -- an operator clearing the
    registry by hand, a restore from a backup taken before the dismissal.

    Unlinks by DIRECTORY ENTRY rather than by rebuilding the path from the name: a
    name this directory holds but the id rule rejects would otherwise be
    unremovable, and counted as removed on every cycle. A scandir name is always
    one path component, so the entry's own path needs no traversal check.
    """
    removed = 0
    try:
        scan = os.scandir(_panel_dismissals_dir())
    except OSError:
        return 0
    with scan:
        for entry in scan:
            try:
                if not entry.is_file():
                    continue
                if _agent_dir(entry.name).is_dir():
                    continue
            except OSError:
                continue
            except ValueError:
                pass  # not an id any run could carry, so no folder can claim it
            try:
                os.unlink(entry.path)
            except OSError:
                logger.debug("panel dismissal sweep failed for %s", entry.name, exc_info=True)
                continue
            removed += 1
    return removed


#: Ceilings on the record's EQUALITY KEYS. These REFUSE the record rather than
#: clamping it, because each one is compared for equality and a truncated key
#: would compare unequal while still looking like a value: the id addresses the
#: run folder, the app is matched against a slot's current owner, and the parent
#: session is matched against a caller and resolved into a slot key.
#:
#: Every string a record retains is accounted for here or clamped by the text caps
#: in :mod:`kiro_crew.subagent_persistence`, and the two lists are the whole record:
#: ``id``, ``app`` and ``parent_session`` are
#: keys; ``task``, ``agent``, ``error`` and ``result`` are text; ``started``,
#: ``elapsed`` and ``stopped`` are numbers and a flag; ``outcome`` is one of
#: three fixed words. A field added to the record belongs in one of the two.
_PANEL_ID_CAP = 255
_PANEL_APP_CAP = 200
_PANEL_PARENT_SESSION_CAP = 400
