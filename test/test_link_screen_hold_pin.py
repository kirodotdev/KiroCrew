"""Pin the screen-then-resolve-by-name class, enumerated from source by condition.

THE CONDITION this gate enumerates:

    a function asks the filesystem whether path P is a link (the SCREEN), and
    then hands P -- or a name derived from P -- to something that resolves it,
    follows it, or reads through it (the USE).

The screen answers about a NAME at one instant. A USE that resolves that same
name afterwards can traverse a link planted in between, so the screen's verdict
does not carry. The cure is to hold one descriptor per component across both
halves, which makes the proof and the use the same walk.

Two things this gate deliberately does.

It finds sites by the CONDITION, never by a list of file names, because a key
shaped like "places that look like the ones already known" grows a defect one
site at a time. The list below is the declared BASELINE the condition's output is
compared against, not the search key.

And a USE is not only an ``os`` or ``pathlib`` call. A project helper that
resolves internally is equally a resolve at the call site, so the resolver set is
computed from source over the call graph. ``is_sensitive_path`` builds its
candidate forms with ``realpath``, so handing it a screened name is the probe.

Why the resolver set is bounded at ONE call hop. Depth 0 is the primitives alone
and misses a site whose USE is ``atomic_write`` rather than a primitive, so it
under-reports. Taken to a fixpoint the set saturates at about 20000 names --
essentially every function in the package, because nearly everything eventually
touches the filesystem -- and a saturated set makes the USE predicate vacuous and
the count meaningless. Depth 1 is the smallest hop count at which every control
module is found, so it is the honest one.

A run in which the controls do not all hit is UNKNOWN, not a pass: the condition
failed to find sites known to satisfy it, so its silence about everything else
carries no weight. :func:`test_control_modules_are_all_found` prints the count so
a reader sees the number rather than inferring it from a green tick.

The class divides into two kinds wanting OPPOSITE treatment, which is why this
gate pins a shape rather than converting callers. A site that VETS a link target
and then continues needs the hold. A site that refuses every link never traverses
one, so a hold buys it nothing -- and tightening it further costs an ordinary
Windows or macOS layout that puts a junction or symlink on the path to a config
directory, for no privilege gain. :data:`HELD_SITES` therefore names only the
sites whose contract is to hold, and those are the ones whose shape is asserted.

NOTHING here is skipped on Windows, which is the platform whose junctions are the
reason the hold exists. The shape gates read source, so they reach the same
verdict everywhere, including the mutation that proves the shape gate can tell a
held use from an unheld one. The two behavioural controls create their link with
:func:`platform_compat.symlink_or_junction` -- a symlink where the privilege
allows one, a directory junction otherwise -- so the layout under test is the
shape the product itself creates rather than a POSIX-only stand-in, and each one
drives BOTH platform branches: the one this host takes, and the chain-walking one
with the descriptor capability forced off, which is the only branch Windows has.
A control that silently skipped the branch it names would pass everywhere and
prove nothing where it matters.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from link_screen_sites import DECLARED_SITES
from source_corpus import candidate_sources, parsed_candidates, src_root

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew import platform_compat

#: Every test here reads the whole package, so they share one worker rather than
#: paying that scan once per worker under ``--dist loadgroup``.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_link_screen_hold_pin")

#: Predicates that ask the filesystem whether a path is a link. Each one marks
#: the pattern, so keying on a single symbol under-reports: the ancestor walk and
#: the single-path check reach the same shape by different routes.
SCREENS = frozenset({"first_linked_ancestor", "is_link_or_junction", "is_reparse_point"})

#: By-name resolves in ``os``, ``os.path``, ``pathlib`` and ``shutil``. Both
#: spellings of each question, because ``pathlib`` renames all of them.
PRIMITIVES = frozenset(
    {
        "realpath",
        "readlink",
        "resolve",
        "samefile",
        "stat",
        "lstat",
        "exists",
        "getsize",
        "getmtime",
        "isdir",
        "isfile",
        "islink",
        "is_dir",
        "is_file",
        "is_symlink",
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "listdir",
        "scandir",
        "iterdir",
        "glob",
        "rglob",
        "walk",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "move",
        "rmtree",
        "unlink",
        "mkdir",
        "makedirs",
        "rename",
        "touch",
    }
)

#: Names that are never a path resolve, however the call-graph hop admits them.
#: The resolver set keys on the UNQUALIFIED name, so a project helper called
#: ``append`` or ``run`` that touches the filesystem would otherwise make every
#: ``list.append`` and ``subprocess.run`` read as a resolve.
NEVER = frozenset(
    {
        "append",
        "extend",
        "add",
        "update",
        "insert",
        "pop",
        "remove",
        "discard",
        "close",
        "run",
        "get",
        "set",
        "put",
        "send",
        "format",
        "join",
        "split",
        "encode",
        "decode",
        "strip",
        "lower",
        "upper",
        "keys",
        "values",
        "items",
    }
)

#: A USE carrying this keyword is anchored to a descriptor, not to a name.
FD_KWARGS = frozenset({"dir_fd"})

#: One call hop. See the module docstring for why this is not a tunable knob.
RESOLVER_DEPTH = 1

#: Modules known to satisfy the condition. The gate must find every one, or its
#: report on the rest of the tree is UNKNOWN rather than clean.
CONTROL_MODULES = (
    "acp/prompt_blocks.py",
    "apps/builtins/auto_improvement/backend/clone_setup.py",
    "apps/builtins/aws_control/backend/backup.py",
    "apps/builtins/issue_radar/backend/crew_store.py",
    "apps/plugin_import.py",
    "dashboard/handlers/themes.py",
    "image_artifacts.py",
    "member_essential_context.py",
    "memory.py",
    "messaging/outbound_files.py",
)

#: Sites whose contract is to hold the chain across the screen AND the use. The
#: shape of each is asserted structurally below. A site earns a place here by
#: holding, not by being important.
HELD_SITES = frozenset(
    {
        ("apps/builtins/issue_radar/backend/crew_store.py", "_write_unit_order"),
    }
)

#: The helper a held site walks the chain with, and the one that releases it.
HOLD_CALL = "_hold_chain_no_follow"
RELEASE_CALL = "_release_held"

#: Floors on the gate's OWN lists. Both lists are the subject of a check that
#: iterates them, so deleting an entry shrinks the check that would have caught the
#: deletion and everything stays green. A floor is the one assertion a deletion
#: cannot satisfy, which is why these are numbers and a named member rather than a
#: second copy of the lists.
MIN_CONTROL_MODULES = 10
REQUIRED_HELD_SITE = ("apps/builtins/issue_radar/backend/crew_store.py", "_write_unit_order")


def test_the_gates_own_lists_are_not_shrunk() -> None:
    """Dropping a control module or the held site is a loosening, and it reddens."""
    assert len(CONTROL_MODULES) >= MIN_CONTROL_MODULES, (
        f"CONTROL_MODULES is down to {len(CONTROL_MODULES)} from a floor of "
        f"{MIN_CONTROL_MODULES}. Shrinking it weakens the UNKNOWN-rather-than-clean "
        "safeguard, which is the only thing making this gate's silence mean anything."
    )
    assert REQUIRED_HELD_SITE in HELD_SITES, (
        f"{REQUIRED_HELD_SITE[1]} is no longer named in HELD_SITES, so its shape is no "
        "longer asserted. A site leaves this set when it stops holding, which is a "
        "change to the site, not to this gate."
    )


def _verb(node: ast.Call) -> str:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def _qual(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f"{ast.unparse(f.value)}.{f.attr}"
    return f.id if isinstance(f, ast.Name) else ""


def _names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _anchored(node: ast.Call) -> bool:
    """Whether this call addresses a descriptor rather than a name.

    The keyword test comes first because it is the cheap one, and the receiver
    question is answered structurally rather than by unparsing it: this runs for
    every attribute call in the whole package, and ``ast.unparse`` on each receiver
    is the most expensive thing the scan would otherwise do.
    """
    if any(kw.arg in FD_KWARGS for kw in node.keywords):
        return True
    f = node.func
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name) and f.value.id == "pinned_fs":
            return True
        return "fstat" in f.attr
    return isinstance(f, ast.Name) and "fstat" in f.id


def _rel(path: Path) -> str:
    return path.relative_to(src_root()).as_posix()


#: Path fragments this gate does not police. Which files a gate covers is the
#: gate's own contract, so the corpus does not apply these. A test that plants a
#: junction on purpose screens and then resolves it BY DESIGN -- that is the
#: fixture, not the defect -- and vendored code is not ours to reshape.
UNPOLICED = ("tests/", "/testing/", "_vendor")


def _policed(path: Path) -> bool:
    rel = _rel(path)
    if path.name.startswith("test_") or path.name == "conftest.py":
        return False
    return not any(fragment in f"/{rel}" for fragment in UNPOLICED)


@functools.cache
def _resolver_names() -> frozenset[str]:
    """Names that resolve a path by name, within :data:`RESOLVER_DEPTH` hops."""
    callees: dict[str, set[str]] = {}
    for path, _text, tree in parsed_candidates():
        if not _policed(path):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seen = callees.setdefault(fn.name, set())
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and not _anchored(node):
                    seen.add(_verb(node))
    resolvers = set(PRIMITIVES)
    for _ in range(RESOLVER_DEPTH):
        grown = {n for n, calls in callees.items() if (calls & resolvers) and n not in NEVER}
        if grown <= resolvers:
            break
        resolvers |= grown
    return frozenset(resolvers)


class _Sites(ast.NodeVisitor):
    """Collect screens, the names they taint, and later uses of a tainted name.

    Scoped to ONE function. A nested ``def`` is a scope of its own, so its screens
    and the names they taint do not belong to the function that encloses it: a
    closure holding the only screen would otherwise make its enclosing function
    read as a site, and a variable name reused inside a closure would carry taint
    across a boundary it never crosses at runtime. :func:`_sites_in` walks every
    function in the tree, so a nested one is still scanned -- on its own.

    A ``lambda`` is deliberately NOT excluded: it is an expression, so
    :func:`_sites_in` never reaches it as a function of its own, and skipping it
    here would drop its body from every scan rather than move it.
    """

    def __init__(self, resolvers: frozenset[str]) -> None:
        self.resolvers = resolvers
        self.screen_lines: list[int] = []
        self.tainted: set[str] = set()
        self.uses: list[tuple[int, str, set[str]]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call) and _verb(node.value) in SCREENS:
            # The ancestor a screen RETURNS is itself a name, so reading through
            # it re-walks just as the argument does.
            for target in node.targets:
                self.tainted |= _names(target)
        elif self.tainted & _names(node.value):
            for target in node.targets:
                self.tainted |= _names(target)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        verb = _verb(node)
        if verb in SCREENS:
            arg = node.args[0] if node.args else None
            self.tainted |= _names(arg)
            self.screen_lines.append(node.lineno)
        elif verb in self.resolvers and verb not in NEVER and not _anchored(node):
            subject: ast.AST | None = node.args[0] if node.args else None
            if subject is None and isinstance(node.func, ast.Attribute):
                subject = node.func.value
            self.uses.append((node.lineno, _qual(node) or verb, _names(subject)))
        self.generic_visit(node)


def _qualified_functions(
    node: ast.AST, prefix: str = ""
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every function under *node*, each paired with its dotted scope path.

    The path is what makes a site's identity unique inside one module. A bare
    ``fn.name`` collapses two same-named functions -- two classes' methods, or a
    nested ``def`` whose name is reused -- into one entry, and the baseline is
    compared by set difference, so the second one is subtracted away by the first
    and reaches no reviewer. ``Outer.inner`` and ``ClassName.method`` do not
    collide.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = f"{prefix}{child.name}"
            yield qualified, child
            yield from _qualified_functions(child, f"{qualified}.")
        elif isinstance(child, ast.ClassDef):
            yield from _qualified_functions(child, f"{prefix}{child.name}.")
        else:
            # Not a scope of its own -- a function under an ``if`` or a ``try`` at
            # module level still belongs to the scope that encloses that statement.
            yield from _qualified_functions(child, prefix)


def _sites_in(tree: ast.Module, resolvers: frozenset[str]) -> Iterator[tuple[str, int]]:
    for qualified, fn in _qualified_functions(tree):
        scan = _Sites(resolvers)
        for child in fn.body:
            scan.visit(child)
        if not scan.screen_lines:
            continue
        first = min(scan.screen_lines)
        if any(line >= first and (names & scan.tainted) for line, _verb_, names in scan.uses):
            yield qualified, first


@functools.cache
def discovered_sites() -> frozenset[tuple[str, str]]:
    """Every ``(module, function)`` in the package that satisfies the condition."""
    resolvers = _resolver_names()
    found: set[tuple[str, str]] = set()
    for path, _text, tree in parsed_candidates(require_any=tuple(SCREENS)):
        if not _policed(path):
            continue
        for func, _line in _sites_in(tree, resolvers):
            found.add((_rel(path), func))
    return frozenset(found)


def test_control_modules_are_all_found() -> None:
    """Every module known to satisfy the condition is found, and the count is shown."""
    modules = {module for module, _func in discovered_sites()}
    hit = [m for m in CONTROL_MODULES if m in modules]
    missed = [m for m in CONTROL_MODULES if m not in modules]
    # Printed rather than only asserted: a reader judging this gate needs the
    # number, and a green tick alone does not carry it.
    print(
        f"link-screen condition: {len(hit)}/{len(CONTROL_MODULES)} control modules, "
        f"{len(discovered_sites())} sites in {len(modules)} modules, "
        f"resolver depth {RESOLVER_DEPTH} ({len(_resolver_names())} names)"
    )
    assert not missed, (
        "the condition does not find these control modules, so its report on the "
        f"rest of the tree is UNKNOWN rather than clean: {sorted(missed)}"
    )


def test_every_site_is_declared() -> None:
    """A new screen-then-resolve site declares itself in :data:`DECLARED_SITES`."""
    undeclared = sorted(discovered_sites() - DECLARED_SITES)
    assert not undeclared, (
        "these functions screen a path for links and then resolve, follow or read "
        "the same name afterwards, which is the window a link planted in between "
        "slips through. Decide which kind the new site is FIRST: if it vets a link "
        "target and continues, hold the chain across both halves and add it to "
        "HELD_SITES, whose shape is asserted; if it refuses every link it never "
        "traverses one and needs no hold. Then run test/gen_link_screen_sites.py to "
        "record it. The baseline is a discovery record and carries no reason, so a "
        "reason belongs in the commit message or the reviewed issue, never here:\n"
        + "\n".join(f"    {module}::{func}" for module, func in undeclared)
    )


def test_declared_sites_still_exist() -> None:
    """Every name in the baseline is a site the condition still finds."""
    stale = sorted(DECLARED_SITES - discovered_sites())
    assert not stale, (
        "DECLARED_SITES names sites the condition does not find. Remove them so "
        "the baseline keeps meaning what it says:\n"
        + "\n".join(f"    {module}::{func}" for module, func in stale)
    )


def _held_try_blocks(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Try]:
    """``try`` blocks that run while a chain walked by :data:`HOLD_CALL` is held."""
    held_names: set[str] = set()
    blocks: list[ast.Try] = []
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            if _verb(stmt.value) == HOLD_CALL:
                for target in stmt.targets:
                    held_names |= _names(target)
        elif isinstance(stmt, ast.Try):
            released = any(
                isinstance(node, ast.Call) and _verb(node) == RELEASE_CALL
                for node in ast.walk(ast.Module(body=stmt.finalbody, type_ignores=[]))
            )
            if released and held_names:
                blocks.append(stmt)
    return blocks


def _function_named(tree: ast.Module, func: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The function at the dotted scope path *func*, the same key sites are keyed by."""
    return next((fn for qualified, fn in _qualified_functions(tree) if qualified == func), None)


