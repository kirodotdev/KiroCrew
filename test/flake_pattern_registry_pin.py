"""The registry-pin detectors of ``test_flake_pattern_ratchet.py`` (K22, J7).

A test that copies a product registry's size or members by hand checks nothing the
registry does not already say, and every legitimate addition has to edit it: two
changes that each add an entry both rewrite the same number (``main`` goes red after
both merge) or the same list (the second one conflicts). The detectors count that
shape so a change cannot add one; the fix is a property test that derives the set
from its source and asserts what each member must satisfy
(testing-conventions § No change-detector tests). The ratchet owns counting and runs
the cases kept here.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

#: Words one of which every module K22 reads holds (``PY_TRIGGERS``).
K22_TRIGGERS = ("kiro_crew",)
#: Below this a pinned size is an emptiness or singleton check, not a copied total.
MIN_PINNED_SIZE = 2
#: Below this a literal collection is a tiny enum, not a copied registry.
MIN_LISTED = 3
#: A public ALL-CAPS name. A private one (``_ADMISSIONS``) is usually live module
#: state a test inspects, not a registry.
_CAPS = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: A constant whose length is its format (a commit SHA, a digest, a key), not a count.
_FORMAT_NAME = re.compile(r"(SHA\d*|COMMIT|DIGEST|HASH|KEY|TOKEN|SECRET)$")
_WRAPS = frozenset({"set", "frozenset", "sorted", "list", "tuple"})
_VIEWS = frozenset({"keys", "values", "items"})


def _product_imports(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """``(ALL-CAPS names imported from kiro_crew, names bound to a kiro_crew module)``."""
    caps: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if node.module.split(".")[0] != "kiro_crew":
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                (caps if _CAPS.match(alias.name) else modules).add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "kiro_crew":
                    modules.add(alias.asname or "kiro_crew")
    return frozenset(caps), frozenset(modules)


def _root(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_registry(node: ast.AST, caps: frozenset[str], modules: frozenset[str]) -> bool:
    """``REG``, ``mod.REG``, ``REG.keys()`` or one of those inside ``set()`` / ``sorted()``."""
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _WRAPS
        and len(node.args) == 1
    ):
        node = node.args[0]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _VIEWS
        and not node.args
    ):
        node = node.func.value
    if isinstance(node, ast.Name):
        return node.id in caps
    if isinstance(node, ast.Attribute):
        return bool(_CAPS.match(node.attr)) and _root(node.value) in modules
    return False


def _literal_collection(node: ast.AST) -> bool:
    return (
        isinstance(node, (ast.List, ast.Set, ast.Tuple))
        and len(node.elts) >= MIN_LISTED
        and all(isinstance(e, ast.Constant) and isinstance(e.value, (str, int)) for e in node.elts)
    )


def _pins(left: ast.AST, right: ast.AST, caps: frozenset[str], modules: frozenset[str]) -> bool:
    if (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Name)
        and left.func.id == "len"
        and len(left.args) == 1
        and _is_registry(left.args[0], caps, modules)
        and not _FORMAT_NAME.search(ast.unparse(left.args[0]))
    ):
        return (
            isinstance(right, ast.Constant)
            and type(right.value) is int
            and right.value >= MIN_PINNED_SIZE
        )
    return _is_registry(left, caps, modules) and _literal_collection(right)


def k22_registry_pin(module) -> Iterable[ast.AST]:
    """An ``assert`` or ``assertEqual`` that pins a kiro_crew registry's size to an
    integer literal, or its members to a literal list, set or tuple."""
    caps, modules = _product_imports(module.tree)
    if not caps and not modules:
        return
    for node in module.asserts:
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and (
                _pins(test.left, test.comparators[0], caps, modules)
                or _pins(test.comparators[0], test.left, caps, modules)
            )
        ):
            yield node
    for call in module.calls:
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "assertEqual"
            and len(call.args) >= 2
            and (
                _pins(call.args[0], call.args[1], caps, modules)
                or _pins(call.args[1], call.args[0], caps, modules)
            )
        ):
            yield call


#: (snippets the detector must count once each, a near miss it must not count).
K22_CASES: tuple[tuple[str, ...], str] = (
    (
        "from kiro_crew.metrics import CHANNEL_VALUES\n"
        "def test_x():\n    assert len(CHANNEL_VALUES) == 12\n",
        "from kiro_crew.agent_sdk import backends as sdk\n"
        "def test_x():\n    assert sorted(sdk.ACP_BACKENDS_KNOWN) == ['', 'claude', 'codex']\n",
        "import kiro_crew.hooks.events as ev\n"
        "def test_x():\n    assert set(ev.HOOK_EVENTS.keys()) == {'a', 'b', 'c'}\n",
        "import unittest\nfrom kiro_crew.argv_rules import ARGV_RULE_BY_ID\n"
        "class T(unittest.TestCase):\n"
        "    def test_x(self):\n        self.assertEqual(14, len(ARGV_RULE_BY_ID))\n",
    ),
    # A non-empty check, a singleton, a two-value enum, a relational size, a
    # test-local constant, a set of names rather than literals, a ceiling, the size
    # of a function's output, private live state, and a commit SHA's length.
    "from kiro_crew.metrics import CHANNEL_VALUES, ORIGINS, KINDS, A, B, C, build\n"
    "from kiro_crew import registry as reg\n"
    "SECRET = 'x' * 40\n"
    "def test_x():\n    assert len(CHANNEL_VALUES) == 1\n    assert len(reg.ROWS) == 0\n"
    "    assert set(ORIGINS) == {'a', 'b'}\n    assert len(KINDS) == len(reg.KIND_LABELS)\n"
    "    assert len(SECRET) == 40\n    assert set(KINDS) == {A, B, C}\n"
    "    assert len(reg.ROWS) <= 50\n    assert len(build(CHANNEL_VALUES)) == 12\n"
    "    assert len(reg._ADMISSIONS) == 2\n    assert len(reg.ENGINE_COMMIT) == 40\n",
)

_JS_IMPORT = re.compile(
    r"\bimport\s+(?:type\s+)?(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s+['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
#: An import from one of these is a test's own fixture, not a product registry.
_JS_TEST_SOURCE = re.compile(r"test|fixture|mock|helper|stub|fake", re.IGNORECASE)
_JS_SUBJECT = r"(?:\[\s*\.\.\.\s*)?(?:Object\.(?:keys|values|entries)\(\s*)?([A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)*\s*\)?\s*\]?(?:\.sort\(\))?"
_JS_LENGTH_PIN = re.compile(
    r"expect\(\s*" + _JS_SUBJECT + r"\s*\)\s*\.toHaveLength\(\s*(\d+)\s*\)"
    r"|expect\(\s*([A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)*\.length\s*\)\s*\.toBe\(\s*(\d+)\s*\)"
)
_JS_LIST_PIN = re.compile(r"expect\(\s*" + _JS_SUBJECT + r"\s*\)\s*\.toEqual\(\s*\[")
#: One member of an array literal: a quoted string or a number, nothing computed.
_JS_LITERAL_MEMBER = re.compile(r"""^(?:'[^']*'|"[^"]*"|`[^`$]*`|-?\d+(?:\.\d+)?)$""")
_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def _js_literal_array(rest: str) -> bool:
    """Whether the array literal that *rest* opens (after its ``[``) holds only
    literal members, at least :data:`MIN_LISTED` of them."""
    end = rest.find("]")
    if end < 0:
        return False
    members = [m.strip() for m in _JS_COMMENT.sub("", rest[:end]).split(",")]
    members = [m for m in members if m]
    return len(members) >= MIN_LISTED and all(_JS_LITERAL_MEMBER.match(m) for m in members)


