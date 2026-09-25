"""A mirrored name's owner has ONE storage location, in every module that mirrors one.

``sys.modules`` is where a module is stored. A module that mirrors a surface and
also holds a mapping to the resolved owner MODULE has a second storage location
for it, and the two can disagree: purging an owner and importing it again -- an
idiom this suite uses in twenty files -- leaves such a mapping reading and
forwarding writes to the discarded module while a direct importer holds the fresh
one. A test patching a control through the mirror then passes while exercising an
object nobody is running, and a later test in the same worker reads a value that
disagrees with its own owner, in another file, under some shard splits and not
others, with nothing pointing back at the cause.

The rule, stated once:

    a module that defines a module-level ``__getattr__``, or swaps its own
    ``__class__`` to forward attribute writes, MUST NOT hold a mapping whose
    values are module objects. It keeps the owner's dotted NAME and resolves it
    per use with ``importlib.import_module``, which answers from ``sys.modules``
    and waits on that module's import lock while its body runs.

These tests find the modules the rule binds BY THEIR SHAPE, read off the source
tree, rather than from a list of names. A module that begins mirroring a surface is
therefore covered on the commit that introduces it, with no edit here. A list of
covered modules would be a second thing to remember, which is the same failure the
rule is about.

Seven modules in this package hold such a mapping today, so the rule is enforced as
a RATCHET rather than as a flat universal. ``_KNOWN_RESOLVED_OWNER_MIRRORS`` names
those seven, and the assertions run in both directions against it: a mirroring
module OUTSIDE that set must satisfy the rule, and a module INSIDE it must still
violate the rule, so a module that gets converted has to be de-listed and the set
can only shrink. An eighth violator reddens on the commit that introduces it, and a
name that stops violating reddens until it leaves the list. The set is therefore a
measurement of where the package stands, not permission to stay there.

Two guards keep that generation honest, because a case list derived from a detector
goes SILENT rather than red when the detector stops matching:
``test_the_shape_detector_answers_both_ways`` pins the detector on synthetic sources
it must accept and reject, and ``test_the_discovery_rule_answers_both_ways`` does the
same for the attribute discovery that the purge cases rest on. For the same reason no
case here skips: a module the purge probe cannot measure fails, because a verdict
nobody can produce is indistinguishable from a rule nobody checks.

The purge cases run in a child process. Importing a module binds it onto its parent
package, so a purge and reimport changes the ``sys.modules`` entry, the parent's
attribute, and any memo the mirror keeps; restoring one of the three in a ``finally``
leaves exactly the split-brain residue this rule forbids, and restoring all three is
a maintenance contract inside a test. A child process has no contract: its residue
leaves with it, and ``test_the_probe_leaves_this_process_untouched`` reads the
parent-package bindings back afterwards to prove it.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus
from mirrored_owner_probe import SENTINEL, one_reexported_pair

from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: Text that must appear for a file to be worth parsing, which is what keeps this
#: scan off the two thirds of the tree that mirror nothing.
_SHAPE_NEEDLES = ("__getattr__", "__class__")


def mirrors_a_surface(tree: ast.Module) -> bool:
    """True when the module resolves or forwards attributes on another's behalf.

    Two spellings do that, and both are visible at module level: a ``__getattr__``
    function, which Python calls for a name the module does not hold, and an
    assignment to some object's ``__class__``, which is how a module installs a
    ``ModuleType`` subclass over itself to intercept writes.
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "__class__":
                    return True
    return False


def _module_level_statements(tree: ast.Module) -> list[ast.stmt]:
    """Every statement that executes in the module's own namespace.

    Descends through module-level ``if`` / ``try`` / ``with`` / ``for`` bodies, because
    a table can be declared inside one, but never into a function or class: a name
    bound there lives in that scope, not in the module, so it is not a storage
    location for anything the module mirrors.
    """
    collected: list[ast.stmt] = []
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        node = stack.pop()
        collected.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            for child in getattr(node, field, []) or []:
                if isinstance(child, ast.stmt):
                    stack.append(child)
                elif isinstance(child, ast.ExceptHandler):
                    stack.extend(child.body)
    return collected


