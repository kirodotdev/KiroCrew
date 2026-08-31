"""GATE — the agent-SDK boundary is declared, classified, and shrink-only.

``scripts/check_agent_sdk_boundary.py`` refuses new imports of the ACP layer from
application code. This test pins the three properties that make that gate
trustworthy rather than decorative, in the shape the sibling architecture gates
use (``test_messaging_import_purity.py``, ``test_workflows_architecture.py``):

1. **The exempt set is exactly the boundary.** Only ``agent_sdk/``, ``acp/`` and
   ``providers/`` may reach the ACP layer, the set is pinned here, and each
   prefix must name a directory that exists. Widening the boundary fails a test
   rather than passing quietly.
2. **The recorded violations still exist.** A baseline entry that has been paid
   off must be pruned, or the list stops shrinking and starts lying.
3. **The scan cannot be walked around.** ``TYPE_CHECKING``-only imports,
   relative imports, ``importlib.import_module`` and a bound name that IS the
   forbidden package (``from kiro_crew import acp``) are all in scope, and the
   probes below fail if any of them stops being caught.

If this goes RED you either added an ACP import outside the boundary or paid one
off without recording it. Fix the import direction or run
``--update-baseline`` — do not relax the rule.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_agent_sdk_boundary.py"
BASELINE = ROOT / ".github" / "agent-sdk-boundary-baseline.txt"


def _gate():
    spec = importlib.util.spec_from_file_location("check_agent_sdk_boundary", GATE)
    assert spec is not None and spec.loader is not None, f"cannot load {GATE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _gate()


def test_the_sdk_package_exists_and_is_exempt(gate):
    """The boundary cannot be enforced before the package it names exists."""
    assert (ROOT / "src" / "kiro_crew" / "agent_sdk" / "__init__.py").is_file()
    assert gate._is_exempt("src/kiro_crew/agent_sdk/drivers/acp.py")


def test_the_exempt_set_is_exactly_the_boundary(gate):
    """A fourth exemption is someone widening the boundary; ratchet the set.

    Hand-listing all 230-odd top-level modules as "consumers" would rot on every
    new module and prove nothing. What is worth pinning is the SHORT side: the
    trees allowed to reach the ACP layer. providers/ is on it only until the
    RFC's final phase deletes the package.
    """
    assert set(gate.EXEMPT_PREFIXES) == {
        "src/kiro_crew/agent_sdk/",
        "src/kiro_crew/acp/",
        "src/kiro_crew/providers/",
    }


def test_every_exempt_prefix_points_at_a_real_tree(gate):
    """A typo'd or stale prefix exempts nothing, or exempts a tree that is gone."""
    missing = [p for p in gate.EXEMPT_PREFIXES if not (ROOT / p).is_dir()]
    assert not missing, f"exempt prefixes with no directory: {missing}"


def test_the_scan_actually_walks_the_tree(gate):
    """A broken walk must not read as a clean tree.

    The gate is shrink-only, so a scan that silently visited nothing would look
    like total success and invite a prune that deletes the whole baseline.
    """
    scanned = 0
    for path in (ROOT / "src").rglob("*.py"):
        if not gate._is_exempt(path.relative_to(ROOT).as_posix()):
            scanned += 1
    assert scanned > 500, f"only {scanned} consumer file(s) in scope; the walk is broken"


def test_the_forbidden_roots_include_the_re_export_channel(gate):
    """Watching only kiro_crew.acp would let providers launder ACP shapes.

    ``providers/base.py`` aliases ``acp.types.AcpEvent`` as ``LLMEvent`` and
    re-exports the ``EVENT_*`` constants, so a consumer can depend on the backend
    without naming it. If this drops to acp-only, the baseline can fall while
    nothing is actually decoupled.
    """
    assert "kiro_crew.acp" in gate.FORBIDDEN_ROOTS
    assert "kiro_crew.providers" in gate.FORBIDDEN_ROOTS


