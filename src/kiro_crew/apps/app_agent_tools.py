"""The agent tools first-party apps declare, read once per manifest change, for every reader.

An app's manifest ``agentTools`` block (``apps/manifest_agent_tools.py``) declares MCP
tools the app serves from its own process. Three core readers build on this module, and
none of them names an app:

- the ``kirocrew-core`` MCP server lists each declared tool (``mcp_tools/app_tools.py``)
  and validates a call's arguments against the declared schema, through the same field
  engine every core tool uses (:func:`build_tool_schema`);
- that server forwards a validated call to the ONE gateway route
  ``POST /api/app-tools/call``, a strict internal path;
- the gateway handler (``dashboard/handlers/app_agent_tools.py``) checks the call again
  against the declaration and hands it to the app's own declared route.

Trust. A declaration is data the core checks; no app code is imported here or in the
MCP server. Only a FIRST-PARTY app's block is read: a name in
``execution.builtin_app_names()`` (a shipped manifest declares it AND the builtin owns
the installed slot), with the block read from that shipped ``app.json``, never from the
data-home copy. The tools are listed under ``kirocrew-core``, which the default agent
auto-approves as a whole server, so a tool listed there skips the permission prompt; a
third-party app's tools have no such grant, and an installed or registry app's block is
therefore ignored here until a listing path exists that asks before each call. Such an
app can still ship its own ``mcpServers`` entry, which is approved per call.

The reader keeps a tool only when the app's whole block parsed cleanly, its name is in
the app's namespace, and no other app declares the same name. A name a core tool
already has is never the app's: the MCP domain (``mcp_tools/app_tools.py``) drops it,
so the core tool keeps its handler and schema. The deny universe of an app agent
(:func:`shipped_tools`) names every tool a shipped first-party manifest declares, whether
its app is installed, switched off or neither, so a scoped agent is never handed a tool
by an install that happens after it was materialized; the gateway refuses a call to an
app that is not enabled.

The scan stats ``<apps dir>/<name>/app.json`` for every installed app and caches the
result on each file's ``(mtime_ns, size)`` and on the first-party set, so a
``tools/list`` costs one stat per app plus one read of the shipped manifests.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.apps.manifest import (
    MAX_ARRAY_ITEMS,
    MAX_STRING_ARGUMENT_CHARS,
    AgentToolDecl,
    AppManifest,
)

if TYPE_CHECKING:
    from kiro_crew.validation_parts.field_engine import ToolSchema

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeclaredTool:
    """One tool a first-party app declares, with the app and route that serve it."""

    app: str
    route: str
    decl: AgentToolDecl

    @property
    def name(self) -> str:
        return self.decl.name


_cache_lock = threading.Lock()
_cache: tuple[tuple[Any, ...], tuple[DeclaredTool, ...]] | None = None


def _fingerprint(root: str) -> tuple[Any, ...]:
    rows: list[tuple[str, int, int]] = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return ()
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            st = os.stat(os.path.join(entry.path, "app.json"))
        except OSError:
            continue
        rows.append((entry.name, st.st_mtime_ns, st.st_size))
    return (root, *rows)


def _first_party_apps() -> frozenset[str]:
    """The apps whose declarations may be listed: see the module docstring's Trust."""
    from kiro_crew.apps.execution import builtin_app_names

    try:
        return builtin_app_names()
    except Exception:  # noqa: BLE001 - an unreadable provenance trusts no app
        logger.warning("first-party app names unreadable; no app agent tool is listed")
        return frozenset()


def _first_party_manifest(name: str) -> AppManifest | None:
    """*name*'s SHIPPED manifest, or ``None`` when no shipped copy serves it now."""
    from kiro_crew.apps.execution import shipped_builtin_app_root

    root = shipped_builtin_app_root(name)
    return None if root is None else _read_shipped_manifest(name, root)


def _read_shipped_manifest(name: str, root: Path) -> AppManifest | None:
    try:
        return AppManifest.from_json_file(root / "app.json")
    except Exception:  # noqa: BLE001 - an unreadable shipped manifest declares nothing
        logger.warning("app %s: shipped manifest unreadable; agentTools ignored", name)
        return None


def _shipped_first_party_roots() -> dict[str, Path]:
    """Every app a shipped manifest declares, installed or not, by name: the deny universe's apps.

    Read with no release check: that check reads config, and a released app's name only
    stays withheld.
    """
    from kiro_crew.apps.execution import _shipped_builtin_apps

    roots: dict[str, Path] = {}
    for root, name in _shipped_builtin_apps():
        roots.setdefault(name, root)
    return roots


