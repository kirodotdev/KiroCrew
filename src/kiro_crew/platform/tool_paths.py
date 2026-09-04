"""Shared, bounded, depth-aware extraction of a tool call's target file paths.

This module is the SINGLE source of the traversal that both the sensitive-path
keystone in :mod:`kiro_crew.hooks` (hard-deny plane) and the governance
intersection plane in :mod:`kiro_crew.platform.governance` (permit-by-default
plane) rely on. It lives BELOW both on purpose: ``hooks`` imports FROM
``platform.governance``, so ``governance`` cannot import ``hooks`` without a
cycle, and both need the same walk. Keeping it here — and depending on nothing
but the stdlib and :mod:`collections.abc` — lets either caller import it with no
cycle and no heavy transitive dependency.

The two callers apply DIFFERENT fail semantics to the ``truncated`` flag (the
keystone hard-denies an unverifiable scan; governance denies only the scopes the
tool kind implies, per its permit-by-default contract), but the extraction
itself is identical and must not drift between them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: EVERY argument name a tool may carry its target file path under. Public because
#: it is shared with the consent prompt in ``cli_chat``: a prompt that disclosed a
#: path the gate did not inspect would let the two disagree about what the target
#: is, and the surface asking a human would be reading the weaker field. One tuple
#: is what makes that parity structural instead of a comment claiming it.
#:
#: The camel-case spelling is not hypothetical -- ``_SEARCH_DENY_ARG_KEYS`` has
#: accepted it for the search plane all along, while the sensitive-path keystone
#: below read only the two snake_case forms.
TARGET_PATH_KEYS: tuple[str, ...] = ("path", "file_path", "filePath")


#: Cap on the number of DISTINCT candidate paths collected below. The extractor
#: runs synchronously on the gateway event loop for every tool call, and each
#: collected path costs the keystone an ``is_sensitive_path`` resolution (two
#: symlink-following syscall chains) — so an attacker-shaped batch carrying tens
#: of thousands of paths could stall the loop. The cap does NOT fail open: hitting
#: it sets ``TargetPaths.truncated`` and the gate DENIES an unverifiable call
#: (same deny-by-default shape as the unrecoverable-shell-command branch).
#: Generous on purpose: no legitimate tool schema names hundreds of files in one
#: call, and a denied call merely falls to the human with a clear reason.
_TARGET_PATH_MAX_PATHS = 256

#: Budget of container nodes (dicts/lists) the walk will visit, bounding total
#: traversal work independently of how the paths are arranged. Exceeding it also
#: sets ``TargetPaths.truncated`` → deny. High enough that any real tool call is
#: orders of magnitude below it.
_TARGET_PATH_MAX_NODES = 10_000


class TargetPaths(list):
    """The collected paths, plus whether collection had to stop early.

    A ``list`` subclass so every existing consumer (iteration in the gate loops,
    ``found[0]`` in the consent prompt, truthiness, equality in tests) works
    unchanged. ``truncated`` is True when the walk hit ``_TARGET_PATH_MAX_PATHS``
    or ``_TARGET_PATH_MAX_NODES``, meaning the returned list may be INCOMPLETE —
    a security consumer must treat that as "the call could not be verified" and
    deny, never as "everything present was checked".

    ``unanchored`` is True when :func:`edit_target_candidates` was handed a diff
    content block path that is still relative after ``~``/env expansion. Such a
    path resolves against the PROCESS working directory — the gateway's, not the
    agent workspace's — so no gate can establish what file it actually names
    (a workspace symlink can point it at a protected file). A security consumer
    must deny on this flag exactly like ``truncated``: the target set could not
    be verified.
    """

    truncated: bool = False
    unanchored: bool = False


def target_paths(raw_params: Mapping | None) -> TargetPaths:
    """Every non-empty string path in *raw_params*, under any accepted spelling,
    at ANY nesting depth.

    Returns ALL of them rather than the first match, and callers deny if ANY is
    forbidden. That is deliberately different from "normalize the aliases onto one
    key and reject conflicts": a conflict rule has to decide which spelling wins,
    and picking wrong is how a sensitive path slips past. Checking every value
    present cannot be gamed by adding a second, innocent-looking alias, and needs
    no adjudication.

    Nesting is walked for the same ground-truth reason: a batch-shaped tool
    carries its real targets inside an array argument (e.g.
    ``{"operations": [{"mode": "Line", "path": …}]}``), so an extraction that
    read only the top-level keys never surfaced those paths to the
    sensitive-path keystone — the call was then evaluated as having no target
    at all and could auto-approve a read the flat spelling of the same path
    would have denied. The walk is ITERATIVE and EXHAUSTIVE — there is no depth
    bound that a sufficiently nested path could hide beyond, and no
    ``RecursionError`` can escape into the gate — visits every dict/list value,
    collects strings under ``TARGET_PATH_KEYS`` wherever they appear (including
    a list of strings directly under such a key), and stays extract-only: no
    sensitivity decision is made here, order of first appearance is preserved,
    and duplicates collapse (set-backed, so collection is linear). The only
    limits are the ``_TARGET_PATH_MAX_PATHS`` / ``_TARGET_PATH_MAX_NODES``
    work caps, and those fail CLOSED: the result is marked ``truncated`` and
    the gate denies the call as unverifiable rather than trusting a partial
    scan. Over-extraction is the safe direction, since callers deny on ANY hit
    and the consent prompt merely discloses more.
    """
    found = TargetPaths()
    if not isinstance(raw_params, Mapping):
        return found
    seen: set[str] = set()
    nodes = 0
    # Explicit LIFO stack, entries pushed in reverse so traversal matches
    # document order: at each mapping the accepted spellings are collected in
    # ``TARGET_PATH_KEYS`` order first (preserving the flat extraction's
    # historical ordering), then every value is walked in insertion order.
    stack: list[object] = [raw_params]
    while stack:
        if len(found) >= _TARGET_PATH_MAX_PATHS or nodes >= _TARGET_PATH_MAX_NODES:
            found.truncated = True
            return found
        node = stack.pop()
        nodes += 1
        if isinstance(node, Mapping):
            for key in TARGET_PATH_KEYS:
                _collect_path_strings(node.get(key), found, seen)
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, (list, tuple)):
            stack.extend(reversed(node))
    return found


def is_edit_call(tool_kind: str, diff_path: str = "") -> bool:
    """Whether a tool call is on the WRITE plane: it declared the ``edit`` kind,
    OR its tool_call frame carried a ``{"type": "diff"}`` content block naming a
    path (*diff_path*).

    The diff content block is the edit's target of record, and its PRESENCE is
    what routes a call onto the write plane — the ACP ``kind`` field is
    spec-optional, agent-influenced on permission frames, and can arrive empty
    or as ``read`` on a call whose content block declares a file change. The
    ``diff_path`` cache is written only when a tool_call frame's content
    includes a diff block with a nonempty path, so no legitimate non-edit call
    carries one. This is the routing predicate the hook edit gate and
    governance classification share, so those two planes cannot disagree on
    what counts as an edit; the always-enforced tier
    (``llm_helpers._resolve_permission``) composes the same two facts with its
    client-derived provenance flags before rerouting, so its route is this
    predicate NARROWED, never a different reading of what an edit is. The read
    allowance is keyed on the ABSENCE of a diff block: a read
    emits none, which is exactly what makes it a read.
    """
    return tool_kind == "edit" or bool(diff_path)


def edit_target_candidates(raw_params: Mapping | None, diff_path: str = "") -> TargetPaths:
    """The target set a file-EDIT tool call is judged by: the UNION of every
    accepted path spelling in *raw_params* (via :func:`target_paths`) and
    *diff_path*, the path the tool_call's ``{"type": "diff"}`` content block
    named.

    A backend may stream trusted params that carry no path key at all and name
    the file only in that block, so judging the params alone judges nothing.
    This is the SINGLE source of that union for BOTH edit gates — the
    always-enforced tier (``llm_helpers._edit_target_denial``) and the hook tier
    (``hooks.on_tool_call``'s edit branch) — so the two cannot drift apart on
    what counts as an edit's target. It lives here for the same layering reason
    as :func:`target_paths`: ``llm_helpers`` imports ``hooks``, so ``hooks``
    cannot import the helper from ``llm_helpers`` without a cycle.

    Extraction only, no sensitivity decision: the ``truncated`` flag is carried
    through from the walk, and a *diff_path* that is still relative after
    ``~``/env expansion sets ``unanchored`` instead of joining the set — the
    diff block's path is a verbatim backend field, and a relative one resolves
    against the gateway process CWD, so no consumer can verify what it names.
    Both consumers keep their HARD-DENY reading of either flag (an unverifiable
    target set is denied, never trusted). The empty-union verdict also stays
    with the consumers — an empty return here is the fact, the deny is theirs.
    """
    candidates = target_paths(raw_params)
    if candidates.truncated:
        # A truncated walk is already unverifiable and both consumers hard-deny
        # on the flag before iterating; appending past it would also break the
        # module contract that the work caps bound the returned set.
        return candidates
    if diff_path:
        expanded = os.path.expanduser(os.path.expandvars(diff_path))
        if not os.path.isabs(expanded):
            # Not appended: an unanchored path resolves against the process CWD,
            # so any sensitivity verdict computed from it would be about the
            # wrong file. The flag is the verdict-carrier; consumers deny on it.
            candidates.unanchored = True
            return candidates
        if diff_path not in candidates:
            candidates.append(diff_path)
    return candidates


def _collect_path_strings(value: object, found: TargetPaths, seen: set[str]) -> None:
    """Collect *value* (or its items, for a sequence) as candidate paths.

    Handles a string or an arbitrarily nested list/tuple of strings directly
    under an accepted key, iteratively. Non-string leaves are ignored — the
    generic walk in ``target_paths`` still descends into any mappings inside.
    """
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if item.strip() and item not in seen:
                if len(found) >= _TARGET_PATH_MAX_PATHS:
                    found.truncated = True
                    return
                seen.add(item)
                found.append(item)
        elif isinstance(item, (list, tuple)):
            pending.extend(reversed(item))
