"""The pinned apps that supply the input of a core security limit only a wheel copy reads.

A pin row can outlive its wheel copy (app-kit-platform.md § 33, "A pin row with no
wheel copy"), so the tests that read a wheel copy skip a pinned app that has none.
Some core limits read their input from the wheel copy alone (the § 33 table), so
deleting the copy of an app that supplies one drops that limit for the app, unless the
limit can read the input from the app's pin row instead: the release that deletes the
copy freezes it there (``deletedCopy``, :data:`FROZEN`). :func:`limit_sources` derives,
per limit, which pinned apps supply its input today, from the wheel copy as the core
readers read it or, for a limit in :data:`FROZEN`, from the row's frozen copy, so that
set can be held by name: a deleted copy whose row does not carry the input drops its app
from a set and fails the test that pins it.

Read straight from the tree and the pin file, never through the readers' process-wide
caches, which a test earlier in the same worker may have filled from a fixture tree
(the masks' reader caches per file and its identity, so a fixture cannot answer for a
shipped file). No app is named here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import kiro_crew.apps.builtins as builtins_pkg
from kiro_crew.apps import agent_readonly_dirs, builtin_host_grants, credential_masks, seams
from kiro_crew.apps.builtin_adoption_baseline import freeze_deleted_copy
from kiro_crew.apps.builtin_adoption_pins import DELETED_COPY_KEY, parse_adoption_pins

_BUILTINS = Path(builtins_pkg.__file__).resolve().parent
_PINS = _BUILTINS.parent / "adoption-pins.json"

#: Each limit, with the § 33 table rows it is, as the test's failure names it.
MCP_SERVERS = "the stdio server ceiling and own-server grant (mcpServers, § 33 rows 1-2)"
AGENT_TOOLS = "the agentTools ceiling and the app-own-tools gate row (§ 33 rows 3-4)"
SEAMS = "the built-in seam providers (seams.py, § 33 row 5)"
HOST_GRANTS = "the relay credential refusal and strict internal paths (host_grants.py, § 33 row 6)"
DECLARED_DIRS = "the built-in credential masks and agent read-only dirs (§ 33 row 9)"

#: The limits whose input a pin row's ``deletedCopy`` carries once the copy is deleted.
FROZEN = frozenset({MCP_SERVERS, AGENT_TOOLS, DECLARED_DIRS})

#: The one seam a built-in provides that is no limit (§ 33 row 10).
_NOT_A_LIMIT = frozenset({seams.SEAM_STOPPED_LOOP_TOMBSTONE})


def wheel_copy(app: str) -> Path:
    """Where *app*'s wheel copy sits, whether or not it still ships."""
    return _BUILTINS / app.replace("-", "_")


def _host_grant_apps() -> set[str]:
    """``builtin_host_grants`` read afresh: its cache may hold a fixture tree's grants."""
    return {
        grants.app
        for grants in builtin_host_grants.builtin_host_grants.__wrapped__()
        if grants.relay_credential_paths or grants.strict_internal_paths
    }


def _registers_a_limit(path: Path) -> bool:
    """Whether the ``seams.py`` at *path* registers any seam but :data:`_NOT_A_LIMIT`.

    A registration this cannot resolve to a seam name counts, so it holds the copy.
    """
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "register_worker_slot_mode":
            return True
        if node.func.attr != "register":
            continue
        first = node.args[0] if node.args else None
        seam = getattr(seams, first.attr, None) if isinstance(first, ast.Attribute) else None
        if seam not in _NOT_A_LIMIT:
            return True
    return False


def _seam_apps() -> set[str]:
    """The pinned packages ``seams._load_builtin_seams`` imports a limit from."""
    found = set()
    for package in builtins_pkg.BUILTIN_NAMES:
        path = _BUILTINS / package / f"{seams.BUILTIN_SEAMS_MODULE}.py"
        if path.is_file() and _registers_a_limit(path):
            found.add(package.replace("_", "-"))
    return found


def _shipped_rows() -> dict[str, dict[str, Any]]:
    """The shipped pin table's valid rows by name, read from the file, as written."""
    document = json.loads(_PINS.read_text(encoding="utf-8"))
    valid = parse_adoption_pins(document)
    return {row["name"]: row for row in document["pins"] if row["name"] in valid}


def shipped_pin_names() -> frozenset[str]:
    """The shipped pin table's names, read from the file: a test may have swapped the loaded one."""
    return frozenset(_shipped_rows())


def limit_sources() -> dict[str, frozenset[str]]:
    """``{limit: the pinned apps that supply its input}``, for every limit.

    An app whose wheel copy ships supplies what ``freeze_deleted_copy`` reads from that
    copy, the readers' own inputs; one whose copy is gone supplies only a limit in
    :data:`FROZEN`, and only when its row's ``deletedCopy`` carries that input.
    """
    rows = _shipped_rows()
    pinned = frozenset(rows)
    shipped = {app for app in pinned if wheel_copy_ships(app)}
    inputs = {
        app: (
            freeze_deleted_copy(wheel_copy(app))
            if app in shipped
            else rows[app].get(DELETED_COPY_KEY) or {}
        )
        for app in pinned
    }

    def declaring(*keys: str) -> frozenset[str]:
        return frozenset(app for app, frozen in inputs.items() if any(frozen.get(k) for k in keys))

    return {
        MCP_SERVERS: declaring("mcpServers"),
        AGENT_TOOLS: declaring("agentTools"),
        SEAMS: frozenset(_seam_apps() & pinned),
        HOST_GRANTS: frozenset(_host_grant_apps() & pinned),
        DECLARED_DIRS: declaring(credential_masks.MANIFEST_KEY, agent_readonly_dirs.MANIFEST_KEY),
    }


def wheel_copy_ships(app: str) -> bool:
    """The manifest, not the directory: a deleted copy can leave a ``__pycache__`` behind."""
    return (wheel_copy(app) / "app.json").is_file()