def _assert_screen_and_use_are_held(source: str, module: str, func: str) -> None:
    """Raise ``AssertionError`` unless *func* screens and resolves inside one hold.

    Takes SOURCE TEXT rather than a path so the same judgement can be run against a
    mutated copy, which is how this gate proves it discriminates. AST only, so it
    reaches the same verdict on every platform.
    """
    tree = ast.parse(source, filename=module)
    target = _function_named(tree, func)
    assert target is not None, f"{module} defines no {func}"

    blocks = _held_try_blocks(target)
    assert blocks, (
        f"{module}::{func} names a held site, but no try block runs while a chain "
        f"walked by {HOLD_CALL} is held and released by {RELEASE_CALL}"
    )

    resolvers = _resolver_names()
    for block in blocks:
        body = ast.Module(body=block.body, type_ignores=[])
        calls = [node for node in ast.walk(body) if isinstance(node, ast.Call)]
        screened = [c for c in calls if _verb(c) in SCREENS]
        # A screen is never its own use. The screens reach a primitive themselves
        # -- the junction check asks ``islink`` -- so the resolver set admits them,
        # and counting one as the use lets a block satisfy this assertion with the
        # screen alone while the write it is supposed to cover sits outside the hold.
        used = [
            c
            for c in calls
            if _verb(c) in resolvers
            and _verb(c) not in SCREENS
            and _verb(c) not in NEVER
            and not _anchored(c)
        ]
        if screened and used:
            return

    raise AssertionError(
        f"{module}::{func} holds a chain, but its screen and its by-name use are not "
        "both inside that hold, so the window between them is open again. Keep the "
        f"{', '.join(sorted(SCREENS))} check and the write in the same held block."
    )