def test_the_baseline_header_comes_from_the_generator(gate):
    """The committed baseline must start with the generator's own HEADER.

    ``_write_baseline`` writes ``HEADER + body``, so the script is the source of
    truth for that prose and the committed file is only its output. Editing the
    file directly looks like it works and then silently reverts the moment
    anyone runs ``--update-baseline`` -- which is exactly how the "floor, not a
    countdown" rationale was lost once already.

    Keeping them equal is what lets a reader trust the committed header: it is
    the text every future refresh will reproduce.
    """
    committed = BASELINE.read_text(encoding="utf-8")
    assert committed.startswith(gate.HEADER), (
        "the committed baseline does not begin with scripts/"
        "check_agent_sdk_boundary.py's HEADER -- put the prose in HEADER and "
        "regenerate with --update-baseline, rather than editing the artifact"
    )


def test_every_recorded_violation_still_exists(gate):
    """A paid-off entry must be pruned; a stale baseline stops shrinking."""
    baseline = gate._read_baseline(BASELINE)
    current = {rel: len(hits) for rel, hits in gate._scan(gate.DEFAULT_TARGETS).items()}
    stale = {
        rel: (recorded, current.get(rel, 0))
        for rel, recorded in baseline.items()
        if current.get(rel, 0) < recorded
    }
    assert not stale, (
        "these baseline entries are now lower than recorded; record the progress "
        "with `python3 scripts/check_agent_sdk_boundary.py --update-baseline`: "
        f"{stale}"
    )


def test_a_violation_outside_the_baseline_is_caught(gate):
    """The plain case: an ACP import in a file the baseline does not list."""
    hits = gate._violations_in_source(
        "src/kiro_crew/dashboard/brand_new.py",
        "from kiro_crew.acp.types import AcpEvent\n",
    )
    assert hits, "a fresh ACP import must be flagged"


def test_a_type_checking_only_import_is_still_refused(gate):
    """A type-only dependency is still boundary knowledge."""
    hits = gate._violations_in_source(
        "src/kiro_crew/dashboard/brand_new.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.acp.runtime import AcpRuntime\n",
    )
    assert hits, "a TYPE_CHECKING import must not escape the scan"


def test_a_dynamic_import_does_not_escape_the_scan(gate):
    hits = gate._violations_in_source(
        "src/kiro_crew/dashboard/brand_new.py",
        "import importlib\nimportlib.import_module('kiro_crew.acp.client')\n",
    )
    assert hits, "importlib.import_module with a literal must be flagged"


def test_a_relative_import_resolves_to_its_absolute_target(gate):
    """`from ..acp.types import x` inside kiro_crew is the same violation."""
    hits = gate._violations_in_source(
        "src/kiro_crew/dashboard/brand_new.py",
        "from ..acp.types import AcpEvent\n",
    )
    assert hits, "a relative ACP import must resolve and be flagged"


def test_a_neighbouring_name_is_not_a_violation(gate):
    """`kiro_crew.acp_backends` is a leaf that imports no ACP; prefix-matching it
    would flag four consumers for depending on the module that exists to keep
    them off the ACP package."""
    hits = gate._violations_in_source(
        "src/kiro_crew/session.py",
        "from kiro_crew.acp_backends import selectable_backends\n",
    )
    assert not hits, "a sibling module sharing the prefix must not be flagged"


def test_a_bound_name_that_is_the_forbidden_package_is_a_violation(gate):
    """`from kiro_crew import acp` binds the forbidden package by NAME.

    The from-target is `kiro_crew`, which matches no forbidden root, so a gate
    that reads only `node.module` misses it. With no inline opt-out marker this
    ordinary spelling would have been the standard way around the rule, with the
    baseline still shrinking while dependence stayed put.
    """
    rel = "src/kiro_crew/dashboard/probe.py"
    for source in (
        "from kiro_crew import acp\n",
        "from kiro_crew import providers\n",
        "from kiro_crew import acp as _a\n",
        "from .. import acp\n",
    ):
        assert gate._violations_in_source(rel, source), f"not flagged: {source!r}"


def test_a_bound_name_that_is_an_unrelated_sibling_is_clean(gate):
    """The rule must not widen to every `from kiro_crew import ...` line."""
    rel = "src/kiro_crew/dashboard/probe.py"
    for source in (
        "from kiro_crew import session\n",
        "from kiro_crew import config\n",
        "from kiro_crew import *\n",
    ):
        assert not gate._violations_in_source(rel, source), f"flagged: {source!r}"