def _collect(
    names: Iterable[str],
    *,
    drop_clashes: bool = True,
    read: Callable[[str], AppManifest | None] | None = None,
) -> tuple[DeclaredTool, ...]:
    # Looked up per call, not bound as the default, so the module global stays the seam.
    reader = _first_party_manifest if read is None else read
    found: list[DeclaredTool] = []
    for name in names:
        manifest = reader(name)
        if manifest is None or manifest.name != name:
            continue
        block = manifest.agentTools
        if not block.tools:
            continue
        errors = block.validate(name)
        if errors:
            logger.warning("app %s: agentTools ignored: %s", name, "; ".join(errors))
            continue
        found.extend(DeclaredTool(app=name, route=block.route, decl=t) for t in block.tools)
    if not drop_clashes:
        # The deny universe keeps a clashing name once: naming it there only withholds.
        first: dict[str, DeclaredTool] = {}
        for tool in found:
            first.setdefault(tool.name, tool)
        return tuple(first.values())
    counts: dict[str, int] = {}
    for tool in found:
        counts[tool.name] = counts.get(tool.name, 0) + 1
    clashes = sorted(n for n, c in counts.items() if c > 1)
    if clashes:
        # Neither app gets the name: picking one would make the answer depend on order.
        logger.warning("agent tools declared by more than one app are ignored: %s", clashes)
    return tuple(t for t in found if counts[t.name] == 1)


def _scan(rows: tuple[Any, ...], trusted: frozenset[str]) -> tuple[DeclaredTool, ...]:
    return _collect(name for name, _mtime, _size in rows[1:] if name in trusted)


def declared_tools() -> tuple[DeclaredTool, ...]:
    """Every tool a first-party app declares, enabled or not, in app-name order."""
    global _cache
    from kiro_crew.apps.manager import apps_dir

    rows = _fingerprint(str(apps_dir()))
    trusted = _first_party_apps() if rows else frozenset()
    key = (rows, trusted)
    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
    tools = _scan(rows, trusted) if rows else ()
    with _cache_lock:
        _cache = (key, tools)
    return tools


@functools.lru_cache(maxsize=1)
def shipped_tools() -> tuple[DeclaredTool, ...]:
    """Every tool a shipped first-party manifest declares, whether or not its app is installed.

    This is the set an app agent's deny universe must name. The agent's
    ``managedToolPolicy.exclude`` is computed once, when the agent is materialized
    (``apps.permissions.managed_tool_exclusion``), and installing a first-party app
    afterwards does not materialize it again. So a tool must be in the universe before
    its app is installed, or a scoped agent could call it once the app is. Naming a tool
    here only ever withholds it; :func:`declared_tools` still decides what is listed and
    called. Both read the shipped ``app.json``, so a shipped manifest that cannot be read
    is neither named here nor listed there.

    Read once per process, and without the release check :func:`_first_party_manifest`
    makes: that check reads config, which a names-only tool listing never does, and an
    app released by adoption only leaves a name that is still withheld.
    """
    roots = _shipped_first_party_roots()
    return _collect(
        sorted(roots),
        drop_clashes=False,
        read=lambda name: _read_shipped_manifest(name, roots[name]),
    )


def declared_tool(name: str) -> DeclaredTool | None:
    """The declared tool called *name*, or ``None``."""
    for tool in declared_tools():
        if tool.name == name:
            return tool
    return None


def build_tool_schema(tool: DeclaredTool) -> ToolSchema:
    """The field-engine schema of *tool*: the same checks a core tool's arguments get.

    String arguments are sanitized and bounded (``maxLength``, else
    :data:`MAX_STRING_ARGUMENT_CHARS`) and an ``enum`` is enforced; numbers and
    integers reject a bool and honour ``minimum`` / ``maximum``; an array checks its
    item type (an integer item rejects a bool too), item length and ``maxItems``, else
    :data:`MAX_ARRAY_ITEMS`. An argument the schema does not declare is refused, as for
    every core tool.
    """
    from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError

    schema = tool.decl.input_schema
    required = set(schema.get("required", ()))
    specs: list[FieldSpec] = []
    int_arrays: list[str] = []
    for arg, prop in schema.get("properties", {}).items():
        kind = prop["type"]
        if kind == "string":
            enum = prop.get("enum")
            specs.append(
                FieldSpec(
                    arg,
                    str,
                    required=arg in required,
                    max_len=prop.get("maxLength", MAX_STRING_ARGUMENT_CHARS),
                    allowed=frozenset(enum) if enum else None,
                )
            )
        elif kind in ("integer", "number"):
            specs.append(
                FieldSpec(
                    arg,
                    int if kind == "integer" else (int, float),
                    required=arg in required,
                    min_val=prop.get("minimum"),
                    max_val=prop.get("maximum"),
                )
            )
        elif kind == "boolean":
            specs.append(FieldSpec(arg, bool, required=arg in required))
        else:
            items = prop["items"]
            item_is_str = items["type"] == "string"
            if not item_is_str:
                int_arrays.append(arg)
            specs.append(
                FieldSpec(
                    arg,
                    list,
                    required=arg in required,
                    item_type=str if item_is_str else int,
                    item_max_len=(
                        items.get("maxLength", MAX_STRING_ARGUMENT_CHARS) if item_is_str else 0
                    ),
                    max_items=prop.get("maxItems", MAX_ARRAY_ITEMS),
                )
            )

    def _no_bool_items(cleaned: dict[str, Any]) -> None:
        # The field engine checks an item with ``isinstance(item, int)``, which a bool
        # passes; a top-level integer refuses one, and so does an integer item here.
        for arg in int_arrays:
            for i, item in enumerate(cleaned.get(arg) or ()):
                if isinstance(item, bool):
                    raise ValidationError(arg, f"item[{i}]: expected int, got bool")

    return ToolSchema(
        tool_name=tool.name,
        fields=specs,
        custom_validator=_no_bool_items if int_arrays else None,
    )