def owner_module_tables(tree: ast.Module) -> list[str]:
    """Return the MODULE-LEVEL names bound to a mapping whose VALUES are module objects.

    Two spellings reach that: an annotation declaring ``dict[..., ModuleType]``, and a
    dict comprehension whose value expression is a bare name -- a name bound to an
    imported module, since a comprehension over modules is how such a table gets
    built. A mapping of dotted names is neither, because its values are strings and
    its annotation says so.

    Only module-level bindings count. A function's own local of the same annotation is
    scratch space that dies with the call, so counting it would report a storage
    location that does not exist.
    """
    found: list[str] = []
    for statement in _module_level_statements(tree):
        if isinstance(statement, ast.AnnAssign):
            targets: list[ast.expr] = [statement.target]
            annotation = ast.unparse(statement.annotation)
            value: ast.expr | None = statement.value
        elif isinstance(statement, ast.Assign):
            targets = list(statement.targets)
            annotation = ""
            value = statement.value
        else:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names:
            continue
        declares_modules = annotation.startswith("dict") and "ModuleType" in annotation
        builds_modules = isinstance(value, ast.DictComp) and isinstance(value.value, ast.Name)
        if declares_modules or builds_modules:
            found.extend(names)
    return sorted(set(found))


def _module_name(path: Path) -> str:
    """Return the dotted import name of the package file at *path*."""
    root = source_corpus.src_root()
    parts = [root.name, *path.relative_to(root).with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _mirroring_modules() -> list[tuple[str, str]]:
    """Return ``(dotted name, source text)`` for every module that mirrors a surface.

    Read through ``source_corpus``, which holds one cached read of the package and
    hands out one parsed tree at a time, so this costs no second walk of the tree and
    retains no trees. Its ``src_root`` is also the importable package, which is what
    keeps these names in step with the runtime half below.
    """
    rows: list[tuple[str, str]] = []
    for path, text, tree in source_corpus.parsed_candidates(require_any=_SHAPE_NEEDLES):
        if mirrors_a_surface(tree):
            rows.append((_module_name(path), text))
    return rows


MIRRORING = _mirroring_modules()

#: One case per mirroring module, so a failure names the module in its own id.
MIRRORING_IDS = [name for name, _text in MIRRORING]

# The modules in this package that hold a mapping to resolved owner MODULES, as the
# source scan above finds them. RATCHET: this set may only SHRINK. Converting a module
# to hold dotted names and resolve them per use means de-listing it here, and a name
# left behind after its conversion fails ``test_only_baseline_modules_violate_the_rule``
# in the other direction. Do NOT add a name here to make a red go away: a new mirroring
# module that stores resolved owners is the defect this file exists to catch, and the
# fix is the conversion, which is a handful of lines per module.
_KNOWN_RESOLVED_OWNER_MIRRORS = frozenset(
    {
        "kiro_crew.config",
        "kiro_crew.crew_log",
        "kiro_crew.dashboard",
        "kiro_crew.diag",
        "kiro_crew.mcp_gateway",
        "kiro_crew.security",
        "kiro_crew.stt",
    }
)

#: The mirroring modules the rule binds with no exception, which is every one the scan
#: finds minus the baseline. The purge cases below run over these, so a module that
#: leaves the baseline gains that coverage on the same commit, with no edit here.
COMPLIANT = [row for row in MIRRORING if row[0] not in _KNOWN_RESOLVED_OWNER_MIRRORS]
COMPLIANT_IDS = [name for name, _text in COMPLIANT]


def rule_violations(name: str, text: str) -> list[str]:
    """Return the ways *name* breaks the one-storage rule, read off its source.

    Two halves, both visible in the source: a module-level mapping whose values are
    module objects is a second storage location, and a mirroring module that never
    calls ``importlib.import_module`` is not resolving owners from ``sys.modules`` at
    all. Either one alone is a violation, so the empty list is the only clean answer.
    """
    problems: list[str] = []
    tables = owner_module_tables(ast.parse(text))
    if tables:
        problems.append(
            f"{tables} is a mapping to resolved owner MODULES, a second storage "
            "location beside sys.modules"
        )
    if "importlib.import_module(" not in text:
        problems.append(
            "it never calls importlib.import_module, so its owners are not resolved "
            "from sys.modules per use"
        )
    return problems


_DETECTOR_CASES: tuple[tuple[str, bool, str], ...] = (
    ("def __getattr__(name):\n    return 1\n", True, "a module-level __getattr__"),
    (
        "import sys\nsys.modules[__name__].__class__ = X\n",
        True,
        "installing a ModuleType subclass over itself",
    ),
    ("class C:\n    def __getattr__(self):\n        return 1\n", False, "a CLASS __getattr__"),
    ("X = 1\n", False, "an ordinary module"),
)

_TABLE_CASES: tuple[tuple[str, list[str], str], ...] = (
    ("from types import ModuleType\n_O: dict[str, ModuleType] = {}\n", ["_O"], "declared"),
    ("_O = {n: mod for mod in MODS for n in mod.__all__}\n", ["_O"], "a comprehension of modules"),
    ("_O = {n: mod.__name__ for mod in MODS for n in mod.__all__}\n", [], "dotted names"),
    ("_O: dict[str, str] = {}\n", [], "a declared name table"),
    (
        "def f():\n    owners: dict[str, ModuleType] = {}\n    return owners\n",
        [],
        "a FUNCTION-LOCAL table, which dies with the call",
    ),
    (
        "class C:\n    _O: dict[str, ModuleType] = {}\n",
        [],
        "a CLASS attribute, which is not the module's namespace",
    ),
    (
        "if TYPE_CHECKING:\n    _O: dict[str, ModuleType] = {}\n",
        ["_O"],
        "a module-level table inside an if block",
    ),
)


def test_the_shape_detector_answers_both_ways() -> None:
    """The detector the cases below are generated from, pinned on synthetic sources.

    A detector matching nothing would make every parametrized case vanish rather
    than fail, so it is measured directly, on inputs it must accept and inputs it
    must reject.
    """
    for source, expected, why in _DETECTOR_CASES:
        assert mirrors_a_surface(ast.parse(source)) is expected, why
    for source, expected_tables, why in _TABLE_CASES:
        assert owner_module_tables(ast.parse(source)) == expected_tables, why


def test_the_needle_prefilter_admits_every_shape_the_detector_accepts() -> None:
    """The text prefilter must not hide a module the detector would have matched."""
    for source, expected, why in _DETECTOR_CASES:
        if expected:
            assert any(needle in source for needle in _SHAPE_NEEDLES), (
                f"the prefilter would skip {why}, so such a module would never reach "
                "the detector and its absence would read as compliance"
            )


def test_the_discovery_rule_answers_both_ways() -> None:
    """The discovery the purge cases rest on, pinned like the shape detector.

    A table spelling this rule does not recognise leaves the purge cases with nothing
    to measure, which they report as a failure, so the rule is measured on modules
    built here: each value spelling it must accept, and the two kinds of value it must
    pass over.
    """
    probe = ModuleType("kiro_crew_probe_owner")
    probe.__name__ = "kiro_crew.probe_mirror"
    leaf = importlib.import_module("kiro_crew.subprocess_utf8")

    accepted = {
        "a dotted module name": {"UTF8_TEXT": "kiro_crew.subprocess_utf8"},
        "a module object": {"UTF8_TEXT": leaf},
        "an (owner, symbol) pair": {"UTF8_TEXT": ("kiro_crew.subprocess_utf8", "UTF8_TEXT")},
        "a bare name under the parent": {"UTF8_TEXT": "subprocess_utf8"},
    }
    for why, table in accepted.items():
        probe._TABLE = table  # type: ignore[attr-defined]
        assert one_reexported_pair(probe) == (
            "UTF8_TEXT",
            "kiro_crew.subprocess_utf8",
        ), f"discovery missed {why}"

    rejected = {
        "a dunder key": {"__spec__": "kiro_crew.subprocess_utf8"},
        "an attribute the owner lacks": {"no_such_attribute_here": "kiro_crew.subprocess_utf8"},
        "an owner outside this package": {"getcwd": "os"},
        "a value that is not a name": {"UTF8_TEXT": 17},
    }
    for why, table in rejected.items():
        probe._TABLE = table  # type: ignore[attr-defined]
        assert one_reexported_pair(probe) is None, f"discovery accepted {why}"

    probe.__dunder_table__ = {  # type: ignore[attr-defined]
        "UTF8_TEXT": "kiro_crew.subprocess_utf8"
    }
    del probe._TABLE  # type: ignore[attr-defined]
    assert one_reexported_pair(probe) is None, "discovery read a dunder-named table"


def test_the_scan_finds_the_modules_that_mirror_a_surface() -> None:
    """The scan reaches the package at all, so an empty result is a broken scan.

    The floor is the baseline plus one, and it is derived rather than chosen: every
    baseline name must be found for its case to exist, and at least one module beyond
    them must be found or the purge cases below would have no case to run and their
    universal half would be vacuous.
    """
    assert MIRRORING, f"no mirroring module found under {source_corpus.src_root()}"
    floor = len(_KNOWN_RESOLVED_OWNER_MIRRORS) + 1
    assert len(MIRRORING) >= floor, (
        f"the scan found only {len(MIRRORING)} mirroring module(s) but the baseline "
        f"names {len(_KNOWN_RESOLVED_OWNER_MIRRORS)}, so the detector has stopped "
        "matching and the cases below have gone quiet rather than red"
    )
    assert COMPLIANT, (
        "every mirroring module the scan found is on the baseline, so the universal "
        "half of this file asserts nothing; the purge cases need at least one module "
        "the rule binds with no exception"
    )


def test_the_probe_bound_sits_under_the_suite_ceiling() -> None:
    """The probe child's own deadline must be reachable as a readable failure.

    pytest-timeout arms at item setup, before the module-scoped fixture spawns the
    child, so a bound at or above the suite-wide ceiling can never fire: the suite
    deadline lands first and takes the whole worker with it, which is a lost run and
    not one failed test. The ceiling is READ here rather than copied into this file,
    because a copy is the defect this file exists to forbid.
    """
    config = source_corpus.repo_root() / "setup.cfg"
    text = config.read_text(encoding="utf-8")
    ceilings = [
        int(match) for match in re.findall(r"^\s*--timeout=(\d+)\s*$", text, flags=re.MULTILINE)
    ]
    assert len(ceilings) == 1, (
        f"expected exactly one suite-wide --timeout in {config.name} to derive the "
        f"probe bound from, found {ceilings}; the derivation below is now ambiguous"
    )
    assert _PROBE_TIMEOUT_SECONDS < ceilings[0], (
        f"the probe bound is {_PROBE_TIMEOUT_SECONDS}s and the suite ceiling is "
        f"{ceilings[0]}s, so the child's own deadline can never be reached: "
        "pytest-timeout fires first and the hang costs the worker, not one test"
    )


def test_the_baseline_names_only_modules_the_scan_still_finds() -> None:
    """Every baseline name must be a module the shape scan still finds.

    Without this, a module deleted or rewritten to mirror nothing would keep its
    entry, the per-module case that checks the other direction would not be generated
    for it, and the list would rot into a name nobody can account for.
    """
    stale = sorted(_KNOWN_RESOLVED_OWNER_MIRRORS - set(MIRRORING_IDS))
    assert stale == [], (
        f"the baseline names module(s) the shape scan does not find: {stale}. Remove "
        "them so the list keeps tightening."
    )


@pytest.mark.parametrize(("name", "text"), MIRRORING, ids=MIRRORING_IDS)
def test_only_baseline_modules_violate_the_rule(name: str, text: str) -> None:
    """The rule, read off the source of every module the scan finds, in both directions.

    A module off the baseline must satisfy the rule outright. A module on it must still
    break the rule, which is what stops the baseline becoming standing permission: the
    commit that converts a module has to de-list it, and the list can only shrink.
    """
    problems = rule_violations(name, text)
    if name in _KNOWN_RESOLVED_OWNER_MIRRORS:
        assert problems != [], (
            f"{name} satisfies the one-storage rule but is still listed in "
            "_KNOWN_RESOLVED_OWNER_MIRRORS. Remove it from that set so the ratchet "
            "keeps tightening."
        )
        return
    assert problems == [], (
        f"{name} mirrors a surface and breaks the one-storage rule: "
        + "; ".join(problems)
        + ". Hold the owner's dotted NAME and resolve it per use with "
        "importlib.import_module, which answers from sys.modules. Do not add this "
        "module to _KNOWN_RESOLVED_OWNER_MIRRORS."
    )


@pytest.mark.parametrize(("name", "text"), MIRRORING, ids=MIRRORING_IDS)
def test_no_unlisted_mapping_holds_a_module_object(name: str, text: str) -> None:
    """The same rule measured on the imported module, which also catches memoisation.

    A table built from dotted names but memoising the module it resolved satisfies
    the source cases and fails here, so the two are not redundant. This one only
    reads, so it runs in process.

    For a baseline module the assertion is the tighter one available: whatever it holds
    at runtime must be confined to the tables its own source declares. A second,
    undeclared store appearing inside a module that is already listed is a new defect
    the baseline does not cover, and it reddens here.
    """
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        pytest.fail(
            f"{name} mirrors a surface but does not import: {type(exc).__name__}: {exc}. "
            "The runtime half of this rule cannot be measured on a module that will not "
            "load, and an unmeasurable module is a red rather than a pass."
        )
    offenders = {
        attr: sorted(key for key, val in value.items() if isinstance(val, ModuleType))
        for attr, value in list(vars(module).items())
        if isinstance(value, dict) and any(isinstance(val, ModuleType) for val in value.values())
    }
    if name in _KNOWN_RESOLVED_OWNER_MIRRORS:
        declared = set(owner_module_tables(ast.parse(text)))
        undeclared = sorted(set(offenders) - declared)
        assert undeclared == [], (
            f"{name} holds resolved owner modules in {undeclared}, which its source does "
            f"not declare as an owner table (it declares {sorted(declared)}). The "
            "baseline covers the declared table only."
        )
        return
    assert offenders == {}, (
        f"{name} holds resolved owner modules at runtime, so a purged owner stays "
        "invisible through it: " + repr({attr: keys[:3] for attr, keys in offenders.items()})
    )


def _parent_attribute_divergences() -> set[str]:
    """Submodules whose parent-package attribute is not the object ``sys.modules`` holds.

    The same one-storage question this rule asks of a mirroring module, asked of the
    interpreter: ``importlib`` finishes a load with ``setattr(parent, child, module)``,
    so a purge and reimport that puts back only the ``sys.modules`` entry leaves the
    parent naming the discarded module. That is a second reference to a value the
    authoritative store has moved past.
    """
    diverged: set[str] = set()
    for name, _text in MIRRORING:
        module = sys.modules.get(name)
        if module is None:
            continue
        prefix = f"{name}."
        for loaded_name, loaded in list(sys.modules.items()):
            if not loaded_name.startswith(prefix):
                continue
            leaf = loaded_name[len(prefix) :]
            if "." in leaf:
                continue
            bound = getattr(module, leaf, None)
            if isinstance(bound, ModuleType) and bound is not loaded:
                diverged.add(f"{name}.{leaf} is not sys.modules[{loaded_name!r}]")
    return diverged


def _sentinel_sightings() -> set[str]:
    """Places holding the probe's sentinel, which only the probe ever writes."""
    seen: set[str] = set()
    for name, _text in MIRRORING:
        for candidate_name, candidate in list(sys.modules.items()):
            if candidate_name != name and not candidate_name.startswith(f"{name}."):
                continue
            for attr, value in list(vars(candidate).items()):
                if isinstance(value, str) and value == SENTINEL:
                    seen.add(f"{candidate_name}.{attr}")
    return seen


def test_the_probe_leaves_this_process_untouched(
    divergence_baseline: set[str], probe_verdicts: dict[str, dict[str, object]]
) -> None:
    """The rule turned on this file's own fixture, which is where it is easiest to break.

    A probe that purged in process would have to put back the ``sys.modules`` entry,
    the parent package's attribute and any memo the mirror keeps; missing one leaves
    the split-brain the rule forbids, for whatever test runs next in this worker.
    Running the probe in a child process is what removes that obligation, and this
    case is what proves the obligation is really gone rather than merely intended.

    Baselined rather than absolute: another file's fixture may already have left a
    divergence in this worker, and that is not this probe's doing. Only a divergence
    the probe ADDS fails here. The sentinel needs no baseline, since nothing else
    writes it. Both come from fixtures, which is what guarantees the baseline predates
    the child no matter which case pulls the probe in first.
    """
    assert probe_verdicts, "the probe child returned nothing, so this case measured nothing"

    introduced = _parent_attribute_divergences() - divergence_baseline
    assert introduced == set(), (
        "running the purge probe left this process holding a parent attribute that "
        f"disagrees with sys.modules: {sorted(introduced)}"
    )
    sightings = _sentinel_sightings()
    assert (
        sightings == set()
    ), f"the probe's sentinel value is still reachable in this process: {sorted(sightings)}"


#: Bound on the probe child. It imports the package under test and nothing else, so a
#: run this long means it is blocked rather than slow. ``source_corpus`` bounds its own
#: child the same way, for the same reason: a hung child must cost one test, not the run.
#:
#: The value is DERIVED from the suite-wide ``--timeout`` in ``setup.cfg`` and must stay
#: under it. pytest-timeout arms at item setup, before the module-scoped fixture below
#: spawns the child, so a bound equal to that ceiling can never be reached as a readable
#: failure: the suite deadline fires first and takes the worker with it, which is a lost
#: run rather than one failed test. The relationship is asserted, not trusted, by
#: ``test_the_probe_bound_sits_under_the_suite_ceiling``, which reads the real ceiling.
_PROBE_TIMEOUT_SECONDS = 60


def _run_probe_child(workdir: Path) -> dict[str, dict[str, object]]:
    """Run the purge probe for every mirroring module in ONE child process.

    One child rather than one per module, because the probe's cost is the import of
    the package under test. Nothing is restored afterwards: the residue of a purge and
    reimport -- the ``sys.modules`` entry, the parent package's attribute, any memo the
    mirror keeps -- exits with the child.

    The child runs in *workdir*, not the checkout: importing seven packages and every
    owner submodule the discovery rule reaches is not a read-only act, and a relative
    path written during one of those imports would land in the repository.
    """
    if not MIRRORING_IDS:  # pragma: no cover - the empty scan has its own case
        return {}
    test_dir = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(test_dir.parent / "src"), str(test_dir), env.get("PYTHONPATH", "")]
    )
    argv = [sys.executable, str(test_dir / "mirrored_owner_probe.py"), *MIRRORING_IDS]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=True,
            env=env,
            cwd=str(workdir),
            timeout=_PROBE_TIMEOUT_SECONDS,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"the purge probe child did not finish within {_PROBE_TIMEOUT_SECONDS}s while "
            f"importing {MIRRORING_IDS}; it is blocked rather than slow"
        )
    verdicts = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    return {str(verdict["module"]): verdict for verdict in verdicts}