def test_every_dynamic_import_spelling_is_a_violation(gate):
    """One case per argument form a dynamic import can carry the module in.

    A gate that reads only the first positional argument is bypassed by every
    other spelling, and a reviewer hands these out one per round -- so the whole
    branch table is asserted at once rather than the one that was reported.
    """
    rel = "src/kiro_crew/dashboard/probe.py"
    spellings = {
        "canonical attribute call": (
            "import importlib\nimportlib.import_module('kiro_crew.acp.client')\n"
        ),
        "canonical dunder": "__import__('kiro_crew.acp.client')\n",
        "module alias": ("import importlib as _il\n_il.import_module('kiro_crew.acp.types')\n"),
        "bare from-import": (
            "from importlib import import_module\nimport_module('kiro_crew.acp')\n"
        ),
        "renamed from-import": (
            "from importlib import import_module as _im\n_im('kiro_crew.acp')\n"
        ),
        "bound by assignment": (
            "import importlib\n_im = importlib.import_module\n_im('kiro_crew.providers')\n"
        ),
        "keyword name": ("import importlib\nimportlib.import_module(name='kiro_crew.acp')\n"),
        "dunder fromlist tuple": "__import__('kiro_crew', fromlist=('acp',))\n",
        "dunder fromlist list": "__import__('kiro_crew', fromlist=['providers'])\n",
        "dunder fromlist set": "__import__('kiro_crew', fromlist={'acp'})\n",
        "dunder fromlist dict keys": ("__import__('kiro_crew', fromlist={'acp': True})\n"),
        "relative via package": (
            "import importlib\nimportlib.import_module('.acp', package='kiro_crew')\n"
        ),
        "relative via level": "__import__('acp', level=2)\n",
    }
    missed = [
        label for label, source in spellings.items() if not gate._violations_in_source(rel, source)
    ]
    assert not missed, f"dynamic-import spellings that escape the gate: {missed}"


def test_a_positional_package_resolves_a_relative_dynamic_import(gate):
    """`package` is argument 1, so reading only the keyword form is a false green.

    With `package` unread, `import_module(".acp", "kiro_crew")` fell through to
    resolving against the CALLING file's package. From a nested consumer that
    lands on `kiro_crew.dashboard.acp` -- not a forbidden root -- so the file read
    as clean while importing `kiro_crew.acp`.
    """
    rel = "src/kiro_crew/dashboard/probe.py"
    flagged = {
        "positional package": ("import importlib\nimportlib.import_module('.acp', 'kiro_crew')\n"),
        "positional package, two dots": (
            "import importlib\n" "importlib.import_module('..providers', 'kiro_crew.dashboard')\n"
        ),
        "keyword package": (
            "import importlib\nimportlib.import_module('.acp', package='kiro_crew')\n"
        ),
    }
    for label, source in flagged.items():
        assert gate._violations_in_source(rel, source), f"{label} was not flagged"

    clean = {
        "positional package on a sibling": (
            "import importlib\nimportlib.import_module('.userns', 'kiro_crew.sandbox')\n"
        ),
        # `__import__`'s slot 1 is `globals`, not `package`. A dict literal is not
        # a string constant, so reading that slot cannot invent a package name.
        "dunder globals in slot 1 is not a package": ("__import__('.acp', {}, {}, ('x',), 0)\n"),
    }
    for label, source in clean.items():
        assert not gate._violations_in_source(rel, source), f"{label} was flagged"


def test_dynamic_import_of_an_unrelated_module_is_clean(gate):
    """The dynamic branch must not widen to every importlib call."""
    rel = "src/kiro_crew/dashboard/probe.py"
    clean = {
        "unrelated module": ("import importlib\nimportlib.import_module('kiro_crew.session')\n"),
        "unrelated fromlist": "__import__('kiro_crew', fromlist=('session',))\n",
        "star fromlist": "__import__('kiro_crew', fromlist=('*',))\n",
        "fromlist bare string": "__import__('kiro_crew', fromlist='acp')\n",
        "non-literal target": (
            "import importlib\nmod = 'kiro_crew.acp'\nimportlib.import_module(mod)\n"
        ),
    }
    flagged = [label for label, source in clean.items() if gate._violations_in_source(rel, source)]
    assert not flagged, f"clean dynamic imports wrongly flagged: {flagged}"


