"""Launches that keep an approval while their stored digest differs.

Two repairs leave the launch the person approved byte for byte the same while
its digest moves, so :meth:`LaunchApprovals.admit` admits the new digest under
the approved one instead of asking again:

* the PATHEXT casing repair rewrites the extension case of a Windows command
  (:func:`casing_repair_of_approved`);
* launch approvals began folding the gateway's own part of ``PATH``
  (``hashing.hash_launch_env``), so a store written before that holds the
  literal digests of launches that have not changed (:func:`prefold_approval_of`).

Each returns the approved fingerprint and pair the new digest is admitted under;
``save_pass`` writes the new digest only while the store still approves that
source. Standard library only, like :mod:`kiro_crew.mcp_gateway.launch_approval`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Iterable, Mapping

from kiro_crew.mcp_gateway.hashing import hash_command, hash_declared_env
from kiro_crew.mcp_gateway.launch_approval_normalize import NORMALIZED_AUDIT_REASON

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.launch_approval import LaunchApprovals


def casing_repair_of_approved(
    approvals: LaunchApprovals,
    stem: str,
    fingerprint: str,
    derived_env_hash: str,
    launch: tuple[str, list[str]] | None,
) -> tuple[str, str] | None:
    """The approved fingerprint and pair *launch* repairs, or ``None``.

    The approved spelling differs from the live command only in the case of
    its extension, the repair maps it onto the live command, and its exact
    pair, same args and env, is approved with a stored display. The repair
    reads the spelling from the directory entry, so no agent chooses it.
    """
    if launch is None:
        return None
    from kiro_crew.env import saved_command_casing  # deferred: config.loader import path
    from kiro_crew.mcp_gateway.launch_approval import launch_pair

    command, args = str(launch[0]), [str(a) for a in launch[1]]
    root, ext = os.path.splitext(command)
    env_hash = fingerprint.partition(":")[2]
    for spelling in {root + ext.upper(), root + ext.lower()} - {command}:
        approved_hash = hash_command(spelling, args)
        source = f"{approved_hash}:{env_hash}"
        source_pair = launch_pair(approved_hash, derived_env_hash)
        if (
            source in approvals.approved.get(stem, ())
            and approvals.admits_launch(stem, approved_hash, derived_env_hash)
            and source_pair in approvals.approved_displays.get(stem, {})
            and saved_command_casing(spelling) == command
        ):
            return source, source_pair
    return None


def prefold_digests(
    declared_env: Mapping[Any, Any], expanded_env: Mapping[Any, Any]
) -> tuple[str, str]:
    """The env digests a store written before the ``PATH`` fold holds for one launch.

    The declared text and its expansion, hashed as written, as
    ``launch_fingerprint`` and the rewriter's derived hash did before the fold.
    """
    from kiro_crew.mcp_gateway.launch_approval import _coerced_env

    return hash_declared_env(_coerced_env(declared_env)), hash_declared_env(
        _coerced_env(expanded_env)
    )


def prefold_approval_of(
    approvals: LaunchApprovals,
    stem: str,
    fingerprint: str,
    prefold: tuple[str, str] | None,
    launch: tuple[str, list[str]] | None = None,
) -> tuple[str, str] | None:
    """The pre-fold approval of this exact launch, or ``None``.

    *prefold* is :func:`prefold_digests` of the launch behind *fingerprint*.
    Its fingerprint and pair match only when the command, args and env are the
    same bytes the person approved, so the folded digest of that launch is the
    approved launch, not a widened one. A launch whose digest did not move
    (nothing folded) has nothing to carry over. When the PATHEXT casing repair
    also rewrote the command since the approval, the approved spelling is tried
    too, under the same check as :func:`casing_repair_of_approved`.
    """
    if prefold is None:
        return None
    from kiro_crew.mcp_gateway.launch_approval import launch_pair

    live_hash = fingerprint.partition(":")[0]
    declared, derived = prefold
    if f"{live_hash}:{declared}" == fingerprint:
        return None
    for command_hash in (live_hash, *_approved_spellings(approvals, stem, launch)):
        source = f"{command_hash}:{declared}"
        if source in approvals.approved.get(stem, ()) and approvals.admits_launch(
            stem, command_hash, derived
        ):
            return source, launch_pair(command_hash, derived)
    return None


def _approved_spellings(
    approvals: LaunchApprovals, stem: str, launch: tuple[str, list[str]] | None
) -> list[str]:
    """Command hashes of the spellings the PATHEXT casing repair maps onto *launch*."""
    if launch is None:
        return []
    from kiro_crew.env import saved_command_casing  # deferred: config.loader import path

    command, args = str(launch[0]), [str(a) for a in launch[1]]
    root, ext = os.path.splitext(command)
    return [
        hash_command(spelling, args)
        for spelling in sorted({root + ext.upper(), root + ext.lower()} - {command})
        if saved_command_casing(spelling) == command
    ]


def without_sources(
    approvals: LaunchApprovals,
    stem: str,
    prefolded: tuple[str, str] | None,
    additions: Iterable[tuple[Mapping[str, Collection[Any]], Collection[Any]]],
    trusted: bool = False,
) -> list[tuple[Mapping[str, Collection[Any]], Collection[Any]]]:
    """*additions* with the approvals that a launch replaces left out of its population.

    A carried-over launch replaces its pre-fold source, so the per-server cap
    counts that launch once, not once per digest. A trusted change replaces the
    launch it changed from: its approved population counts only the launches
    this pass found live, as a complete pass's save leaves it
    (:func:`retire_trusted`); an incomplete pass's save still caps the whole store.
    """
    sources = {
        part
        for source in (prefolded, *approvals.prefold_healed.get(stem, {}).values())
        if source is not None
        for part in source
    }
    live: dict[int, set[str]] = {}
    if trusted:
        live[id(approvals.approved)] = approvals.trust_live.get(stem, set())
        live[id(approvals.approved_pairs)] = set(
            approvals.launch_identities.get(stem, {})
        ) | approvals.live_overflow.get(stem, set())
    if not sources and not live:
        return list(additions)
    return [
        (
            {
                stem: [
                    x
                    for x in population.get(stem, ())
                    if x not in sources and x in live.get(id(population), {x})
                ]
            },
            candidates,
        )
        for population, candidates in additions
    ]


def superseded_sources(
    approvals: LaunchApprovals, stem: str, withdrawn: Collection[str]
) -> set[str]:
    """The pre-fold fingerprints and pairs a save replaces with their folded ones."""
    return {
        part
        for fp, source in approvals.prefold_healed.get(stem, {}).items()
        if fp not in withdrawn
        for part in source
    }


#: Audit reason for a pair admitted because only a ``${VAR}`` value changed.
REBIND_AUDIT_REASON = "approved launch, ${VAR} value changed"
#: Audit reason for a pair admitted because only the PATHEXT casing repair moved.
CASING_AUDIT_REASON = "approved launch, Windows command casing repaired"
#: Audit reason for a pair admitted under its approval from before the PATH fold.
PREFOLD_AUDIT_REASON = "approved launch, stored before the gateway PATH fold"
#: Audit reason for a changed launch of a server whose changes the operator trusts.
TRUST_AUDIT_REASON = "changed launch of a server the operator trusts"
#: Audit reason for a launch the gateway start recorded by itself (``LaunchApprovals.auto``).
AUTO_AUDIT_REASON = "recorded automatically at gateway start (sharing has no approval step)"


def trusts_change(approvals: LaunchApprovals, stem: str, managed: bool) -> bool:
    """Whether a changed launch of *stem* is admitted because the operator trusts it.

    The dashboard's approve press sets the choice, and only with an approval: a
    stem with no approved launch is not admitted by it. A pass with
    ``auto`` (the gateway start) admits every stem, its first launch included. A never-pooled server or a managed launch is never admitted
    this way.
    """
    from kiro_crew.mcp_gateway.launch_approval import _unpoolable_stems

    if managed or stem in _unpoolable_stems():
        return False
    return approvals.auto or (stem in approvals.trusted and bool(approvals.approved.get(stem)))


def captured_by_reason(
    approvals: LaunchApprovals, withheld: Mapping[str, set[str]]
) -> dict[str, dict[str, set[str]]]:
    """Split what a pass captured by why it was admitted, for the audit line.

    A rebound fingerprint is an operator-approved declaration whose ``${VAR}``
    expansion changed, a casing-healed one an approved launch whose command the
    PATHEXT casing repair rewrote, a pre-fold one an approval stored before the
    PATH fold, a normalized one an approved launch whose only change is a
    version folder the edition normalizer folds, a trusted one a changed launch
    of a server the operator trusts;
    every other captured fingerprint is a managed launch. A fingerprint in
    *withheld* (stem -> fingerprints the save did not write) is left out.
    """
    by_reason: dict[str, dict[str, set[str]]] = {}
    for stem, fps in approvals.captured.items():
        rebound = set(approvals.rebound.get(stem, {}).values())
        for fingerprint in fps - withheld.get(stem, set()):
            reason = REBIND_AUDIT_REASON if fingerprint in rebound else "managed launch"
            if fingerprint in approvals.casing_healed.get(stem, {}):
                reason = CASING_AUDIT_REASON
            if fingerprint in approvals.prefold_healed.get(stem, {}):
                reason = PREFOLD_AUDIT_REASON
            if fingerprint in approvals.normalized_healed.get(stem, {}):
                reason = NORMALIZED_AUDIT_REASON
            if fingerprint in approvals.trust_admitted.get(stem, {}):
                reason = AUTO_AUDIT_REASON if approvals.auto else TRUST_AUDIT_REASON
            by_reason.setdefault(reason, {}).setdefault(stem, set()).add(fingerprint)
    return by_reason


def forget_unseen_trust(seen: LaunchApprovals, current: LaunchApprovals) -> bool:
    """Drop the trust choice of every server that a full pass did not find declared.

    A removed server keeps its name on the share list, so a server added again
    under that name would otherwise inherit the choice. A server still declared
    keeps it, whatever this pass did with its launch. Only a complete full pass
    can say a server is gone: a pass served from cache, or one that skipped an
    agent or the settings file for any reason but deletion, drops nothing. True when one was dropped.
    """
    if not seen.full_pass or seen.live_incomplete or seen.declared_incomplete:
        return False
    # Only choices the pass started from: a grant made while it ran is newer.
    gone = (current.trusted & seen.trusted) - seen.declared
    current.trusted -= gone
    return bool(gone)


def forget_trust(names: Iterable[str], path: Path | None = None) -> None:
    """Drop the trust choice of *names*: their sharing was turned off in the dashboard."""
    from kiro_crew.mcp_gateway import launch_approval as store

    stems = {store.target_stem(n) for n in names if isinstance(n, str) and n}
    target = path or store.approvals_path()
    with store._STORE_LOCK:
        problem = store.approvals_store_problem(target)
        if problem:
            raise store.ApprovalStoreUnreadable(f"{target.name}: {problem}")
        current = store._load_for_update(target)
        if not stems & current.trusted:
            return
        current.trusted -= stems
        store._write(target, store._document(current, current.stored_refused))


def note_trust_live(approvals: LaunchApprovals, stem: str, fingerprint: str) -> None:
    """Record a launch of a trusted server this pass found, however it is admitted."""
    from kiro_crew.mcp_gateway.launch_approval import _MAX_SERVER_LAUNCHES

    if not approvals.probe and approvals.trusts(stem):
        live = approvals.trust_live.get(stem, set())
        if not approvals._stem_admitted(stem, stem) or (
            len(live) >= _MAX_SERVER_LAUNCHES and fingerprint not in live
        ):
            approvals.live_incomplete = True
            return
        approvals.trust_live.setdefault(stem, set()).add(fingerprint)


def retire_trusted(
    approvals: LaunchApprovals,
    current: LaunchApprovals,
    stem: str,
    final_fps: set[str],
    final_pairs: set[str],
) -> tuple[set[str], set[str]]:
    """Drop the approved launches of a trusted server that a complete pass did not see.

    A trusted server's launches change freely, so without this every change
    would stay in the store and fill the per-server launch cap. Only what the
    pass started from is retired: an approval written while it ran is kept, and
    a pass that skipped a source or was served from cache retires nothing.
    """
    if (
        not (approvals.auto or stem in current.trusted)
        or not approvals.full_pass
        or approvals.live_incomplete
        or approvals.declared_incomplete
        or stem not in approvals.trust_live
    ):
        return final_fps, final_pairs
    live_pairs = set(approvals.launch_identities.get(stem, {})) | approvals.live_overflow.get(
        stem, set()
    )
    stale_fps = set(approvals.approved.get(stem, ())) - approvals.trust_live[stem]
    stale_pairs = set(approvals.approved_pairs.get(stem, ())) - live_pairs
    return final_fps - stale_fps, final_pairs - stale_pairs


def _pairs_under(pairs: Iterable[str], command_hash: str) -> set[str]:
    return {pair for pair in pairs if pair.partition(":")[0] == command_hash}


def settle_rebinds(
    approvals: LaunchApprovals, stem: str, current_pairs: set[str], before: set[str]
) -> tuple[set[str], set[str], set[str]]:
    """Which rebound pairs a save may write, retire, or leave untouched.

    A rebind is written only while the store under its command hash is exactly
    what the pass started from: an operator approval or another pass that
    landed meanwhile is newer, and wins. A rebind whose fingerprint is absent
    from the current store is dropped too. Where it is written, the pairs
    under that command hash become the ones this full pass saw live
    (``launch_identities``, plus the approved pairs admitted past its cap in
    ``live_overflow``), so a ``${VAR}`` that keeps changing replaces its older
    expansions instead of filling the per-server launch cap, whichever agents
    declare the launch.

    Retiring what the pass did not see needs the pass to have seen everything:
    a pass that kept an agent's overlay without reading its source
    (``live_incomplete``) never admitted that agent's launches, so its own
    still-approved pair is absent from ``launch_identities``. Such a pass
    writes the rebind and retires nothing; the stale expansions wait for a
    pass that admitted every live launch. A pass whose environment sidecar
    publication failed writes and retires no rebound pair.
    """
    if approvals.rebind_incomplete:
        return set(), set(), set()
    base = approvals.rebind_base.get(stem, frozenset())
    live = set(approvals.launch_identities.get(stem, {})) | approvals.live_overflow.get(stem, set())
    by_command: dict[str, set[str]] = {}
    for pair, fingerprint in approvals.rebound.get(stem, {}).items():
        if fingerprint in before:
            by_command.setdefault(pair.partition(":")[0], set()).add(pair)
    written: set[str] = set()
    retired: set[str] = set()
    untouched: set[str] = set()
    for command_hash, pairs in by_command.items():
        stored = _pairs_under(current_pairs, command_hash)
        if stored != _pairs_under(base, command_hash):
            untouched.add(command_hash)
            continue
        written |= pairs
        if approvals.full_pass and not approvals.live_incomplete:
            retired |= stored - (live & (stored | pairs))
    return written, retired, untouched
