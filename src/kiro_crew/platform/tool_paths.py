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
    """

    truncated: bool = False


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