def test_an_undecidable_fromlist_under_the_boundary_is_a_violation(gate):
    """The invariant, not a list of container shapes.

    Three review rounds each closed one `fromlist` shape and revealed the next --
    tuple/list, then set and dict keys, then starred unpacking, dict-unpack and
    literal concatenation. Enumerating shapes cannot converge: `('acp',) * 1`,
    `('a' + 'cp',)`, `frozenset({'acp'})` and a comprehension all import the same
    package. So the rule is inverted: when `name` is an ancestor of a forbidden
    root, CLEAN requires proving the `fromlist` names none of it, and anything the
    scanner cannot fully read is reported.
    """
    rel = "src/kiro_crew/dashboard/probe.py"
    undecidable = {
        "starred": "x = ['acp']\n__import__('kiro_crew', fromlist=(*x,))\n",
        "dict unpack": "__import__('kiro_crew', fromlist={**{'acp': True}})\n",
        "concatenation": "__import__('kiro_crew', fromlist=('acp',) + ())\n",
        "repetition": "__import__('kiro_crew', fromlist=('acp',) * 1)\n",
        "opaque call": "__import__('kiro_crew', fromlist=frozenset({'acp'}))\n",
        "comprehension": "__import__('kiro_crew', fromlist=[m for m in ('acp',)])\n",
        "generator": "__import__('kiro_crew', fromlist=(m for m in ('acp',)))\n",
        "name reference": "f = ['acp']\n__import__('kiro_crew', fromlist=f)\n",
        "element concat": "__import__('kiro_crew', fromlist=('a' + 'cp',))\n",
    }
    missed = [
        label
        for label, source in undecidable.items()
        if not gate._violations_in_source(rel, source)
    ]
    assert not missed, f"an unreadable fromlist under the boundary passed: {missed}"


def test_the_invariant_does_not_flag_a_sibling_package(gate):
    """`kiro_crew.sandbox` is a sibling of the boundary, not an ancestor of it.

    The tree really does call `__import__('kiro_crew.sandbox',
    fromlist=['userns_available'])`, so a rule that reported every opaque
    `fromlist` regardless of the package would red the gate on existing code.
    """
    rel = "src/kiro_crew/dashboard/probe.py"
    clean = {
        "real in-tree usage": ("__import__('kiro_crew.sandbox', fromlist=['userns_available'])\n"),
        "opaque under a sibling": ("f = compute()\n__import__('kiro_crew.sandbox', fromlist=f)\n"),
        "decidable and forbidden-free": ("__import__('kiro_crew', fromlist=('session',))\n"),
        "empty": "__import__('kiro_crew', fromlist=())\n",
        "non-strings only": "__import__('kiro_crew', fromlist=(1, 2))\n",
        "bare string iterates characters": ("__import__('kiro_crew', fromlist='acp')\n"),
    }
    flagged = [label for label, source in clean.items() if gate._violations_in_source(rel, source)]
    assert not flagged, f"the invariant is too wide: {flagged}"


def test_an_empty_module_name_is_a_relative_import_not_a_missing_one(gate):
    """`__import__("")` with a level is how a purely relative import spells itself.

    `_str_const` returns `""` for it, which is falsy but a legal name, so chaining
    the positional and keyword lookups with `or` treated it as "no name given" and
    returned before the relative branch ran. Verified against the interpreter:
    `__import__("", globals(), locals(), ("acp",), 2)` from a module two levels
    under `kiro_crew` really imports `kiro_crew.acp`.
    """
    rel = "src/kiro_crew/dashboard/probe.py"
    relative = {
        "positional level": "__import__('', globals(), locals(), ('acp',), 2)\n",
        "keyword level": "__import__('', fromlist=('providers',), level=2)\n",
        "name by keyword": "__import__(name='', fromlist=('acp',), level=2)\n",
        "opaque fromlist": "f = compute()\n__import__('', fromlist=f, level=2)\n",
    }
    missed = [
        label for label, source in relative.items() if not gate._violations_in_source(rel, source)
    ]
    assert not missed, f"an empty-name relative import escaped: {missed}"


