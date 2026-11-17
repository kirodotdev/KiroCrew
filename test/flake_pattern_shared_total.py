"""The K21 shared-total-pin detector of ``test_flake_pattern_ratchet.py``.

A test that walks the product tree and pins what it finds to ONE number breaks
``main`` when two changes each add a site: both rewrite the number to the same
value, git merges that cleanly, and the merged tree holds one site more
(testing-conventions § Merge-safe ratchets and counts). The detector takes the
ratchet's parsed module (``tree``, ``asserts``, ``calls``, ``parents``,
``enclosing_function``); the ratchet owns counting and runs the cases kept here.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

#: Text that shows a module walks a file tree, and text that shows the tree is the
#: product's. K21 reads a module only when it holds one of each.
_TREE_WALKS = re.compile(
    r"source_corpus|glob\(|os\.walk\(|ls-files|iter_modules\(|walk_packages\(|_helpers import"
)
_PRODUCT_TREE = re.compile(
    r"source_corpus|repo_root|src_root|\b_*SRC\w*\b|['\"](src|kiro_crew)['\"]|__path__|ls-files"
)
#: Words one of which every module :data:`_TREE_WALKS` matches holds (``PY_TRIGGERS``).
K21_TRIGGERS = ("source_corpus", "glob", "walk", "ls-files", "iter_modules", "_helpers")
#: A module constant whose name says it holds a total: ``_BASELINE_SITES``, ``EXPECTED_ROWS``.
_TOTAL_NAME = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
_TOTAL_WORDS = ("BASELINE", "COUNT", "TOTAL", "EXPECTED", "ROWS")
#: A name holding a count or total; a mock's own call tally is not one.
_COUNT_NAME = re.compile(r"^(?!.*(call|await)_count$).*(count|total)", re.IGNORECASE)
#: Below this a literal pin is a fixture's own size, not a total of the tree.
MIN_PINNED_TOTAL = 10
#: Calls that walk a tree, beside ``os.walk``: ``Path.rglob``, ``glob.glob``, ``pkgutil``'s.
_WALK_CALLS = frozenset({"rglob", "glob", "iglob", "iter_modules", "walk_packages"})
#: ``source_corpus`` helpers that name a root or walk an AST, not the tree.
_CORPUS_NON_WALKS = frozenset({"repo_root", "src_root", "iter_statements"})
#: A name imported from a sibling test helper that reads source files: the walk sits in
#: that helper, so a test that reads the name reads the walk (``_owner_sources``).
_HELPER_READER = re.compile(r"source|files|scan|corpus", re.IGNORECASE)
#: A walk of the test's own scratch directory is not a walk of the product tree.
_TMP_ROOT = re.compile(r"^\(*(tmp_path|tmpdir|tmp|tmp_path_factory)\b")
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _int(node: ast.AST | None) -> int | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    return None


def _root_name(node: ast.AST) -> str | None:
    """The name an expression such as ``(sessions / 'archive').glob`` starts from."""
    while True:
        if isinstance(node, ast.BinOp):
            node = node.left
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            return node.id if isinstance(node, ast.Name) else None


def _counts_something(node: ast.AST) -> bool:
    """``len(...)``, ``sum(...)``, ``<x>.count(...)``, or a name holding a count or total."""
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        return name in ("len", "sum") or name.endswith(".count")
    while isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return bool(_COUNT_NAME.search(node.id))
    if isinstance(node, ast.Attribute):
        return bool(_COUNT_NAME.search(node.attr))
    return False


def _tree_scanners(tree: ast.Module) -> frozenset[str]:
    """Module-level functions, methods and names whose value comes from a walk of a
    tree: one that calls a walk (:data:`_WALK_CALLS`, ``os.walk``) outside the test's
    scratch directory or a directory passed in as a parameter, runs ``git ls-files``,
    calls a ``source_corpus`` helper that
    reads the tree or a ``*_helpers`` module's source reader (:data:`_HELPER_READER`),
    or reads a function or name that does."""
    corpus = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "source_corpus"
        for alias in node.names
        if alias.name not in _CORPUS_NON_WALKS
    }
    if any(
        isinstance(node, ast.Import) and any(a.name == "source_corpus" for a in node.names)
        for node in tree.body
    ):
        corpus.add("source_corpus")
    corpus |= {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and not node.level
        and node.module
        and node.module.split(".")[0] not in ("kiro_crew", "source_corpus")
        and node.module.endswith("_helpers")
        for alias in node.names
        if _HELPER_READER.search(alias.name)
    }
    bodies: dict[str, ast.AST] = {}
    for node in tree.body:
        for member in node.body if isinstance(node, ast.ClassDef) else [node]:
            if isinstance(member, _FUNCS):
                bodies[member.name] = member
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Name):
                    bodies[target.id] = node.value

    module_names = {name for name, body in bodies.items() if not isinstance(body, _FUNCS)}

    def params(body: ast.AST) -> set[str]:
        if not isinstance(body, _FUNCS):
            return set()
        a = body.args
        return {arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)}

    def walk_calls(body: ast.AST) -> Iterable[ast.AST]:
        """The root expression of each tree walk in ``body`` outside the scratch dir."""
        for node in ast.walk(body):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                func = node.func
                if func.attr in _WALK_CALLS or ast.unparse(func) == "os.walk":
                    if _TMP_ROOT.match(ast.unparse(func.value)):
                        continue
                    os_walk = ast.unparse(func) == "os.walk" and node.args
                    yield node.args[0] if os_walk else func.value

    # A helper whose walk is rooted at its own parameter walks whatever directory the
    # caller hands it: the product tree when a caller passes a module-level root
    # (``_scan_tree(_SRC)``), a seeded home or scratch dir when it passes a local or
    # its own parameter (``_segments(home / 'sessions')``).
    param_walkers = {
        name
        for name, body in bodies.items()
        if any(_root_name(root) in params(body) for root in walk_calls(body))
    }

    def walks(body: ast.AST, known: set[str]) -> bool:
        own = params(body)
        if any(_root_name(root) not in own for root in walk_calls(body)):
            return True
        for node in ast.walk(body):
            if isinstance(node, ast.Constant) and node.value == "ls-files":
                return True
            if isinstance(node, ast.Name) and (node.id in corpus or node.id in known):
                return True
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in param_walkers
                and any(_root_name(arg) in module_names for arg in node.args)
            ):
                return True
        return False

    found: set[str] = set()
    while True:
        more = {name for name, body in bodies.items() if name not in found and walks(body, found)}
        if not more:
            return frozenset(found)
        found |= more


def _pinned_totals(tree: ast.Module) -> frozenset[str]:
    """Module-level ALL-CAPS int constants named as a total, other than zero."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not _int(node.value):
            continue
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and _TOTAL_NAME.match(target.id)
                and any(word in target.id for word in _TOTAL_WORDS)
            ):
                names.add(target.id)
    return frozenset(names)