@pytest.mark.parametrize(("module", "func"), sorted(HELD_SITES))
def test_held_site_screens_and_resolves_inside_one_hold(module: str, func: str) -> None:
    """The screen and the by-name use both run while the chain is held."""
    path = src_root() / module
    _assert_screen_and_use_are_held(path.read_text(encoding="utf-8"), module, func)


class _Mutation(NamedTuple):
    """A mutated source, and the statements the mutation relocated to produce it."""

    source: str
    moved: tuple[str, ...]


def _use_moved_out_of_the_hold(source: str, module: str, func: str) -> _Mutation:
    """*source* with *func*'s by-name uses moved to AFTER the hold is released.

    The exact defect this gate exists to catch: the screen still runs under the
    hold, and the resolve of the same name runs once the descriptors are gone.
    Rewritten on the AST rather than by string surgery, so it applies to any held
    site instead of one hand-written spelling.

    Reports the relocated statements alongside the result, so a caller can assert
    that the edit moved something instead of inferring it from the two texts
    differing -- which they do for any input, since the result is unparsed.
    """
    tree = ast.parse(source, filename=module)
    target = _function_named(tree, func)
    assert target is not None, f"{module} defines no {func}"
    resolvers = _resolver_names()

    def resolves(stmt: ast.stmt) -> bool:
        return any(
            isinstance(node, ast.Call)
            and _verb(node) in resolvers
            and _verb(node) not in SCREENS
            and _verb(node) not in NEVER
            and not _anchored(node)
            for node in ast.walk(stmt)
        )

    for parent in ast.walk(target):
        for field, value in ast.iter_fields(parent):
            if not isinstance(value, list):
                continue
            for index, stmt in enumerate(list(value)):
                if not isinstance(stmt, ast.Try) or stmt not in _held_try_blocks(target):
                    continue
                moved = [inner for inner in stmt.body if resolves(inner)]
                if not moved:
                    continue
                relocated = tuple(ast.unparse(inner) for inner in moved)
                kept = [inner for inner in stmt.body if inner not in moved]
                stmt.body = kept or [ast.Pass()]
                value[index + 1 : index + 1] = moved
                setattr(parent, field, value)
                return _Mutation(ast.unparse(ast.fix_missing_locations(tree)), relocated)

    raise AssertionError(f"{module}::{func} has no by-name use inside its hold to move")


