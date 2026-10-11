"""The list of paths by which an agent spec becomes the one a session runs.

Two admission gates decide whether a session may start on an agent spec:
``require_fork_governance`` (a private template copy may not start on grants its
ceiling has since tightened away, nor from a project copy that shadows it) and
``require_fresh_derived_spec`` (the derived worker spec must match the default it
mirrors, and must not be shadowed by the checkout). Their reach is disjoint and
neither is a superset of the other. The tests beside each gate prove that it WORKS
where it is called; nothing else records WHERE it is called, so a new start path
that calls neither is invisible until a reviewer happens to find it.

A third check, ``managed_agent_shadow_refusal``, refuses a kiro-cli start whose
managed agent name the checkout claims. It matters only where kiro-cli resolves
``--agent`` itself, so it is pinned per ``--agent`` site rather than per admission
site.

This module is that record, as pinned tables read off the source tree by AST:

* ``ADMISSION_SITES`` -- every function that references either gate, and which
  gates it references. A reference counts whether it is a call, the function handed
  on as a value (``asyncio.to_thread(require_fork_governance, ...)``, the form a
  symbol grep for ``require_fork_governance(`` misses), a name the gate was imported
  under, or the gate's name as the attribute string of a ``getattr``.
* ``KNOWN_GAPS`` -- per admission site, the gate it does not reference and why that
  is or is not covered elsewhere. Derived from ``ADMISSION_SITES`` and pinned
  separately, so closing a gap, or opening one, is a stated change.
* ``AGENT_ARGV_SITES`` -- every function that writes ``--agent`` (the bare literal,
  or a string or f-string beginning ``--agent=``) into source, which is how a
  kiro-cli child is told which spec to resolve. A literal handed to an argparse
  ``add_argument`` call declares this program's own option and is not a site. Each
  entry names the admission sites that govern it, or says plainly that none does
  and why. A literal outside any function is keyed by the empty function name.

Every table is compared for EXACT equality with what the tree contains. Adding a
start path that references a gate or writes ``--agent``, adding or dropping a gate
reference, or closing a gap fails here until the table is edited, so the edit and
the reason for it land in the same diff as the code. A failing assertion is the
prompt to classify the site, not a bug in this test.

What it cannot see: a spawn through a harness that selects its spec without
``--agent`` and references neither gate leaves nothing for the scan to find. The
``KNOWN_GAPS`` entry for ``AcpRuntime._resolve_spawn_plan`` records that those
harnesses reach no fork-governance check today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

FORK = "require_fork_governance"
FRESH = "require_fresh_derived_spec"
SHADOW = "managed_agent_shadow_refusal"
GATES = frozenset({FORK, FRESH})
SCANNED = GATES | {SHADOW}
AGENT_FLAG = "--agent"

Site = tuple[str, str]

# (module path under src/kiro_crew, dotted enclosing function) -> gates referenced.
ADMISSION_SITES: dict[Site, frozenset[str]] = {
    ("acp/client.py", "AcpClient._spawn"): frozenset({FORK, FRESH}),
    ("acp/harness/kas.py", "KasHarness.session_extras._build"): frozenset({FORK, FRESH}),
    ("acp/harness/kiro.py", "KiroHarness.resolve_spawn"): frozenset({FORK}),
    ("acp/runtime.py", "AcpRuntime._resolve_spawn_plan"): frozenset({FRESH}),
    ("acp/runtime.py", "AcpRuntime._activate_mode_bracketed"): frozenset({FRESH}),
    ("acp/session_mcp.py", "_agent_spec_and_snapshot_for"): frozenset({FRESH}),
}

# Per admission site: (gates it does not reference, whether something else covers it).
KNOWN_GAPS: dict[Site, tuple[frozenset[str], str]] = {
    ("acp/harness/kiro.py", "KiroHarness.resolve_spawn"): (
        frozenset({FRESH}),
        "covered: AcpRuntime._resolve_spawn_plan runs the freshness gate after this "
        "method returns, as the last check before the process is created",
    ),
    ("acp/runtime.py", "AcpRuntime._resolve_spawn_plan"): (
        frozenset({FORK}),
        "covered for the kiro host only, by KiroHarness.resolve_spawn; every other "
        "harness this runtime drives reaches no fork-governance check",
    ),
    ("acp/runtime.py", "AcpRuntime._activate_mode_bracketed"): (
        frozenset({FORK}),
        "UNCOVERED: a shared runtime spawned as one agent and switched to another "
        "here passes no fork-governance check for the agent it switched to; a "
        "project copy of a managed name is refused at the spawn, whichever agent "
        "the spawn names",
    ),
    ("acp/session_mcp.py", "_agent_spec_and_snapshot_for"): (
        frozenset({FORK}),
        "not a start path: it resolves the spec whose MCP servers a session mounts, "
        "and the spawn that consumes it runs its own admission",
    ),
}

# (module, function) -> the admission sites that govern the spawn it builds, or a
# string stating why no admission gate applies.
AGENT_ARGV_SITES: dict[Site, tuple[Site, ...] | str] = {
    ("acp/client.py", "AcpClient._spawn"): (("acp/client.py", "AcpClient._spawn"),),
    ("acp/harness/kiro.py", "KiroHarness.resolve_spawn"): (
        ("acp/harness/kiro.py", "KiroHarness.resolve_spawn"),
        ("acp/runtime.py", "AcpRuntime._resolve_spawn_plan"),
    ),
    # Rewrites the agent in the argv of the plan ``_resolve_spawn_plan`` returned.
    ("acp/runtime.py", "AcpRuntime._spawn_admitted"): (
        ("acp/harness/kiro.py", "KiroHarness.resolve_spawn"),
        ("acp/runtime.py", "AcpRuntime._resolve_spawn_plan"),
    ),
    ("dashboard/handlers/sessions.py", "_usage_scrape_argv"): (
        "UNGATED: a one-shot `/usage` read on the fixed kirocrew-lite service agent, "
        "spawned with no cwd argument, so the child resolves --agent against the "
        "gateway process's own working directory rather than a session checkout"
    ),
    ("mcp_gateway/rewriter.py", "_build_stub_entry"): (
        "not a kiro-cli spawn: the agent name is an argument to the MCP gateway stub"
    ),
    ("testing/workflow_memory_scenario.py", "respond"): (
        "not a kiro-cli spawn: a test scenario reading its own argv"
    ),
}

# Functions that reference the managed-shadow refusal. Every governed ``--agent``
# site must name one of them among its governors (see the last test below).
SHADOW_SITES: frozenset[Site] = frozenset(
    {
        ("acp/client.py", "AcpClient._spawn"),
        ("acp/harness/kiro.py", "KiroHarness.resolve_spawn"),
    }
)

_NEEDLES = (FORK, FRESH, SHADOW, AGENT_FLAG)


def _is_agent_flag(value: object) -> bool:
    return isinstance(value, str) and (value == AGENT_FLAG or value.startswith(AGENT_FLAG + "="))


def _gate_aliases(tree: ast.AST) -> dict[str, str]:
    """Local names a module imported a gate under, mapped to the gate."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in SCANNED:
                    aliases[alias.asname or alias.name] = alias.name
    return aliases