def _js_product_imports(text: str) -> frozenset[str]:
    names: set[str] = set()
    for default, named, source in _JS_IMPORT.findall(text):
        if not source.startswith((".", "@/")) or _JS_TEST_SOURCE.search(source.rsplit("/", 1)[-1]):
            continue
        if default:
            names.add(default)
        for part in named.split(","):
            part = part.strip().removeprefix("type ").strip()
            if part:
                names.add(part.split(" as ")[-1].strip())
    return frozenset(names)


def j7_registry_pin(rel: str, text: str) -> list[int]:
    """``expect(REG).toHaveLength(n)``, ``expect(REG.length).toBe(n)`` or
    ``expect([...REG].sort()).toEqual(['a', 'b', 'c'])`` (an array of literals only,
    on one line or several), for a REG the file imports from a product module, with
    ``n`` of 2 or more."""
    imported = _js_product_imports(text)
    if not imported:
        return []
    found = []
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    for number, line in enumerate(text.splitlines(), 1):
        for match in _JS_LENGTH_PIN.finditer(line):
            name, size = (match.group(1), match.group(2)) if match.group(1) else match.group(3, 4)
            if name in imported and _CAPS.match(name) and int(size) >= MIN_PINNED_SIZE:
                found.append(number)
                break
        else:
            match = _JS_LIST_PIN.search(line)
            if (
                match
                and match.group(1) in imported
                and _CAPS.match(match.group(1))
                and _js_literal_array(text[starts[number - 1] + match.end() :])
            ):
                found.append(number)
    return found


#: (file, snippets the detector must count once each, a near miss it must not count).
J7_CASES: tuple[str, tuple[str, ...], str] = (
    "website/src/a.test.ts",
    (
        "import { CREW_PHASES } from '../phases'\nexpect(CREW_PHASES).toHaveLength(13)\n",
        "import { INSTANCE_SHORTCUTS } from '@/hooks/keys'\n"
        "expect(INSTANCE_SHORTCUTS.length).toBe(6)\n",
        "import { PRESET_STATES } from '../lib/broadcast'\n"
        "expect([...PRESET_STATES.all].sort()).toEqual(['a', 'b', 'c'])\n",
        "import { PROFILE_TABS } from './tabs'\nexpect(PROFILE_TABS).toEqual([\n"
        "  'sessions', // first\n  'schedule',\n  'goals',\n])\n",
    ),
    # A test-local constant, an import from a test helper, a size of one, a
    # relational size, lists built from names on one line or several, and a
    # two-member list.
    "import { CREW_PHASES, KINDS, A, B } from '../phases'\n"
    "import { WS_INSTANCES } from './helpers/wsMock'\n"
    "const IDS = readdirSync(dir)\nexpect(IDS).toHaveLength(13)\n"
    "expect(WS_INSTANCES).toHaveLength(3)\nexpect(CREW_PHASES).toHaveLength(1)\n"
    "expect(KINDS).toHaveLength(CREW_PHASES.length)\nexpect(KINDS).toEqual([A, B])\n"
    "expect(KINDS).toEqual([\n  A,\n  B,\n  'c',\n])\nexpect(KINDS).toEqual(['a', 'b'])\n",
)