@pytest.fixture(scope="module")
def divergence_baseline() -> set[str]:
    """Parent-attribute divergences present BEFORE any probe child has run.

    A fixture rather than a call inside a test, because that is what orders it: the
    probe fixture below depends on this one, so the baseline is taken before the child
    runs no matter which test pulls the probe in first. Read inside a test instead, it
    would be captured after the child on every ordering but one, and the case that
    compares against it would quietly measure nothing.
    """
    return _parent_attribute_divergences()


@pytest.fixture(scope="module")
def probe_verdicts(
    divergence_baseline: set[str], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, dict[str, object]]:
    """The one probe child's verdicts, keyed by module name."""
    assert divergence_baseline is not None
    return _run_probe_child(tmp_path_factory.mktemp("mirrored-owner-probe"))


def _verdict(verdicts: dict[str, dict[str, object]], name: str) -> dict[str, object]:
    verdict = verdicts.get(name)
    if verdict is None:
        pytest.fail(f"the probe child returned no verdict for {name}")
    return verdict


def _measurable(verdict: dict[str, object], name: str) -> None:
    """Fail when the probe could not measure *name* at all.

    An unmeasurable module is a red, not a pass. A module that will not import, or
    whose table spelling the discovery rule does not recognise, leaves the purge
    assertions with nothing to evaluate -- and an assertion that cannot fail is the
    defect this rule is about, worn on the test instead of the code. The remedy is to
    widen the discovery rule, which has its own both-ways case above.
    """
    reason = verdict.get("unmeasurable")
    if reason is not None:
        pytest.fail(
            f"the purge probe could not measure {name}: {reason}. The purge half of the "
            "one-storage rule is unverified for this module until the probe can reach it."
        )


