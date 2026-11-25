"""The pinned apps whose wheel copy is the only input of a core security limit.

A pin row can outlive its wheel copy (app-kit-platform.md § 33, "A pin row with no
wheel copy"), so the tests that read a wheel copy skip a pinned app that has none.
Some core limits still read their input from the wheel copy alone (the § 33 table),
so deleting the copy of an app that supplies one drops that limit for the app.
:func:`wheel_only_limit_sources` derives, per limit, which pinned apps' copies supply
it today, reading the files and keys the core readers read, so that set can be held by
name: a deleted copy drops its app from a set and fails the test that pins it.

Read straight from the tree, never through the readers' process caches, which a test
earlier in the same worker may have filled from a fixture tree. No app is named here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import kiro_crew.apps.builtins as builtins_pkg
from kiro_crew.apps import agent_readonly_dirs, builtin_host_grants, credential_masks, seams
from kiro_crew.apps.builtin_adoption_pins import parse_adoption_pins

_BUILTINS = Path(builtins_pkg.__file__).resolve().parent
_PINS = _BUILTINS.parent / "adoption-pins.json"

#: Each limit, with the § 33 table rows it is, as the test's failure names it.
MCP_SERVERS = "the stdio server ceiling and own-server grant (mcpServers, § 33 rows 1-2)"
AGENT_TOOLS = "the agentTools ceiling and the app-own-tools gate row (§ 33 rows 3-4)"
SEAMS = "the built-in seam providers (seams.py, § 33 row 5)"
HOST_GRANTS = "the relay credential refusal and strict internal paths (host_grants.py, § 33 row 6)"
DECLARED_DIRS = "the built-in credential masks and agent read-only dirs (§ 33 row 9)"

#: The one seam a built-in provides that is no limit (§ 33 row 10).
_NOT_A_LIMIT = frozenset({seams.SEAM_STOPPED_LOOP_TOMBSTONE})


def wheel_copy(app: str) -> Path:
    """Where *app*'s wheel copy sits, whether or not it still ships."""
    return _BUILTINS / app.replace("-", "_")


def _manifest(root: Path) -> dict:
    path = root / "app.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


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


def shipped_pin_names() -> frozenset[str]:
    """The shipped pin table's names, read from the file: a test may have swapped the loaded one."""
    return frozenset(parse_adoption_pins(json.loads(_PINS.read_text(encoding="utf-8"))))


def wheel_only_limit_sources() -> dict[str, frozenset[str]]:
    """``{limit: the pinned apps whose wheel copy supplies its input}``, for every limit."""
    pinned = shipped_pin_names()
    manifests = {app: _manifest(wheel_copy(app)) for app in pinned}

    def declaring(*keys: str) -> frozenset[str]:
        return frozenset(app for app, m in manifests.items() if any(m.get(k) for k in keys))

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
