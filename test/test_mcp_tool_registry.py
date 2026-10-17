"""The two halves of a tool must stay in sync: descriptor and handler.

A ``kirocrew-core`` tool is declared twice in the same domain module under
:mod:`kiro_crew.mcp_tools` -- a descriptor in ``schemas()`` (what ``tools/list``
advertises) and a function in ``HANDLERS`` (what runs). Nothing at runtime
notices when only one half lands: a descriptor with no handler advertises a tool
that answers with the dispatcher's fallthrough, and a handler with no descriptor
is unreachable because the model is never told the name.

``kirocrew-dashboard`` has no two halves: each tool is one row of
``mcp_dashboard.TABLE``, so the tests for it check what a row has to agree with
outside itself -- the validation registry, the gate the session-control rows are
named for, and the body's calling shape.

The last test here guards the seam that makes the core split safe. Handlers read this
server's plumbing as attributes of ``mcp_core`` -- ``mcp_core._post``,
``mcp_core.sel`` -- so that a test rebinding one still intercepts. That is an
attribute lookup resolved at call time, which no import checker validates: a
renamed or removed binding in ``mcp_core`` stays silent until the handler runs.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
from pathlib import Path

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_tools import DOMAIN_MODULES, build_tool_list, dispatch


def _domain(name: str):
    return importlib.import_module(f"kiro_crew.mcp_tools.{name}")


def _all_handlers() -> dict[str, object]:
    out: dict[str, object] = {}
    for domain in DOMAIN_MODULES:
        out.update(_domain(domain).HANDLERS)
    return out


def _all_declared() -> list[str]:
    """Every tool name a domain declares, enabled or not.

    ``build_tool_list()`` is the ADVERTISED list and may omit a declared tool whose
    app is switched off (``apps.advertised()``); the two halves this file holds in
    sync are the declaration and the handler, so parity is read from ``schemas()``.
    """
    return [t["name"] for domain in DOMAIN_MODULES for t in _domain(domain).schemas()]


def test_every_advertised_tool_has_a_handler() -> None:
    """A descriptor with no handler advertises a tool that cannot run."""
    advertised = {t["name"] for t in build_tool_list()}
    assert advertised - set(_all_handlers()) == set()


def test_every_handler_is_declared() -> None:
    """A handler with no descriptor is unreachable: the model never learns the name."""
    assert set(_all_handlers()) - set(_all_declared()) == set()


@pytest.mark.parametrize("domain", DOMAIN_MODULES)
def test_descriptor_and_handler_live_in_the_same_module(domain: str) -> None:
    """Splitting a tool across two domains is how the halves drift apart."""
    module = _domain(domain)
    assert {t["name"] for t in module.schemas()} == set(module.HANDLERS)


@pytest.mark.parametrize("domain", DOMAIN_MODULES)
def test_a_domain_advertises_only_what_it_declares(domain: str) -> None:
    """``advertised()`` may narrow ``schemas()``, never add to it or reshape a descriptor."""
    module = _domain(domain)
    advertised = getattr(module, "advertised", None)
    if advertised is None:
        return
    declared = {t["name"]: t for t in module.schemas()}
    for spec in advertised():
        assert declared[spec["name"]] == spec, spec["name"]


def test_tool_names_are_unique_across_domains() -> None:
    """Two domains claiming one name would make dispatch order decide the winner."""
    names = _all_declared()
    assert sorted(names) == sorted(set(names))


def test_legacy_monitor_descriptors_route_structured_watches_correctly() -> None:
    """Model-facing compatibility tools must not steal supported PR watches."""
    descriptors = {tool["name"]: tool["description"] for tool in _domain("control").schemas()}

    start = descriptors["monitor_start"].lower()
    assert "unsupported" in start
    assert "monitor_watch" in start
    assert "supported pull-request" in start

    stop = descriptors["autonudge_stop"].lower()
    assert "structured" in stop
    assert "durable" in stop
    assert "retain" in stop


@pytest.mark.parametrize("domain", DOMAIN_MODULES)
def test_descriptor_shape(domain: str) -> None:
    """kiro-cli drops a tool whose descriptor is missing any of the three keys."""
    descriptors = _domain(domain).schemas()
    assert descriptors, f"{domain} declares no tools"
    for spec in descriptors:
        assert set(spec) == {"name", "description", "inputSchema"}, spec.get("name")
        assert spec["name"] and isinstance(spec["name"], str)
        assert spec["description"].strip(), spec["name"]
        assert spec["inputSchema"]["type"] == "object", spec["name"]


@pytest.mark.parametrize("domain", DOMAIN_MODULES)
def test_handler_signature(domain: str) -> None:
    """The dispatcher calls every handler as ``handler(name, args)``."""
    for tool, fn in _domain(domain).HANDLERS.items():
        params = list(inspect.signature(fn).parameters)
        assert params == ["name", "args"], f"{tool} takes {params}"


#: Modules in the package that are not tool domains: the tool table and its
#: dashboard port. Every other module must be a registered domain.
_SUPPORT_MODULES = frozenset({"table", "dashboard_client"})


def test_domain_modules_covers_the_package() -> None:
    """A domain module absent from DOMAIN_MODULES is never advertised at all."""
    package = Path(mcp_core.__file__).parent / "mcp_tools"
    on_disk = {p.stem for p in package.glob("*.py") if not p.stem.startswith("_")}
    assert on_disk - _SUPPORT_MODULES == set(DOMAIN_MODULES)


@pytest.mark.parametrize("support", sorted(_SUPPORT_MODULES))
def test_a_support_module_declares_no_tools(support: str) -> None:
    """The exclusion above holds only while a support module carries no tool half."""
    module = importlib.import_module(f"kiro_crew.mcp_tools.{support}")
    assert not hasattr(module, "HANDLERS") and not hasattr(module, "schemas"), support
    assert support not in DOMAIN_MODULES


def test_the_package_import_stays_a_leaf() -> None:
    """``mcp_core`` imports this package at module scope, so importing it must
    not drag in a domain module or the tool table -- that is what keeps the
    cycle open. Checked in a clean interpreter, where nothing is preloaded."""
    import subprocess
    import sys

    probe = (
        "import sys, kiro_crew.mcp_tools as p\n"
        "print(sorted(m for m in sys.modules if m.startswith('kiro_crew.mcp_tools.')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, encoding="utf-8"
    ).stdout.strip()
    assert out == "['kiro_crew.mcp_tools._limits']" or out == "[]", out


def test_unknown_tool_falls_through() -> None:
    """An unrecognized name must report itself, not raise."""
    assert dispatch("no_such_tool", {}) == "Unknown tool: no_such_tool"


@pytest.mark.parametrize("tool_name", ("workflow_save", "workflow_update"))
def test_workflow_library_mutations_are_human_only(tool_name: str) -> None:
    """Untrusted model output cannot persist or replace a durable workflow."""
    assert tool_name not in {tool["name"] for tool in build_tool_list()}
    assert dispatch(tool_name, {}) == f"Unknown tool: {tool_name}"


# ── kirocrew-dashboard: one row per tool ────────────────────────────────────


def _dashboard_table():
    from kiro_crew.mcp_dashboard import TABLE

    return TABLE


def test_every_dashboard_tool_is_validated() -> None:
    """A row whose name is missing from the dashboard registry runs on raw args.

    ``ToolTable.validate`` passes an unregistered tool's arguments through as
    sent, so the registry and the table must name the same tools.
    """
    from kiro_crew.validation import MCP_DASHBOARD_SCHEMAS

    assert set(_dashboard_table().names()) == set(MCP_DASHBOARD_SCHEMAS)


def test_the_session_control_rows_are_the_strict_rows() -> None:
    """The gate and the containment list read one set, derived from the rows."""
    from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS

    table = _dashboard_table()
    assert SESSION_CONTROL_TOOLS == table.names("strict")
    assert {n for n in table.names() if n.startswith("session_")} == set(SESSION_CONTROL_TOOLS)


def test_every_dashboard_row_is_well_formed() -> None:
    """A descriptor kiro-cli drops, or a body the table cannot call, is a dead tool."""
    for tool in _dashboard_table():
        spec = tool.descriptor()
        assert set(spec) == {"name", "description", "inputSchema"}, tool.name
        assert spec["description"].strip(), tool.name
        assert spec["inputSchema"]["type"] == "object", tool.name
        assert list(inspect.signature(tool.run).parameters) == ["args", "ctx"], tool.name
        assert tool.identity in ("attribution", "strict"), tool.name
        for route in tool.routes:
            method, _, path = route.partition(" ")
            assert method in {"GET", "POST", "PATCH", "PUT", "DELETE"}, (tool.name, route)
            assert path.startswith("/api/") and "?" not in path, (tool.name, route)


def test_the_dashboard_advertises_its_rows_in_order() -> None:
    """``tools/list`` is the rows in order, titled; ``_tool_definitions`` is untitled."""
    from kiro_crew import mcp_dashboard
    from kiro_crew.mcp_tool_titles import with_titles

    table = _dashboard_table()
    assert [t["name"] for t in mcp_dashboard._list_tools()] == list(table.names())
    assert all("title" not in d for d in mcp_dashboard._tool_definitions())
    assert mcp_dashboard._list_tools() == with_titles(
        mcp_dashboard.SERVER_NAME, mcp_dashboard._tool_definitions()
    )


@pytest.mark.parametrize("domain", DOMAIN_MODULES)
def test_every_mcp_core_attribute_a_handler_reads_exists(domain: str) -> None:
    """Guards the late-binding seam against a rename in mcp_core.

    ``mcp_core.X`` is resolved when the handler runs, so neither flake8 nor mypy
    reports a binding that moved or vanished -- it surfaces as an AttributeError
    on a live tool call. This fails at collection time instead.
    """
    module = _domain(domain)
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    read: set[str] = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "mcp_core"
    }
    assert read, f"{domain} reads nothing from mcp_core; the seam moved"
    missing = sorted(a for a in read if not hasattr(mcp_core, a))
    assert missing == [], f"{domain} reads mcp_core.{{{','.join(missing)}}} which no longer exists"


@pytest.mark.parametrize("domain", DOMAIN_MODULES)
def test_no_unresolvable_free_names(domain: str) -> None:
    """Every name a handler loads must resolve, or the tool dies at runtime.

    A handler body was moved out of ``mcp_core``, so a name that was a module
    global there and did not get rewritten resolves to nothing here. Static
    imports catch most of it; this catches the rest without executing handlers.
    """
    module = _domain(domain)
    path = Path(module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_names = set(vars(module)) | set(dir(builtins))

    unresolved: dict[str, set[str]] = {}
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(node.name)
                a = node.args
                bound.update(p.arg for p in a.args + a.kwonlyargs + a.posonlyargs)
                if a.vararg:
                    bound.add(a.vararg.arg)
                if a.kwarg:
                    bound.add(a.kwarg.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update((al.asname or al.name).split(".")[0] for al in node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                bound.update(t.id for t in ast.walk(node.optional_vars) if isinstance(t, ast.Name))
            elif isinstance(node, ast.comprehension):
                bound.update(t.id for t in ast.walk(node.target) if isinstance(t, ast.Name))
        loaded = {
            node.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        gap = loaded - bound - module_names
        if gap:
            unresolved[fn.name] = gap

    assert unresolved == {}, f"{domain}: unresolvable names {unresolved}"
