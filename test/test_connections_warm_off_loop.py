"""Warm-mint tests: no coroutine in the warm engine touches the filesystem directly.

The fixed-point drift guard ``connections/warm.py`` and the connections spec cite: it reads
``warm.py`` and every ``warm_runtime`` owner as one call graph, reusing the mint engine's own
primitive sets so the two guards cannot drift apart, and pins the exact set of helpers doing
filesystem work so a coroutine can neither call one nor quietly drop the work the guard rests on.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import connections_warm_helpers
from test_connections_mint import _FS_ATTRS, _FS_NAMES, _called_names

from kiro_crew.connections import warm

# The shared fixtures, bound here by name so pytest registers them for this module.
_clean_mint_table = connections_warm_helpers._clean_mint_table
_clean_warm_runtime = connections_warm_helpers._clean_warm_runtime


# ── the loop/filesystem invariant (mirrors the mint engine's own guard) ──
#
# Reuses the mint guard's primitive sets so the two cannot drift apart, plus the names that
# reach the filesystem only from THIS module: the MCP inventory read, the grant stat, and the
# credential gate -- which consults the operator's on-disk OAuth-endpoint extension for any
# host outside the builtin set, so it is an unbounded stat like the rest.
_WARM_FS_NAMES = _FS_NAMES | {"list_servers", "grant_present", "oauth_url_contains_credential"}


def _warm_engine_trees() -> list[tuple[str, ast.Module]]:
    """The facade plus every module of its private ``warm_runtime`` package.

    One call graph, because a helper the facade re-exports is still called from the facade's
    coroutines, and a coroutine that moved into an owner still owes the same invariant. Reading
    the facade alone would silently drop both.
    """
    from kiro_crew.connections import warm_runtime

    owners = sorted(Path(warm_runtime.__file__).parent.glob("*.py"))
    assert len(owners) > 1, "warm_runtime moved; this guard is reading the wrong tree"
    return [
        (path.name, ast.parse(path.read_text(encoding="utf-8")))
        for path in [Path(warm.__file__), *owners]
    ]


def test_no_coroutine_in_the_warm_module_touches_the_filesystem_directly():
    sync: dict[str, list[Any]] = {}
    coros: list[tuple[str, Any]] = []
    for label, tree in _warm_engine_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                sync.setdefault(node.name, []).append(node)
            elif isinstance(node, ast.AsyncFunctionDef):
                coros.append((f"{label}:{node.name}", node))
    assert sync and coros, "module shape changed; this guard is reading the wrong tree"

    touches = {
        name: any(_called_names(node) & (_FS_ATTRS | _WARM_FS_NAMES) for node in nodes)
        for name, nodes in sync.items()
    }
    changed = True
    while changed:
        changed = False
        for name, nodes in sync.items():
            if touches[name]:
                continue
            if any(touches.get(callee) for node in nodes for callee in _called_names(node)):
                touches[name] = changed = True
    fs_helpers = {name for name, hit in touches.items() if hit}
    # The known set, so a helper silently losing its filesystem work -- and with it
    # this guard's coverage -- is visible rather than a quietly weaker test.
    # `_audited_mintable_providers` couples that filesystem scan to its SEL event.
    assert fs_helpers == {
        "_log_warm_event",
        "_disabled_provider_slugs",
        "warm_spec_providers",
        "_warm_activation_candidates",
        "_warm_candidate_scan",
        "_current_warm_redrain_entry",
        "mintable_providers",
        "_audited_mintable_providers",
        "_warm_spec_plan",
        "_private_warm_spec_work_dir",
        "_read_warm_spec_body",
        "_warm_spec_is_foreign",
        "_warm_work_dir",
        "_warm_generations_dir",
        "_write_warm_generation_owner",
        "_read_warm_generation_owner",
        "_is_plain_warm_generation_dir",
        "_remove_warm_generation_dir",
        "_create_warm_generation_dir",
        "_bind_warm_generation",
        "_recorded_runtime_is_dead",
        "_spawn_left_no_process",
        "_release_runtime_generation",
        "_scavenge_warm_generation_dirs",
        "_unowned_plan_specs",
        "_write_warm_mint_specs",
        "_remove_warm_mint_specs",
        "scavenge_warm_mint_artifacts",
        "_credential_bearing_slugs",
    }

    offenders = {
        f"{coro} -> {callee}"
        for coro, node in coros
        for callee in _called_names(node) & (fs_helpers | _FS_ATTRS | _WARM_FS_NAMES)
    }
    assert not offenders, (
        "filesystem work on the event loop: "
        + ", ".join(sorted(offenders))
        + " -- route it through asyncio.to_thread"
    )