#: The shape the judgement must ACCEPT: hold, screen and use, release, in that order.
_CORRECT_SHAPE = """
def target(path):
    held = _hold_chain_no_follow(path.parent)
    try:
        if is_link_or_junction(path):
            raise OSError("refused")
        return path.read_text()
    finally:
        _release_held(held)
"""

#: Shapes the judgement must REJECT, each removing exactly ONE thing it requires. A
#: judgement is only as good as what it refuses, and a predicate no input can
#: exercise is a predicate that can be deleted without any test noticing. Synthetic
#: sources rather than mutations of the real file, so each case isolates one
#: predicate instead of whatever the real file happens to contain.
_BROKEN_SHAPES = {
    "no screen inside the hold": """
def target(path):
    held = _hold_chain_no_follow(path.parent)
    try:
        return path.read_text()
    finally:
        _release_held(held)
""",
    "no release in the finally": """
def target(path):
    held = _hold_chain_no_follow(path.parent)
    try:
        if is_link_or_junction(path):
            raise OSError("refused")
        return path.read_text()
    finally:
        pass
""",
    "the use sits outside the hold": """
def target(path):
    held = _hold_chain_no_follow(path.parent)
    try:
        if is_link_or_junction(path):
            raise OSError("refused")
    finally:
        _release_held(held)
    return path.read_text()
""",
    "no hold walked at all": """
def target(path):
    if is_link_or_junction(path):
        raise OSError("refused")
    return path.read_text()
""",
    "the screen is the only thing held": """
def target(path):
    held = _hold_chain_no_follow(path.parent)
    try:
        if is_link_or_junction(path):
            raise OSError("refused")
    finally:
        _release_held(held)
""",
}