def k21_shared_total_pin(module, source: str) -> Iterable[ast.AST]:
    """A shared total pinned exactly in a module that walks the product tree, in a
    function that reads the walk (:func:`_tree_scanners`): an ``assert`` (or
    ``assertEqual``) holding a count ``==`` an int literal of at least
    :data:`MIN_PINNED_TOTAL`, or a count ``==`` / ``!=`` a module constant named as a
    total (:func:`_pinned_totals`). Known misses: a pin enforced as two one-sided
    ``if total > X`` / ``if total < X`` checks, a pin on a walk the test reaches
    through a module other than ``source_corpus`` or a ``*_helpers`` source reader,
    and a walk helper that takes its root as a parameter and is called with a
    local root (``_scan(root)`` after ``root = SRC``)."""
    if not (_TREE_WALKS.search(source) and _PRODUCT_TREE.search(source)):
        return
    constants = _pinned_totals(module.tree)
    scanners = _tree_scanners(module.tree)

    def scans(node: ast.AST) -> bool:
        outer = None
        current = module.enclosing_function(node)
        while current is not None:
            outer = current
            current = module.enclosing_function(current)
        return outer is not None and outer.name in scanners

    def literal_pin(left: ast.AST, op: ast.cmpop, right: ast.AST) -> bool:
        if not isinstance(op, ast.Eq):
            return False
        return any(
            (_int(pin) or 0) >= MIN_PINNED_TOTAL and _counts_something(count)
            for count, pin in ((left, right), (right, left))
        )

    def constant_pin(left: ast.AST, op: ast.cmpop, right: ast.AST) -> bool:
        if not isinstance(op, (ast.Eq, ast.NotEq)):
            return False
        for other, pin in ((left, right), (right, left)):
            if isinstance(pin, ast.Name) and pin.id in constants:
                return not isinstance(other, ast.Constant) and not (
                    isinstance(other, ast.Name) and other.id in constants
                )
        return False

    def pairs(node: ast.Compare):
        return zip([node.left, *node.comparators[:-1]], node.ops, node.comparators)

    asserted = {
        node
        for statement in module.asserts
        for node in ast.walk(statement.test)
        if isinstance(node, ast.Compare)
    }
    for node in module.parents:
        if isinstance(node, ast.Compare) and scans(node):
            if any(constant_pin(*pair) for pair in pairs(node)) or (
                node in asserted and any(literal_pin(*pair) for pair in pairs(node))
            ):
                yield node
    for call in module.calls:
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "assertEqual"
            and len(call.args) >= 2
            and literal_pin(call.args[0], ast.Eq(), call.args[1])
            and scans(call)
        ):
            yield call