@pytest.mark.parametrize(("name", "text"), COMPLIANT, ids=COMPLIANT_IDS)
def test_a_read_through_the_mirror_resolves_the_owner_in_sys_modules(
    name: str, text: str, probe_verdicts: dict[str, dict[str, object]]
) -> None:
    """A read answers from the module ``sys.modules`` holds, not from an earlier one.

    Resolving only the OWNER through the import system while the VALUE stays bound
    in the mirroring module's own namespace is not half of this rule -- it is its own
    defect. A read then returns the binding made before a purge while a write
    resolves the module ``sys.modules`` now holds, and ``monkeypatch`` restores by
    reassignment: it reads the attribute to remember it, then assigns the remembered
    value back. Teardown therefore installs a pre-purge value into the fresh module,
    for the life of the worker.

    Scoped to the modules the rule binds with no exception. Whether a baseline module
    answers a read from the fresh owner depends on whether anything has populated its
    table yet, which varies with what else the worker imported, so there is no stable
    direction to assert for one; a baseline module is held to the source and runtime
    cases above, and gains this one on the commit that de-lists it.
    """
    verdict = _verdict(probe_verdicts, name)
    _measurable(verdict, name)
    assert verdict.get("reimport_is_new") is True, (
        f"{name}: the probe's reimport returned the same object, so it measured "
        "nothing -- there was no purge to see through"
    )
    assert verdict.get("read_resolves") is True, (
        f"reading {name}.{verdict.get('attribute')} did not answer from the "
        f"{verdict.get('owner')} that sys.modules holds (got {verdict.get('read')!r}), so a "
        "read and a write through this module name different objects and a patch "
        "fixture's teardown writes a pre-purge value into the fresh module"
    )