def test_shape_judgement_accepts_the_correct_shape() -> None:
    """The positive control: without it, a judgement that rejects everything passes."""
    _assert_screen_and_use_are_held(_CORRECT_SHAPE, "synthetic.py", "target")


@pytest.mark.parametrize("missing", sorted(_BROKEN_SHAPES))
def test_shape_judgement_rejects_each_broken_shape(missing: str) -> None:
    """Every requirement the judgement states is one an input can actually break."""
    with pytest.raises(AssertionError):
        _assert_screen_and_use_are_held(_BROKEN_SHAPES[missing], "synthetic.py", "target")


@pytest.mark.parametrize(("module", "func"), sorted(HELD_SITES))
def test_shape_gate_reddens_when_the_use_leaves_the_hold(module: str, func: str) -> None:
    """Moving the resolve out of the hold makes this gate fail.

    Without this, a green tick on :func:`test_held_site_screens_and_resolves_inside_one_hold`
    could mean the judgement is sound or that it cannot tell the shapes apart, and a
    reader has no way to know which. Runs on EVERY platform, Windows included, so the
    proof comes from the platform whose junctions are the reason the hold exists --
    not only from the one where a plain symlink can be created.
    """
    source = (src_root() / module).read_text(encoding="utf-8")
    mutation = _use_moved_out_of_the_hold(source, module, func)
    assert mutation.moved, f"{module}::{func}: the mutation relocated nothing"
    # Like against like. The mutation is produced by ``ast.unparse``, whose output
    # differs from the file's own text for every input, because comments are dropped
    # and the docstring is renormalised. Comparing the result against the raw text is
    # a test every input passes, so it carries no evidence that the edit landed.
    assert mutation.source != ast.unparse(ast.parse(source, filename=module)), (
        f"{module}::{func} unparses identically with and without the relocation, "
        "so a red below proves nothing"
    )

    # The unmutated control: a failure here means the mutation is not what reddens.
    _assert_screen_and_use_are_held(source, module, func)
    with pytest.raises(AssertionError):
        _assert_screen_and_use_are_held(mutation.source, module, func)