#: (snippets the detector must count once each, a near miss it must not count),
#: checked by the ratchet's detector-case tests.
K21_CASES: tuple[tuple[str, ...], str] = (
    (
        "from source_corpus import repo_files\ndef test_x():\n"
        "    found = [p for p in repo_files() if p.suffix == '.py']\n"
        "    assert len(found) == 154\n",
        "from pathlib import Path\nSRC = Path(__file__).parents[1] / 'src'\n"
        "def _scan():\n    return list(SRC.rglob('*.py'))\n"
        "def test_x():\n    assert len(_scan()) == 37\n",
        "import os\n_BASELINE_SITES = 143\ndef test_x():\n"
        "    sites = [f for _r, _d, fs in os.walk('src') for f in fs]\n"
        "    assert len(sites) == _BASELINE_SITES\n",
        "import pkgutil\nimport kiro_crew\nEXPECTED_OWNERS = 9\ndef test_x():\n"
        "    owners = list(pkgutil.iter_modules(kiro_crew.__path__))\n"
        "    if len(owners) != EXPECTED_OWNERS:\n        raise AssertionError(owners)\n",
        "from source_corpus import source_texts\n_HOMES = {'a.py': 3}\ndef test_x():\n"
        "    assert {p.name: t.count('x') for p, t in source_texts()} == _HOMES\n"
        "    assert sum(_HOMES.values()) == 12\n",
        "import subprocess\nimport unittest\nclass T(unittest.TestCase):\n"
        "    def test_x(self):\n        out = subprocess.run(['git', 'ls-files', 'src'],"
        " capture_output=True, text=True).stdout\n"
        "        self.assertEqual(len(out.splitlines()), 812)\n",
        "from source_corpus import repo_files\n_FILES = repo_files()\n"
        "def test_x():\n    assert len(_FILES) == 2048\n",
        "from server_contract_helpers import _SRC, _owner_sources\ndef test_x():\n"
        "    found = {n for s in _owner_sources().values() for n in s.split()}\n"
        "    assert len(found) == 154\n",
        "from pathlib import Path\nSRC = Path(__file__).parents[1] / 'src'\n"
        "def _scan_tree(root):\n    return list(root.rglob('*.py'))\n"
        "def _scan():\n    return _scan_tree(SRC)\n"
        "def test_x():\n    assert len(_scan()) == 61\n",
    ),
    # A small literal, a zero pin, a ceiling, a constant not named as a total, a
    # walk of tmp_path, an ast.walk, a mock's call tally, a pin in a test that
    # reads no walk, and a walk of a directory the caller passes in (a seeded
    # home's archive), all in a module that walks src.
    "import ast\nfrom source_corpus import iter_statements, repo_files\n"
    "_BASELINE_SITES = 0\n_LIMIT = 50\ndef test_x(m):\n    found = list(repo_files())\n"
    "    assert len(found) == 3\n    assert len(found) == _BASELINE_SITES\n"
    "    assert len(found) <= 154\n    assert len(found) == _LIMIT\n"
    "    assert m.call_count == 16\n"
    "def test_t(tmp_path):\n    assert len(list(tmp_path.rglob('*'))) == 20\n"
    "def test_a(tree):\n    assert len(list(ast.walk(tree))) == 23\n"
    "    assert len(list(iter_statements(tree))) == 23\n"
    "def test_k(key):\n    assert len(key) == 32\n"
    "def _segments(sessions):\n    return sorted((sessions / 'archive').glob('*.jsonl'))\n"
    "def test_s(home):\n    assert len(_segments(home)) == 120\n",
)

#: A walk of a fixture directory with nothing naming the repo's tree is a test's
#: own data, so its pinned size is not a shared total: this source counts 0, and 2
#: once the walked directory is ``src``.
K21_FIXTURE_WALK = (
    "from pathlib import Path\nHERE = Path(__file__).parent / 'fixtures'\n"
    "EXPECTED_ROWS = 120\ndef test_x():\n    rows = list(HERE.glob('*.jsonl'))\n"
    "    assert len(rows) == 40\n    assert len(rows) == EXPECTED_ROWS\n"
)