@pytest.mark.parametrize(("name", "text"), MIRRORING, ids=MIRRORING_IDS)
def test_a_purged_owner_is_not_retained_by_an_unlisted_mapping(
    name: str, text: str, probe_verdicts: dict[str, dict[str, object]]
) -> None:
    """No mapping keeps the discarded owner once it has been replaced.

    For a baseline module the assertion is the same tightening the runtime case makes:
    it may retain the discarded owner only in a table its own source declares, so a
    purge leaking through some other mapping reddens even inside a listed module.
    """
    verdict = _verdict(probe_verdicts, name)
    _measurable(verdict, name)
    raw = verdict.get("retained")
    retained = sorted(str(entry) for entry in raw) if isinstance(raw, list) else None
    assert retained is not None, (
        f"the probe returned no retained list for {name}, so this case measured nothing; "
        f"its verdict was {verdict!r}"
    )
    if name in _KNOWN_RESOLVED_OWNER_MIRRORS:
        declared = set(owner_module_tables(ast.parse(text)))
        undeclared = sorted(set(retained) - declared)
        assert undeclared == [], (
            f"{name} keeps the discarded {verdict.get('owner')} in {undeclared}, which its "
            f"source does not declare as an owner table (it declares {sorted(declared)})"
        )
        return
    assert retained == [], (
        f"{name} keeps the discarded {verdict.get('owner')} in "
        f"{retained} after a purge and reimport, so reads and writes "
        "through it reach a module nothing else sees"
    )