def test_the_relocation_proves_itself_by_what_it_moved() -> None:
    """The mutation's proof of application is one an input can actually fail.

    A guard that cannot fire is worth nothing, so both halves of the one above are
    checked here against the real held site. ``ast.unparse`` renormalises whatever
    it is handed, so its output differs from the file's own text whether or not a
    statement moved -- which is why the proof is the reported relocation plus a
    comparison against the unparse of the same original.
    """
    module, func = REQUIRED_HELD_SITE
    source = (src_root() / module).read_text(encoding="utf-8")
    mutation = _use_moved_out_of_the_hold(source, module, func)

    assert mutation.moved, "the relocation reports nothing it moved"
    assert any("atomic_write" in moved for moved in mutation.moved), (
        f"the relocated statements do not include the resolve this site holds for: "
        f"{mutation.moved}"
    )

    normalised = ast.unparse(ast.parse(source, filename=module))
    assert normalised != source, (
        "unparse returns this file's own text byte for byte, so comparing a mutation "
        "against the raw text would already be a real test and this guard is idle"
    )
    assert mutation.source != normalised, "the relocation does not change the source"


def test_an_ordinary_link_layout_is_not_broken_by_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write whose own chain carries no link still works on a host that HAS links.

    The passing control for this gate. A hold that refused any host where a reparse
    point exists would read as correct against the refusal direction alone, while
    breaking every machine whose config tree is placed by a dotfile manager: a
    supported layout, not an attack. What the hold owes is that the screen and the
    use reach the same object, NOT that no junction exists here.

    Both branches are exercised on every platform, because one of them is otherwise
    unreachable and a control that never runs the code it names proves nothing.
    Where a parent descriptor can carry the rename, the write takes the pinned
    parent and returns before the chain is ever walked -- so the hold is also driven
    with that capability forced off, which is the only branch Windows has.

    The link is made with the product's own :func:`platform_compat.symlink_or_junction`,
    so the layout is the shape the product itself creates: a symlink where the
    privilege allows one, a directory junction on an unelevated Windows host.
    """
    from kiro_crew.apps.builtins.issue_radar.backend import crew_store

    real = tmp_path / "real"
    (real / "crew").mkdir(parents=True)
    platform_compat.symlink_or_junction(real, tmp_path / "linked")
    assert (tmp_path / "linked" / "crew").is_dir(), "the link layout was not created"

    def write_under(root: Path, *, force_hold: bool) -> None:
        (root / "crew").mkdir(parents=True)
        target = root / "crew" / "unit-order.txt"
        with monkeypatch.context() as patched:
            if force_hold:
                patched.setattr(
                    atomic_write_module, "pinned_parent_replace_supported", lambda: False
                )
            walks_the_chain = not atomic_write_module.pinned_parent_replace_supported()
            if walks_the_chain and platform_compat.first_linked_ancestor(target) is not None:
                # MEASURED, not assumed, and it still asserts rather than passing
                # silently. A host can place its own temp tree under a link -- macOS
                # ``/tmp`` is one -- so a chain with no link on it is not expressible
                # under ``tmp_path`` there, and the write refuses for the host's link
                # rather than for anything this gate is about.
                with pytest.raises(OSError):
                    crew_store._write_unit_order(target, ("alpha", "beta"))
                return
            crew_store._write_unit_order(target, ("alpha", "beta"))
        assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"

    write_under(tmp_path / "platform", force_hold=False)
    write_under(tmp_path / "held", force_hold=True)


def test_write_through_a_link_reaches_the_object_the_screen_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Addressed THROUGH a link, the write lands in the screened object or refuses.

    Which of the two happens is the platform's answer, not this gate's: where a
    parent descriptor can carry the rename, the write goes through the pinned parent
    and lands in the real directory; where the chain is walked and held instead, a
    link on that chain is refused outright. Both are asserted here on every
    platform, the second by forcing the descriptor capability off, so the refusal is
    measured everywhere rather than only where it is the default.

    That refusal is the reason this gate converts nothing. On the held branch --
    which is the only branch the platform with junctions has -- this function does
    not write through an ordinary dotfile-manager layout at all: it already refuses
    every link on its parent chain, so extending the hold to further call sites buys
    no privilege and costs supported layouts. Which is why :data:`HELD_SITES` names
    the sites that vet a target and continue, and leaves the refusing ones alone.
    """
    from kiro_crew.apps.builtins.issue_radar.backend import crew_store

    real = tmp_path / "real"
    (real / "crew").mkdir(parents=True)
    link = tmp_path / "linked"
    platform_compat.symlink_or_junction(real, link)
    settled = real / "crew" / "unit-order.txt"
    settled.write_text("alpha\nbeta\n", encoding="utf-8")
    through_link = link / "crew" / "unit-order.txt"

    if atomic_write_module.pinned_parent_replace_supported():
        crew_store._write_unit_order(through_link, ("gamma",))
        assert settled.read_text(encoding="utf-8") == "gamma\n"
        settled.write_text("alpha\nbeta\n", encoding="utf-8")

    with monkeypatch.context() as patched:
        patched.setattr(atomic_write_module, "pinned_parent_replace_supported", lambda: False)
        with pytest.raises(OSError):
            crew_store._write_unit_order(through_link, ("gamma",))
    assert settled.read_text(encoding="utf-8") == "alpha\nbeta\n"

    # Either way nothing was created outside the object the screen inspected.
    assert sorted(p.name for p in (real / "crew").iterdir()) == ["unit-order.txt"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["linked", "real"]


def test_the_generator_writes_its_baseline_atomically() -> None:
    """The baseline is staged under a unique name and renamed, never truncated in place.

    A truncating write leaves a partial file on an interruption or a full disk, and a
    partial baseline cannot be imported -- which breaks collection for every test in
    this module, including the ones that would have reported the damage.
    """
    generator = Path(__file__).resolve().parent / "gen_link_screen_sites.py"
    tree = ast.parse(generator.read_text(encoding="utf-8"), filename=str(generator))
    writes = [
        _verb(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _verb(node) in {"write_text", "write_bytes", "atomic_write"}
    ]
    assert writes == ["atomic_write"], (
        "the generator must stage its baseline and rename it over the destination, "
        f"not truncate the file it is replacing; found these write calls: {writes}"
    )


def test_gate_reads_the_package_it_claims_to() -> None:
    """The corpus filter reaches files holding a screen, so the scan is not empty."""
    assert candidate_sources(require_any=tuple(SCREENS)), (
        "no source file mentions any screen predicate, so the corpus filter is "
        "wrong and every gate above is vacuously green"
    )
    assert discovered_sites(), "the condition found no sites at all, which is not credible"