def _argparse_flag_literals(tree: ast.AST) -> set[int]:
    """``id`` of every constant passed positionally to an ``add_argument`` call."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            ids.update(id(arg) for arg in node.args if isinstance(arg, ast.Constant))
    return ids


def _scan(root: Path) -> tuple[dict[Site, set[str]], set[Site]]:
    """Every gate reference and every ``--agent`` literal, keyed by enclosing function."""
    gate_refs: dict[Site, set[str]] = {}
    argv_sites: set[Site] = set()
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(needle in text for needle in _NEEDLES):
            continue
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(text, filename=str(path))
        names = {gate: gate for gate in SCANNED} | _gate_aliases(tree)
        argparse_ids = _argparse_flag_literals(tree)

        def walk(node: ast.AST, stack: list[str], rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(child, [*stack, child.name])
                    continue
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    continue
                key = (rel, ".".join(stack))
                gate = None
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    gate = names.get(child.id)
                elif (
                    isinstance(child, ast.Attribute)
                    and isinstance(child.ctx, ast.Load)
                    and child.attr in SCANNED
                ):
                    gate = child.attr
                elif (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "getattr"
                    and len(child.args) >= 2
                    and isinstance(child.args[1], ast.Constant)
                    and child.args[1].value in SCANNED
                ):
                    gate = child.args[1].value
                if gate is not None:
                    gate_refs.setdefault(key, set()).add(gate)
                if isinstance(child, ast.Constant) and id(child) not in argparse_ids:
                    if _is_agent_flag(child.value):
                        argv_sites.add(key)
                if isinstance(child, ast.JoinedStr) and child.values:
                    head = child.values[0]
                    if isinstance(head, ast.Constant) and _is_agent_flag(head.value):
                        argv_sites.add(key)
                    walk(child, stack)
                    continue
                walk(child, stack)

        walk(tree, [])
    return gate_refs, argv_sites


def _diff(found: set, pinned: set) -> str:
    return f"added={sorted(found - pinned)} removed={sorted(pinned - found)}"


@pytest.fixture(scope="module")
def scanned() -> tuple[dict[Site, set[str]], set[Site]]:
    return _scan(SRC)


def test_every_admission_site_is_listed(scanned):
    gate_refs, _ = scanned
    found = {key: frozenset(names & GATES) for key, names in gate_refs.items() if names & GATES}
    changed = sorted(
        (key, sorted(found[key]), sorted(ADMISSION_SITES[key]))
        for key in found.keys() & ADMISSION_SITES.keys()
        if found[key] != ADMISSION_SITES[key]
    )
    assert found == ADMISSION_SITES, (
        "the functions that reach an admission gate changed; classify each in "
        "ADMISSION_SITES and KNOWN_GAPS in the same change: "
        f"{_diff(set(found), set(ADMISSION_SITES))} changed={changed}"
    )


def test_known_gaps_match_the_admission_table():
    derived = {key: GATES - gates for key, gates in ADMISSION_SITES.items() if GATES - gates}
    pinned = {key: missing for key, (missing, _why) in KNOWN_GAPS.items()}
    assert derived == pinned, (
        "an admission site gained or lost a gate; restate KNOWN_GAPS so the change "
        f"in coverage is written down: {_diff(set(derived), set(pinned))}"
    )
    for key, (_missing, why) in KNOWN_GAPS.items():
        assert why.strip(), f"{key} must say whether its gap is covered elsewhere"


def test_every_agent_argv_site_is_classified(scanned):
    _, argv_sites = scanned
    assert argv_sites == set(AGENT_ARGV_SITES), (
        "a function that writes --agent was added or removed; name the admission "
        "sites that govern it in AGENT_ARGV_SITES, or state why none does: "
        f"{_diff(argv_sites, set(AGENT_ARGV_SITES))}"
    )


@pytest.mark.parametrize("site", sorted(AGENT_ARGV_SITES))
def test_a_governed_argv_site_points_at_real_admission_sites(site):
    governed_by = AGENT_ARGV_SITES[site]
    if isinstance(governed_by, str):
        assert governed_by.strip(), "an ungoverned site must say why"
        return
    assert governed_by, f"{site} names no governing site"
    for admission in governed_by:
        assert (
            admission in ADMISSION_SITES
        ), f"{site} claims {admission} governs it, but that reaches no admission gate"


def test_the_scan_sees_every_reference_form(tmp_path):
    pkg = tmp_path / "kiro_crew"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "import argparse, asyncio\n"
        "from kiro_crew import agent as agent_mod\n"
        "from kiro_crew.agent import require_fork_governance\n"
        "from kiro_crew.agent import require_fresh_derived_spec as fresh\n"
        "async def by_value(a, d):\n"
        "    await asyncio.to_thread(require_fork_governance, a, d)\n"
        "    return ['kiro', 'acp', '--agent', a]\n"
        "class Host:\n"
        "    def by_attribute(self, a, d):\n"
        "        agent_mod.require_fresh_derived_spec(a, d)\n"
        "    def by_alias(self, a, d):\n"
        "        fresh(a, d)\n"
        "    def by_getattr(self, a, d):\n"
        "        getattr(agent_mod, 'require_fork_governance')(a, d)\n"
        "def joined(a):\n"
        "    return [f'--agent={a}']\n"
        "def parser():\n"
        "    argparse.ArgumentParser().add_argument('--agent')\n"
        "def stub(m):\n"
        "    m.require_fork_governance = None\n",
        encoding="utf-8",
    )
    (pkg / "unrelated.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    gate_refs, argv_sites = _scan(pkg)
    assert gate_refs == {
        ("m.py", "by_value"): {FORK},
        ("m.py", "Host.by_attribute"): {FRESH},
        ("m.py", "Host.by_alias"): {FRESH},
        ("m.py", "Host.by_getattr"): {FORK},
    }
    assert argv_sites == {("m.py", "by_value"), ("m.py", "joined")}


def test_every_managed_shadow_site_is_listed(scanned):
    gate_refs, _ = scanned
    found = {key for key, names in gate_refs.items() if SHADOW in names}
    assert found == SHADOW_SITES, (
        "the functions that refuse a shadowed managed agent changed; update "
        f"SHADOW_SITES in the same change: {_diff(found, set(SHADOW_SITES))}"
    )


@pytest.mark.parametrize(
    "site", sorted(k for k, v in AGENT_ARGV_SITES.items() if not isinstance(v, str))
)
def test_every_governed_argv_site_refuses_a_shadowed_managed_agent(site):
    """A governed ``--agent`` site is a kiro-cli session spawn, by construction.

    Every other kind of site is classified with a string saying why no admission gate
    applies, so a tuple-valued entry is exactly the set this refusal must cover.
    """
    governed_by = AGENT_ARGV_SITES[site]
    assert SHADOW_SITES & set(governed_by), (
        f"{site} starts kiro-cli in a session's cwd, but none of the sites that "
        "govern it refuses a project copy of a managed agent name"
    )
