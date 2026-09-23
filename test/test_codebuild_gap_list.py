"""Audit the self-hosted-runner gap list: which list is SELECTED, and how it READS.

Two separable things can go wrong, and neither shows up in a normal run.

**Selection.** The list is keyed on the runner rather than the OS, so the predicate
is the whole safety argument: applied too widely it hides a real failure on every
hosted Linux shard, and applied too narrowly it hides nothing and the shard stays
red. The predicate is one environment variable, so it is cheap to pin exactly --
and it must be pinned, because nothing else in the suite fails when it drifts.

**Reading.** The entries are matched by the shared ``_apply_tracked_gap_list``
matcher, which spells a plain entry and a parametrized entry differently: a line
without ``[`` is compared against the param-stripped node id, and a line with ``[``
against the id with its params intact. One of these three entries names a single
parametrization whose siblings PASS on that runner, so getting that spelling wrong
either un-tracks the failing case or marks the passing ones and reds the job with
XPASS forever. So the entries are resolved here against real collected node ids.

Both audits run on every platform, so they execute on the hosted matrix where the
list itself never applies -- the same argument ``test_windows_gap_list`` makes for
auditing a list from outside the environment it protects.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LISTFILE = Path(__file__).with_name("codebuild-expected-failures.txt")

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _rootdir_conftest():
    """The rootdir ``conftest.py`` as a module, loaded by path.

    It is not importable as ``conftest`` from here -- ``test/conftest.py`` owns that
    name in this directory -- and pytest's own plugin instance is not reachable by a
    stable attribute, so the file is loaded under a private name instead.
    """
    spec = importlib.util.spec_from_file_location(
        "_rootdir_conftest_under_audit", _REPO_ROOT / "conftest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entries() -> list[str]:
    text = _LISTFILE.read_text(encoding="utf-8")
    found = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert found, (
        f"read no entries from {_LISTFILE.name} -- this audit has gone blind and would "
        "pass no matter what the list contained"
    )
    return found


# --- selection -------------------------------------------------------------


@pytest.mark.parametrize(
    "value, applies",
    [
        ("self-hosted", True),
        ("github-hosted", False),
        ("", False),
        ("Self-Hosted", False),
        ("self-hosted-linux", False),
    ],
)
def test_the_list_applies_only_on_a_self_hosted_runner(monkeypatch, value, applies):
    """Exactly one value of ``RUNNER_ENVIRONMENT`` selects this list.

    The near-misses are the point: a case-folded or prefixed match would make the
    predicate true on runners GitHub does not call self-hosted, and every hosted
    Linux shard would then stop reporting these three failures for real.
    """
    module = _rootdir_conftest()
    monkeypatch.setenv("RUNNER_ENVIRONMENT", value)
    assert module.on_self_hosted_runner() is applies


def test_an_absent_runner_environment_applies_no_list(monkeypatch):
    """A developer machine has none of the three constraints, so it gets no marks."""
    module = _rootdir_conftest()
    monkeypatch.delenv("RUNNER_ENVIRONMENT", raising=False)
    assert module.on_self_hosted_runner() is False


# --- reading ---------------------------------------------------------------


def _defs_in(path: Path) -> tuple[set[str], dict[str, ast.AST]]:
    """Every ``Class::function`` and bare ``function`` name defined in *path*.

    Resolved by AST, the same choice ``test_windows_gap_list`` makes and for a second
    reason here: collecting these files in a SUBPROCESS cannot be made portable. The
    suite points each test's ``KIROCREW_HOME`` under a scratch tree that its own
    rootdir guard refuses as a child of the live data home, so a child pytest started
    with this session's environment exits before it collects anything.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    functions: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, _DEFS):
            names.add(node.name)
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, _DEFS):
                    names.add(f"{node.name}::{member.name}")
                    functions[f"{node.name}::{member.name}"] = member
    return names, functions


def _parametrize_decorators(fn: ast.AST) -> list[ast.Call]:
    found = []
    for dec in getattr(fn, "decorator_list", []):
        if isinstance(dec, ast.Call) and "parametrize" in ast.unparse(dec.func):
            found.append(dec)
    return found


def test_every_entry_names_a_test_that_exists():
    """A line matching no test is inert, and the entry is then doing nothing.

    Under a strict xfail an inert entry does show up -- the shard stays red -- but it
    shows up as the failure the entry was supposed to explain, which is the slowest
    possible way to learn a node id has a typo in it.
    """
    for entry in _entries():
        relpath, _, tail = entry.partition("::")
        assert tail, f"{entry} names a file with no test in it"
        path = _REPO_ROOT / relpath
        assert path.is_file(), f"{entry} names a file that does not exist"
        names, _ = _defs_in(path)
        target = tail.split("[")[0]
        assert target in names, f"{entry} names no test defined in {relpath}"


def test_a_parametrized_entry_names_a_test_that_is_parametrized():
    """A ``[...]`` suffix on a test taking no parameters would match nothing at all.

    The matcher compares a line WITH ``[`` against the node id with its params intact,
    so the suffix has to be a real parametrization rather than decoration. Checked
    here because the entry that needs this spelling has SIBLING parametrizations that
    pass on that runner: a bare node id would cover them too and red the job with
    XPASS on every one.
    """
    parametrized = [entry for entry in _entries() if "[" in entry]
    assert parametrized, "the list no longer exercises the parametrized spelling"

    for entry in parametrized:
        relpath, _, tail = entry.partition("::")
        target, _, params = tail.partition("[")
        params = params.rstrip("]")
        _, functions = _defs_in(_REPO_ROOT / relpath)
        decorators = _parametrize_decorators(functions[target])
        assert decorators, f"{entry} names a parametrization of an unparametrized test"

        source = " ".join(ast.unparse(dec) for dec in decorators)
        # The last segment of a param id is the last argument's value, and a string
        # value is carried into the id verbatim -- so it is checkable without
        # re-deriving pytest's whole id algorithm.
        assert f'"{params.rsplit("-", 1)[-1]}"' in source or (
            f"'{params.rsplit('-', 1)[-1]}'" in source
        ), f"{entry} names a parametrization whose value is not in the decorator"

        # Siblings must exist, or the narrow spelling buys nothing over a bare id.
        cases = sum(
            len(arg.elts) for dec in decorators for arg in dec.args if isinstance(arg, ast.List)
        )
        assert cases > 1, f"{entry} has no sibling parametrization, so the spelling is moot"


def test_comments_and_blank_lines_are_not_entries():
    """The header is prose, and a reason line above each entry is prose too."""
    raw = _LISTFILE.read_text(encoding="utf-8").splitlines()
    assert any(ln.startswith("#") for ln in raw), "the list carries no header to skip"
    assert any(not ln.strip() for ln in raw), "the list has no blank line to skip"
    for entry in _entries():
        assert not entry.startswith("#")
        assert entry.startswith("test/") and "::" in entry


def test_the_matcher_spells_the_two_entry_kinds_apart():
    """A bare line is matched param-stripped; a ``[...]`` line is matched with params.

    The two sets the matcher builds are what make a single parametrization
    expressible, so the spelling rule is pinned here against the matcher's own
    helpers rather than restated in prose.
    """
    module = _rootdir_conftest()
    bare = "test/test_thing.py::TestThing::test_a_thing"
    exact = f"{bare}[10-::1]"

    assert module._base_nodeid(exact) == bare
    assert module._ungrouped_nodeid(exact) == exact
    # The xdist group suffix is not part of a test's identity, on either spelling.
    assert module._base_nodeid(f"{exact}@grp") == bare
    assert module._ungrouped_nodeid(f"{exact}@grp") == exact