def test_an_empty_name_that_imports_nothing_stays_clean(gate):
    """Only the forms that actually reach a forbidden package are reported.

    `importlib.import_module("", package=...)` raises `ValueError: Empty module
    name` and imports nothing, and level 1 from `dashboard/` resolves to
    `kiro_crew.dashboard.acp`, which is not the boundary. Flagging either would be
    a false red on code that cannot cross.
    """
    rel = "src/kiro_crew/dashboard/probe.py"
    clean = {
        "import_module empty name raises": (
            "import importlib\nimportlib.import_module('', package='kiro_crew.acp')\n"
        ),
        "no level at all": "__import__('')\n",
        "level one lands in dashboard": ("__import__('', fromlist=('acp',), level=1)\n"),
        "unrelated fromlist": "__import__('', fromlist=('session',), level=2)\n",
        "no fromlist": "__import__('', level=2)\n",
    }
    flagged = [label for label, source in clean.items() if gate._violations_in_source(rel, source)]
    assert not flagged, f"a harmless empty-name import was flagged: {flagged}"


def test_the_self_test_passes(gate, capsys):
    """The gate's own probes must pass, so a dead rule fails here too."""
    assert gate._self_test() == 0
    assert "self-test passed" in capsys.readouterr().out


def test_seeding_refuses_to_overwrite_an_existing_baseline(gate):
    """Re-seeding is the laundering move the missing-file error exists to stop."""
    with pytest.raises(SystemExit) as excinfo:
        gate.seed_baseline(BASELINE)
    assert "already exists" in str(excinfo.value)


def test_seeding_outside_the_checkout_reports_the_file_it_wrote(gate, tmp_path, capsys):
    """`--baseline` takes any path, so the success line must not assume repo-relative.

    The seed wrote the file and THEN rendered its name with `relative_to(ROOT)`,
    which raises for a target outside the checkout: the baseline existed on disk
    but the command died with a bare ValueError and a non-zero exit.
    """
    target = tmp_path / "outside-the-checkout.txt"
    assert gate.seed_baseline(target) == 0
    assert target.is_file() and target.read_text(encoding="utf-8").strip()
    assert str(target) in capsys.readouterr().out


def test_growth_in_an_untouched_file_is_not_this_prs_problem(gate):
    """Every verdict is scope-gated, so no PR is failed for a file it never touched.

    `grown` was the one verdict missing the `in_scope` guard its siblings have,
    which made an edge landing anywhere in the tree fail the NEXT unrelated PR --
    with no fix available inside that PR's own diff.
    """
    rel = "src/kiro_crew/dashboard/untouched.py"
    violations = {rel: {10: "kiro_crew.acp", 11: "kiro_crew.acp"}}
    baseline = {rel: 1}

    _, grown, added_line_offenders, _ = gate._verdicts(
        violations, baseline, {"src/kiro_crew/somewhere/else.py"}, {}
    )
    assert grown == [], "an untouched file's growth was charged to this PR"
    assert added_line_offenders == {}, "and it must not resurface as an added-line error"

    _, grown_when_touched, _, _ = gate._verdicts(violations, baseline, {rel}, {})
    assert grown_when_touched == [rel], "the PR that grew the file must still be failed"


def test_every_verdict_is_scope_gated(gate):
    """Guard the shape, not just the two cases: no verdict may ignore `changed`."""
    rels = {
        "new": "src/kiro_crew/dashboard/brand_new.py",
        "grown": "src/kiro_crew/dashboard/grew.py",
        "shrunk": "src/kiro_crew/dashboard/shrank.py",
    }
    violations = {
        rels["new"]: {1: "kiro_crew.acp"},
        rels["grown"]: {1: "kiro_crew.acp", 2: "kiro_crew.acp"},
        rels["shrunk"]: {1: "kiro_crew.acp"},
    }
    baseline = {rels["grown"]: 1, rels["shrunk"]: 2}

    new_offenders, grown, added_line_offenders, shrunk = gate._verdicts(
        violations, baseline, set(), {}
    )
    assert (new_offenders, grown, added_line_offenders, shrunk) == (
        [],
        [],
        {},
        [],
    ), "an empty scope must produce no verdicts at all"

    new_offenders, grown, _, shrunk = gate._verdicts(violations, baseline, None, None)
    assert new_offenders == [rels["new"]]
    assert grown == [rels["grown"]]
    assert shrunk == [rels["shrunk"]]
