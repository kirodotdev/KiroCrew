"""Read the patches a test file makes out of its syntax tree, for the mirrored-facade guards.

``patch_events`` lists every ``mock.patch`` (in every form), every ``setattr`` /
``delattr`` on an object or a dotted string, every plain ``alias.name = value`` and every
namespace-dict write a source makes, with its target resolved through a table of a
facade's spellings; the create=True census, the reach proof, the namespace-write scan and
the direct-owner scan in ``test_facade_mirror_guards.py`` all read it.
``text_patched_names`` is the fast regular expression reader of the same names, and
``needs_syntax_tree`` routes to the syntax tree only the files holding a patch shape the
regular expressions cannot read, so the corpus is parsed where parsing adds something.
"""

from __future__ import annotations

import ast
import bisect
import dataclasses
import dis
import functools
import importlib.util
import re
from collections.abc import Iterable, Iterator, Mapping
from types import CodeType

#: The callables that are ``mock.patch`` and its variants, by the dotted name an import
#: makes them, and where each takes ``create`` by position.
_PATCH_FORMS: dict[str, int] = {}
for _provider in ("unittest.mock", "mock", "mocker"):
    _PATCH_FORMS[f"{_provider}.patch"] = 3
    _PATCH_FORMS[f"{_provider}.patch.object"] = 4
    _PATCH_FORMS[f"{_provider}.patch.multiple"] = 2
#: ``mock.patch.dict``, which writes a mapping in place: on a module's namespace it
#: bypasses the module's class, the mirror's included.
_DICT_FORMS = frozenset(f"{p}.patch.dict" for p in ("unittest.mock", "mock", "mocker"))
#: Methods of a namespace dict that write or delete a key.
_DICT_WRITERS = frozenset(
    {"update", "pop", "setdefault", "__setitem__", "__delitem__", "clear", "popitem"}
)

#: ``patch.multiple`` keywords that configure the patch rather than name a target.
_MULTIPLE_PARAMETERS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_PARTIALS = frozenset({"functools.partial"})
_IMPORTERS = frozenset({"importlib.import_module", "pytest.importorskip"})
_DYNAMIC = "<dynamic>"
_LOCAL = "<local>"
#: The node types :class:`_Bindings` reads; every other node is skipped at one lookup.
_BINDING_TYPES = frozenset(
    {
        ast.Import,
        ast.ImportFrom,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.Lambda,
        ast.ClassDef,
        ast.Assign,
        ast.AnnAssign,
        ast.For,
        ast.AsyncFor,
        ast.comprehension,
        ast.withitem,
        ast.AugAssign,
        ast.NamedExpr,
        ast.ExceptHandler,
    }
)
#: The statements :func:`patch_events` reads a target from.
_WRITE_STATEMENTS = frozenset({ast.Assign, ast.AugAssign, ast.Delete})
_WORD = re.compile(r"\w+")
_FROM_IMPORT = re.compile(r"\bfrom\s+([\w.]+)\s+import\s+")


def _dot_prefixes(dotted: str) -> Iterator[str]:
    """*dotted* and each of its leading dotted segments, longest first: the spellings a
    name can start with, looked up one by one instead of testing every spelling."""
    yield dotted
    at = dotted.rfind(".")
    while at != -1:
        yield dotted[:at]
        at = dotted.rfind(".", 0, at)


class _Bindings:
    """What each name in one test file is bound to, read off its syntax tree.

    ``modules`` maps a name to the dotted module or object it is bound to (``<local>`` for
    a def or class); ``strings`` maps a name to a string constant. A name bound more than
    once keeps the binding that names a facade, so a reassignment cannot hide one, and a
    name with any binding this reader cannot follow -- a parameter, a loop, ``with ... as``
    or tuple target, an augmented or walrus assignment -- reads as ``<dynamic>``.
    """

    def __init__(
        self, tree: ast.AST, facades: Mapping[str, str], nodes: list[ast.AST] | None = None
    ) -> None:
        self.modules: dict[str, str] = {"mocker": "mocker"}
        self.strings: dict[str, str] = {}
        self.untracked: set[str] = set()
        self._facades = facades
        pending: list[tuple[str, ast.expr]] = []
        for node in nodes if nodes is not None else ast.walk(tree):
            if type(node) not in _BINDING_TYPES:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self._bind(alias.asname, alias.name)
                    else:
                        head = alias.name.split(".")[0]
                        self._bind(head, head)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    self._bind(alias.asname or alias.name, f"{node.module}.{alias.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if not isinstance(node, ast.Lambda):
                    self.modules.setdefault(node.name, _LOCAL)
                arguments = node.args
                self.untracked.update(
                    arg.arg
                    for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
                    if arg.arg != "mocker"  # pytest-mock's fixture, read as the patcher
                )
                self.untracked.update(a.arg for a in (arguments.vararg, arguments.kwarg) if a)
            elif isinstance(node, ast.ClassDef):
                self.modules.setdefault(node.name, _LOCAL)
            elif isinstance(node, ast.Assign):
                if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    pending.append((node.targets[0].id, node.value))
                else:
                    self._untrack(*node.targets)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    pending.append((node.target.id, node.value))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self._untrack(node.target)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                self._untrack(node.optional_vars)
            elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
                self._untrack(node.target)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self.untracked.add(node.name)
        for _ in range(3):  # a name assigned from a name assigned from a name
            for name, value in pending:
                text = self.text_of(value)
                if text is not None and text != _DYNAMIC:
                    self._bind_text(name, text)
                dotted = self.dotted_of(value)
                if dotted is not None and dotted != _DYNAMIC:
                    self._bind(name, dotted)

    def _untrack(self, *targets: ast.expr) -> None:
        """Mark the names *targets* bind; an attribute or item target binds no name."""
        for target in targets:
            if isinstance(target, ast.Name):
                self.untracked.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                self._untrack(*target.elts)
            elif isinstance(target, ast.Starred):
                self._untrack(target.value)

    def _names_facade(self, dotted: str) -> bool:
        return any(prefix in self._facades for prefix in _dot_prefixes(dotted))

    def _bind(self, name: str, dotted: str) -> None:
        current = self.modules.get(name)
        if (
            current is None
            or current == _LOCAL
            or (not self._names_facade(current) and self._names_facade(dotted))
        ):
            self.modules[name] = dotted

    def _bind_text(self, name: str, text: str) -> None:
        current = self.strings.get(name)
        if current is None or (not self._names_facade(current) and self._names_facade(text)):
            self.strings[name] = text

    def canonical(self, dotted: str) -> str:
        """*dotted* with a spelling of a facade replaced by the facade's own name (the
        longest spelling that is *dotted* or one of its leading dotted segments)."""
        for prefix in _dot_prefixes(dotted):
            if prefix in self._facades:
                return self._facades[prefix] + dotted[len(prefix) :]
        return dotted

    def dotted_of(self, node: ast.expr) -> str | None:
        """The dotted module or object *node* names, ``<dynamic>``, or None (a literal)."""
        if isinstance(node, ast.Name):
            if node.id in self.untracked:
                return _DYNAMIC
            return self.modules.get(node.id, _DYNAMIC)
        if isinstance(node, ast.Attribute):
            base = self.dotted_of(node.value)
            if base is None or base in (_DYNAMIC, _LOCAL):
                return base
            return f"{base}.{node.attr}"
        if isinstance(node, ast.Call):
            callee = self.dotted_of(node.func)
            if callee in _IMPORTERS and node.args:
                text = self.text_of(node.args[0])
                if text is None or text == _DYNAMIC:
                    return _DYNAMIC
                if not text.startswith("."):
                    return text
                package = self.text_of(node.args[1]) if len(node.args) > 1 else None
                if package is None or package == _DYNAMIC:
                    return _DYNAMIC
                return importlib.util.resolve_name(text, package)
            if callee == "sys.modules.get" and node.args:
                text = self.text_of(node.args[0])
                return text if text is not None else _DYNAMIC
            if callee == _DYNAMIC and getattr(node.func, "id", "") == "getattr":
                if len(node.args) >= 2:
                    base = self.dotted_of(node.args[0])
                    attr = self.text_of(node.args[1])
                    if base not in (None, _DYNAMIC, _LOCAL) and attr not in (None, _DYNAMIC):
                        return f"{base}.{attr}"
            return _DYNAMIC
        if isinstance(node, ast.Subscript) and self.dotted_of(node.value) == "sys.modules":
            text = self.text_of(node.slice)
            return text if text is not None else _DYNAMIC
        if isinstance(node, (ast.Subscript, ast.Await, ast.IfExp, ast.BoolOp, ast.Starred)):
            return _DYNAMIC
        return None

    def text_of(self, node: ast.expr) -> str | None:
        """The string *node* spells, ``<dynamic>`` when it is built at run time, or None."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            if node.id in self.untracked:
                return _DYNAMIC
            return self.strings.get(node.id, _DYNAMIC if node.id not in self.modules else None)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                part: str | None = None
                if isinstance(value, ast.Constant):
                    part = str(value.value)
                elif isinstance(value, ast.FormattedValue):
                    part = self.text_of(value.value)
                if part is None or part == _DYNAMIC:
                    return _DYNAMIC
                parts.append(part)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.text_of(node.left), self.text_of(node.right)
            if left is None or right is None:
                return None
            if _DYNAMIC in (left, right):
                return _DYNAMIC
            return left + right
        return _DYNAMIC if isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)) else None


@dataclasses.dataclass(frozen=True)
class PatchSite:
    """One patch call: where it is, what it patches, and its ``create`` argument."""

    line: int
    function: str
    module: str
    name: str
    create: object  # True, False or <dynamic>
    #: "mock" (mock.patch), "setattr" (setattr / delattr), "assign" (``m.name = ...`` or
    #: ``del m.name``) or "namespace" (a write to the module's ``__dict__`` itself).
    kind: str = "mock"


def _function_spans(nodes: Iterable[ast.AST]) -> list[tuple[int, int, str]]:
    """``(first line, last line, name)`` of every function among *nodes*, outermost first."""
    spans = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in nodes
        if type(node) in (ast.FunctionDef, ast.AsyncFunctionDef)
    ]
    return sorted(spans, key=lambda span: (span[0], -span[1]))


def _enclosing_function(line: int, spans: list[tuple[int, int, str]]) -> str:
    """The innermost function whose lines hold *line*, or ``<module>``.

    *spans* are sorted by first line, so the innermost holder is the last span starting
    at or before *line* that still holds it.
    """
    index = bisect.bisect_right(spans, (line, float("inf"), ""))
    for first, last, candidate in reversed(spans[:index]):
        if first <= line <= last:
            return candidate
    return "<module>"


def _nodes(tree: ast.AST) -> list[ast.AST]:
    """Every node of *tree*, as :func:`ast.walk` yields them, less the shared
    ``Load`` / ``Store`` / ``Del`` context markers, which name nothing."""
    nodes = [tree]
    for node in nodes:  # the list grows while it is read: a breadth-first walk
        for field in node._fields:
            if field == "ctx":
                continue
            value = getattr(node, field, None)
            if isinstance(value, ast.AST):
                nodes.append(value)
            elif isinstance(value, list):
                nodes.extend(item for item in value if isinstance(item, ast.AST))
    return nodes


def _patch_call(
    bindings: _Bindings, call: ast.Call, called_at_once: bool
) -> tuple[str, list[ast.expr], list[ast.keyword]] | None:
    """The patch form, arguments and keywords *call* passes, seeing through ``partial``."""
    callee = bindings.dotted_of(call.func)
    if callee in _PATCH_FORMS:
        return callee, list(call.args), list(call.keywords)
    inner = call.func
    if (
        isinstance(inner, ast.Call)
        and bindings.dotted_of(inner.func) in _PARTIALS
        and inner.args
        and bindings.dotted_of(inner.args[0]) in _PATCH_FORMS
    ):
        form = bindings.dotted_of(inner.args[0])
        assert form is not None
        return form, [*inner.args[1:], *call.args], [*inner.keywords, *call.keywords]
    if callee in _PARTIALS and call.args and bindings.dotted_of(call.args[0]) in _PATCH_FORMS:
        if called_at_once:
            return None  # judged with the arguments its caller adds, above
        form = bindings.dotted_of(call.args[0])
        assert form is not None
        return form, list(call.args[1:]), list(call.keywords)
    return None


def _create_value(form: str, args: list[ast.expr], keywords: list[ast.keyword]) -> object:
    for keyword in keywords:
        if keyword.arg == "create":
            if isinstance(keyword.value, ast.Constant):
                return bool(keyword.value.value)
            return _DYNAMIC
        if keyword.arg is None:
            return _DYNAMIC  # **kwargs may carry create
    position = _PATCH_FORMS[form]
    if any(isinstance(argument, ast.Starred) for argument in args[: position + 1]):
        return _DYNAMIC
    if len(args) > position:
        argument = args[position]
        return bool(argument.value) if isinstance(argument, ast.Constant) else _DYNAMIC
    return False


def _patched_targets(
    bindings: _Bindings, form: str, args: list[ast.expr], keywords: list[ast.keyword]
) -> list[tuple[str, str]]:
    if not args:
        return [(_DYNAMIC, _DYNAMIC)]
    first = args[0]
    if form.endswith(".patch"):
        text = bindings.text_of(first)
        if text is None or text == _DYNAMIC:
            return [(_DYNAMIC, _DYNAMIC)]
        module, _, name = bindings.canonical(text).rpartition(".")
        return [(module, name)]
    module = bindings.text_of(first) if isinstance(first, (ast.Constant, ast.JoinedStr)) else None
    if module is None or module == _DYNAMIC:
        module = bindings.dotted_of(first) or _LOCAL
    module = bindings.canonical(module)
    if form.endswith(".patch.object"):
        if len(args) < 2 or isinstance(args[1], ast.Starred):
            return [(module, _DYNAMIC)]
        name = bindings.text_of(args[1])
        return [(module, name if name is not None else _DYNAMIC)]
    names = [kw.arg for kw in keywords if kw.arg and kw.arg not in _MULTIPLE_PARAMETERS]
    return [(module, name) for name in names] or [(module, _DYNAMIC)]


def _tree(source: str | ast.Module) -> ast.Module:
    return source if isinstance(source, ast.Module) else ast.parse(source)


def _namespace_of(bindings: _Bindings, node: ast.expr) -> str | None:
    """The module whose namespace dict *node* is (``vars(m)`` or ``m.__dict__``), or None."""
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return bindings.canonical(bindings.dotted_of(node.value) or _LOCAL)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
        and "vars" not in bindings.modules
        and len(node.args) == 1
    ):
        return bindings.canonical(bindings.dotted_of(node.args[0]) or _LOCAL)
    return None


def _mapping_keys(
    bindings: _Bindings, args: list[ast.expr], keywords: list[ast.keyword]
) -> list[str]:
    """The keys a ``dict.update``-shaped call writes: a dict literal's and the keywords."""
    keys: list[str] = []
    for argument in args:
        if not isinstance(argument, ast.Dict):
            return [_DYNAMIC]
        for key in argument.keys:
            text = bindings.text_of(key) if key is not None else None
            keys.append(text if text is not None else _DYNAMIC)
    for keyword in keywords:
        keys.append(keyword.arg if keyword.arg else _DYNAMIC)
    return keys


def _namespace_writes(bindings: _Bindings, call: ast.Call) -> tuple[str, list[str]] | None:
    """``(module, names)`` when *call* writes a module's namespace dict, else None.

    ``mock.patch.dict(vars(m), {...})`` (a string target ending in ``.__dict__`` too),
    ``monkeypatch.setitem(vars(m), "name", ...)`` / ``delitem``, and a writing method of
    the dict itself (``m.__dict__.update(...)``, ``vars(m).pop("name")``, ...).
    """
    func, args, keywords = call.func, list(call.args), list(call.keywords)
    if bindings.dotted_of(func) in _DICT_FORMS and args:
        module = _namespace_of(bindings, args[0])
        text = bindings.text_of(args[0])
        if module is None and text not in (None, _DYNAMIC) and text.endswith(".__dict__"):
            module = bindings.canonical(text[: -len(".__dict__")])
        if module is None:
            return None
        return module, _mapping_keys(bindings, args[1:2], [k for k in keywords if k.arg != "clear"])
    verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if verb in {"setitem", "delitem"} and len(args) > 1:
        module = _namespace_of(bindings, args[0])
        if module is not None:
            return module, [bindings.text_of(args[1]) or _DYNAMIC]
    if isinstance(func, ast.Attribute) and func.attr in _DICT_WRITERS:
        module = _namespace_of(bindings, func.value)
        if module is None:
            return None
        if func.attr == "update":
            return module, _mapping_keys(bindings, args, keywords)
        if func.attr in {"clear", "popitem"} or not args:
            return module, [_DYNAMIC]
        return module, [bindings.text_of(args[0]) or _DYNAMIC]
    return None


def patch_events(source: str | ast.Module, facades: Mapping[str, str]) -> list[PatchSite]:
    """Every patch *source* makes, its target resolved through *facades*, in one pass.

    ``mock.patch`` in every form; ``setattr`` / ``delattr`` (a ``monkeypatch`` fixture's or
    the builtin) on an object or on a dotted string; a plain ``alias.name = value`` or
    ``del alias.name``; and every write to a module's namespace dict, which bypasses the
    module's class (``vars(m)[...] = ...``, ``mock.patch.dict(m.__dict__, ...)``,
    ``monkeypatch.setitem(vars(m), ...)``). A module is also reached as
    ``sys.modules["<dotted>"]``. *facades* maps each spelling of a facade (its own dotted
    name, another module's attribute bound to it, or an owner's own name) to the dotted
    name it means.
    """
    tree = _tree(source)
    nodes = _nodes(tree)  # one walk of the tree, read four times below
    bindings = _Bindings(tree, facades, nodes)
    spans = _function_spans(nodes)
    partial_callees = {id(node.func) for node in nodes if type(node) is ast.Call}  # type: ignore[attr-defined]
    events: list[PatchSite] = []
    for node in nodes:
        kind = type(node)
        if kind is ast.Call:
            assert isinstance(node, ast.Call)
            call = _patch_call(bindings, node, id(node) in partial_callees)
            if call is not None:
                function = _enclosing_function(node.lineno, spans)
                form, args, keywords = call
                create = _create_value(form, args, keywords)
                for module, name in _patched_targets(bindings, form, args, keywords):
                    events.append(PatchSite(node.lineno, function, module, name, create))
                continue
            written = _namespace_writes(bindings, node)
            if written is not None:
                function = _enclosing_function(node.lineno, spans)
                module, names = written
                for name in names:
                    events.append(
                        PatchSite(node.lineno, function, module, name, False, "namespace")
                    )
                continue
            func = node.func
            verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if verb not in {"setattr", "delattr"} or not node.args:
                continue
            function = _enclosing_function(node.lineno, spans)
            first = node.args[0]
            text = bindings.text_of(first)
            if isinstance(first, (ast.Constant, ast.JoinedStr)) and text not in (None, _DYNAMIC):
                assert text is not None
                parts = bindings.canonical(text).split(".")
                for cut in range(1, len(parts)):
                    module, name = ".".join(parts[:cut]), parts[cut]
                    events.append(PatchSite(node.lineno, function, module, name, False, "setattr"))
            elif len(node.args) > 1:
                module = bindings.canonical(bindings.dotted_of(first) or _LOCAL)
                name = bindings.text_of(node.args[1]) or _DYNAMIC
                events.append(PatchSite(node.lineno, function, module, name, False, "setattr"))
        elif kind in _WRITE_STATEMENTS:
            assert isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete))
            if isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                targets = node.targets
            for target in targets:
                function = _enclosing_function(node.lineno, spans)
                if isinstance(target, ast.Attribute):
                    module = bindings.canonical(bindings.dotted_of(target.value) or _LOCAL)
                    events.append(
                        PatchSite(node.lineno, function, module, target.attr, False, "assign")
                    )
                elif isinstance(target, ast.Subscript):
                    module = _namespace_of(bindings, target.value)
                    if module is not None:
                        name = bindings.text_of(target.slice) or _DYNAMIC
                        events.append(
                            PatchSite(node.lineno, function, module, name, False, "namespace")
                        )
    return events


def patch_sites(source: str | ast.Module, facades: Mapping[str, str]) -> list[PatchSite]:
    """The ``mock.patch`` calls among :func:`patch_events`."""
    return [event for event in patch_events(source, facades) if event.kind == "mock"]


def create_true_patches(
    source: str | ast.Module,
    facades: Mapping[str, str],
    forwarded: Mapping[str, frozenset[str] | None],
) -> list[PatchSite]:
    """The patch sites in *source* that can leave a forwarded name deleted for good.

    A site counts when ``create`` is true or cannot be read, and its target is a name a
    facade forwards (*forwarded*; None means every name) or cannot be read at all.
    """
    return create_true_sites(patch_sites(source, facades), forwarded)


def create_true_sites(
    sites: Iterable[PatchSite], forwarded: Mapping[str, frozenset[str] | None]
) -> list[PatchSite]:
    """The sites among *sites* that :func:`create_true_patches` counts."""
    found = []
    for site in sites:
        if site.kind != "mock" or (site.create is not True and site.create != _DYNAMIC):
            continue
        names = forwarded.get(site.module, frozenset())
        if site.module == _DYNAMIC:
            found.append(site)
        elif site.module in forwarded and (
            names is None or site.name == _DYNAMIC or site.name in names
        ):
            found.append(site)
    return found


@functools.lru_cache(maxsize=None)
def _spelling_patterns(dotted: str) -> tuple[re.Pattern[str], ...]:
    """Every way a test file can spell module *dotted*, as text it must contain.

    The dotted name itself (an import, a string target, ``import_module``); a
    ``from <parent> import ...`` naming its leaf, parenthesised lists included; an
    attribute of its parent package (``dashboard.state``); and a relative
    ``import_module(".leaf", "<parent>")``. Matching the leaf word alone would admit most
    of the tree for a leaf like ``state``, and parsing it is what the scans cost.
    """
    head, _, leaf = dotted.rpartition(".")
    head_leaf = head.rpartition(".")[2]
    word = rf"\b{re.escape(leaf)}\b"
    patterns = [re.escape(dotted)]
    if head:
        patterns += [
            rf"\bfrom\s+{re.escape(head)}\s+import\s+(?:\([^)]*?{word}|[^\n]*?{word})",
            rf"\b{re.escape(head_leaf)}\s*\.\s*{word}",
            rf"['\"]\.{re.escape(leaf)}['\"]",
        ]
    return tuple(re.compile(pattern) for pattern in patterns)


def _mentions(text: str, dotted_names: Iterable[str]) -> bool:
    """Whether *text* spells any of *dotted_names* (see :func:`_spelling_patterns`), or
    reaches one through an alias of its parent module (:func:`_alias_chain`).

    A spelling of a module always contains its leaf, so a C-level substring check rejects
    almost every file before any pattern runs.

    A plain module name (:func:`_is_plain`) is answered from readings of the text made once
    per text and once per text and leaf (:func:`_mentions_plain`) instead: a facade imported
    by hundreds of modules has hundreds of spellings sharing its leaf, and running the
    patterns of each over the whole text is what a scan of the test tree would otherwise
    cost. A table of many leaves first asks the text's word index (:class:`_TextWords`) whether
    any word begins with the leaf, and skips the name when none does.
    """
    names = tuple(dotted_names)
    leaves, indexed = _plain_leaves(names)
    words = _text_words(text) if indexed else None
    for name, leaf in zip(names, leaves):
        if leaf is not None:
            if (words is None or words.starts(leaf)) and _mentions_plain(text, name, words):
                return True
            continue
        if name in text:
            return True
        head, _, leaf = name.rpartition(".")
        if leaf not in text:
            continue
        from_import, attribute, relative = _spelling_patterns(name)[1:]
        if head in text and from_import.search(text):
            return True
        if f".{leaf}" in text and head.rpartition(".")[2] in text:
            if attribute.search(text) or _alias_chain(text, name):
                return True
        if (f"'.{leaf}'" in text or f'".{leaf}"' in text) and relative.search(text):
            return True
    return False


def _alias_chain(text: str, dotted: str, words: _TextWords | None = None) -> bool:
    """Whether *text* writes ``<alias>.<leaf>`` outside an import line, where ``<alias>``
    is a name *text* binds the parent module of *dotted* to (:func:`_text_aliases`).

    ``from kiro_crew.mcp_tools import browser as mod`` then ``mod.mcp_core`` spells
    ``kiro_crew.mcp_tools.browser.mcp_core`` without any of the literal spellings above.
    """
    head, _, leaf = dotted.rpartition(".")
    head_leaf = head.rpartition(".")[2]
    if not head_leaf:
        return False
    if words is None:
        if f".{leaf}" not in text or head_leaf not in text:
            return False
        chained = _chain_words(text, leaf, head_leaf)
    else:
        chained = words.chain_words(leaf)
    # ``<head leaf>.<leaf>`` is :func:`_spelling_patterns`' to read, not an alias chain.
    candidates = chained - {head_leaf}
    if not candidates or head_leaf not in text:
        return False
    return not candidates.isdisjoint(_text_aliases(text, head))


def _chain_words(text: str, leaf: str, drop: str = "") -> frozenset[str]:
    """The words *text* writes before ``.<leaf>`` outside an import line, for
    :func:`_alias_chain`; they depend on the leaf alone, never on the parent module. A *drop*
    word is left out before the ``as`` check runs, which then has one word fewer to test."""
    needle = f".{leaf}"
    if needle not in text:
        return frozenset()
    # The words written before ``.<leaf>``, read backwards from each occurrence with
    # string scans only: this runs for every file and leaf the cheaper checks pass.
    candidates = set()
    for at in _occurrences(text, needle):
        end = at + len(needle)
        if end < len(text) and (text[end].isalnum() or text[end] == "_"):
            continue
        start = at
        while start > 0 and text[start - 1] in " \t":
            start -= 1
        stop = start
        while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
            start -= 1
        if start == stop or not _starts_a_name(text, start, ".'\""):
            continue
        line = text[text.rfind("\n", 0, start) + 1 : start].lstrip()
        if not line.startswith(("from ", "import ")):
            candidates.add(text[start:stop])
    candidates.discard(drop)
    if "import_module" not in text:
        candidates = {word for word in candidates if re.search(rf"\bas\s+{word}\b", text)}
    return frozenset(candidates)


#: The module names :func:`_mentions_plain` answers: dotted ASCII identifiers, no segment
#: ``from`` or ``import``. Its readings split a text into words the way ``\w`` does, which is
#: exactly what the patterns' ``\b`` boundaries see for such a name.
_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


#: The fewest distinct plain leaves a name table needs before :func:`_mentions` asks the word
#: index. Building the index costs one ``findall``, ``set`` and sort of the text: about as much
#: as 90 ``leaf in text`` scans of an average test file, which is all a small table costs. The
#: per-facade guards pass tables of one to a few leaves, the corpus guards hundreds.
_WORD_INDEX_MIN_LEAVES = 64


@functools.lru_cache(maxsize=64)
def _plain_leaves(names: tuple[str, ...]) -> tuple[tuple[str | None, ...], bool]:
    """The leaf of each of *names* that :func:`_is_plain` admits (``None`` for the rest), and
    whether the table is large enough for the word index (:data:`_WORD_INDEX_MIN_LEAVES`)."""
    leaves = tuple(name.rpartition(".")[2] if _is_plain(name) else None for name in names)
    distinct = {leaf for leaf in leaves if leaf is not None}
    return leaves, len(distinct) >= _WORD_INDEX_MIN_LEAVES


@functools.lru_cache(maxsize=None)
def _is_plain(dotted: str) -> bool:
    """Whether :func:`_mentions` may answer *dotted* from :func:`_mentions_plain`."""
    return _PLAIN_NAME.fullmatch(dotted) is not None and not {"from", "import"} & set(
        dotted.split(".")
    )


def _mentions_plain(text: str, dotted: str, words: _TextWords | None = None) -> bool:
    """:func:`_mentions` for one plain name, from :func:`_from_imports` and
    :func:`_leaf_parts` instead of :func:`_spelling_patterns`; *words*, when given, is the
    text's word index, whose memo :func:`_alias_chain` reads."""
    head, _, leaf = dotted.rpartition(".")
    present, parents, relative = _leaf_parts(text, leaf)
    if not present:
        return False
    if dotted in text or leaf in _from_imports(text).get(head, ()):
        return True
    return head.rpartition(".")[2] in parents or relative or _alias_chain(text, dotted, words)


@functools.lru_cache(maxsize=64)
def _from_imports(text: str) -> dict[str, frozenset[str]]:
    """Each ``from <head> import`` head in *text* -> the words its import list holds: the
    rest of that line, and the parenthesised list when one opens there."""
    imports: dict[str, set[str]] = {}
    for match in _FROM_IMPORT.finditer(text):
        at = match.end()
        line_end = text.find("\n", at)
        words = set(_WORD.findall(text, at, len(text) if line_end == -1 else line_end))
        if text.startswith("(", at):
            close = text.find(")", at)
            words.update(_WORD.findall(text, at, len(text) if close == -1 else close))
        imports.setdefault(match.group(1), set()).update(words)
    return {head: frozenset(words) for head, words in imports.items()}


@functools.lru_cache(maxsize=1024)
def _leaf_parts(text: str, leaf: str) -> tuple[bool, frozenset[str], bool]:
    """``(present, parents, relative)`` for module leaf *leaf* in *text*: whether the leaf
    occurs at all; every word ``<parent>`` the text writes as ``<parent> . <leaf>``, when
    ``.<leaf>`` occurs somewhere in it; and whether it quotes ``'.<leaf>'`` or
    ``".<leaf>"`` (a relative ``import_module``)."""
    if leaf not in text:
        return False, frozenset(), False
    parents = set()
    if f".{leaf}" in text:
        for match in re.finditer(rf"\b{re.escape(leaf)}\b", text):
            at = match.start() - 1
            while at >= 0 and text[at].isspace():
                at -= 1
            if at < 0 or text[at] != ".":
                continue
            at -= 1
            while at >= 0 and text[at].isspace():
                at -= 1
            end = at + 1
            while at >= 0 and _is_word(text[at]):
                at -= 1
            if at + 1 < end:
                parents.add(text[at + 1 : end])
    relative = f"'.{leaf}'" in text or f'".{leaf}"' in text
    return True, frozenset(parents), relative


def _is_word(char: str) -> bool:
    return char.isalnum() or char == "_"


class _TextWords:
    """One text's distinct ``\\w`` words, sorted, with the answers already given about it.

    :func:`_mentions_plain` answers yes only where some word of the text begins with the
    name's leaf: ``<head>.<leaf>`` written in the text has a dot before the leaf, so a word
    starts there; a ``from <head> import`` list, a ``\\b<leaf>\\b`` match, a quoted
    ``'.<leaf>'`` and :func:`_alias_chain`'s ``.<leaf>`` followed by a non-word character
    each hold the leaf as a whole word. :meth:`starts` asks exactly that, with a binary
    search, once per leaf; :meth:`chain_words` keeps :func:`_chain_words` per leaf, since
    the hundreds of spellings sharing a facade's leaf differ only in the parent they name.
    """

    __slots__ = ("_text", "_words", "_starts", "_chains")

    def __init__(self, text: str) -> None:
        self._text: str = text
        self._words: list[str] = sorted(set(_WORD.findall(text)))
        self._starts: dict[str, bool] = {}
        self._chains: dict[str, frozenset[str]] = {}

    def starts(self, leaf: str) -> bool:
        """Whether some word of the text begins with *leaf*."""
        known = self._starts.get(leaf)
        if known is None:
            at = bisect.bisect_left(self._words, leaf)
            known = at < len(self._words) and self._words[at].startswith(leaf)
            self._starts[leaf] = known
        return known

    def chain_words(self, leaf: str) -> frozenset[str]:
        """:func:`_chain_words` of the text for *leaf*, read once."""
        chained = self._chains.get(leaf)
        if chained is None:
            chained = self._chains[leaf] = _chain_words(self._text, leaf)
        return chained


@functools.lru_cache(maxsize=64)
def _text_words(text: str) -> _TextWords:
    """The word index of *text*, built once per text for the large tables that ask it."""
    return _TextWords(text)


def _mentioned(text: str, spellings: Iterable[str]) -> list[str]:
    """The *spellings* :func:`_mentions` answers yes for in *text*, in their order.

    In a large table (:data:`_WORD_INDEX_MIN_LEAVES`) a plain spelling whose leaf no word of
    *text* begins with is dropped from the word index alone, which spares the call for most
    of the hundreds of spellings each text is asked about.
    """
    names = tuple(spellings)
    leaves, indexed = _plain_leaves(names)
    if not indexed:
        return [name for name in names if _mentions(text, [name])]
    words = _text_words(text)
    return [
        name
        for name, leaf in zip(names, leaves)
        if (
            _mentions(text, [name])
            if leaf is None
            else words.starts(leaf) and _mentions_plain(text, name, words)
        )
    ]


def _admitted(text: str, spellings: Mapping[str, str]) -> bool:
    """The text prefilter: as wide as the detector, so it never hides a site."""
    return "patch" in text and _mentions(text, spellings)


#: A ``mock.patch`` call opening, for the positional-``create`` candidate check.
_PATCH_OPEN = re.compile(r"\bpatch(?:\.object|\.multiple)?\s*\(")


def may_create(text: str) -> bool:
    """Whether *text* could pass ``create`` to a patch: by keyword, through ``**``, or
    by position (a patch call with as many top-level commas as the slot needs).

    The create=True scan parses only such files; the rest cannot hold an offender.
    """
    if re.search(r"\bcreate\s*=", text):
        return True
    for match in _PATCH_OPEN.finditer(text):
        depth, commas, start = 1, 0, match.end()
        form = match.group(0)
        needed = 4 if ".object" in form else 2 if ".multiple" in form else 3
        end = start
        for end in range(start, min(len(text), start + 4000)):
            char = text[end]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth == 0:
                    break
            elif char == "," and depth == 1:
                commas += 1
        if commas >= needed or "**" in text[start:end]:
            return True
    return False


def patched_names(source: str | ast.Module, facades: Mapping[str, str]) -> dict[str, set[str]]:
    """Facade -> every name *source* patches through it (:func:`patch_events`)."""
    return names_by_target(patch_events(source, facades), set(facades.values()))


def names_by_target(events: Iterable[PatchSite], targets: Iterable[str]) -> dict[str, set[str]]:
    wanted = set(targets)
    found: dict[str, set[str]] = {}
    for event in events:
        if event.module in wanted and event.name not in (_DYNAMIC, _LOCAL, ""):
            found.setdefault(event.module, set()).add(event.name)
    return found


def text_patched_names(text: str, spellings: Mapping[str, str]) -> dict[str, set[str]]:
    """Facade -> the names *text* patches through it, read with regular expressions.

    The fast half of the reach proof: it only has to list the names tests patch, to check
    none is excluded and to pick the executed sample, because :func:`unreached_readers`
    proves reach for every name from the owners' bytecode. It reads dotted string targets,
    ``setattr`` / ``delattr`` / ``patch.object`` on an alias the file imports the facade
    as (or on ``sys.modules["<dotted>"]``), ``alias.name = value``, and the namespace-dict
    writes :func:`patch_events` reads: ``vars(alias)[...]``, ``alias.__dict__[...]``,
    ``setitem`` / ``delitem`` on either, and the keys of a ``patch.dict`` literal on either.
    Every pattern starts with a literal and is tried only where that literal occurs, so a
    file costs a few substring searches.
    """
    found: dict[str, set[str]] = {}
    for spelling in _mentioned(text, spellings):
        names = found.setdefault(spellings[spelling], set())
        dotted = re.escape(spelling)
        names.update(_at(text, spelling, rf"{dotted}\.(\w+)", before=_QUOTES))
        aliases = _text_aliases(text, spelling)
        for alias in aliases:
            names |= _text_patches_of(text, re.escape(alias), alias)
        if "sys.modules[" in text and spelling in text:
            by_name = rf"sys\.modules\[\s*['\"]{dotted}['\"]\s*\]"
            names |= _text_patches_of(text, by_name, "sys.modules[")
            for alias in _bound_on_the_line(text, "sys.modules[", by_name) - aliases:
                names |= _text_patches_of(text, re.escape(alias), alias)
    return {facade: names for facade, names in found.items() if names}


_QUOTES = "'\""


def _starts_a_name(text: str, at: int, also: str = ".") -> bool:
    """Whether the text at *at* begins a name: the character before it is no part of one."""
    if at == 0:
        return True
    char = text[at - 1]
    return not (char.isalnum() or char == "_" or char in also)


def _at(
    text: str, needle: str, pattern: str, *, before: str | None = None, also: str = "."
) -> list[str]:
    """``group(1)`` of *pattern* (which starts with *needle*) matched at each occurrence of
    *needle* that begins a name (see :func:`_starts_a_name`; *also* lists the characters
    that continue one), or, when *before* is given, that follows one of its characters."""
    compiled = re.compile(pattern)
    found = []
    at = text.find(needle)
    while at != -1:
        if before is not None:
            ok = at > 0 and text[at - 1] in before
        else:
            ok = _starts_a_name(text, at, also)
        match = compiled.match(text, at) if ok else None
        if match is not None:
            found.append(match.group(1))
        at = text.find(needle, at + 1)
    return found


def _bound_on_the_line(text: str, needle: str, value: str) -> set[str]:
    """The names a line binds to *value* (``name = <value>``), found from *needle*."""
    compiled = re.compile(rf"[ \t]*(\w+)[ \t]*=[ \t]*{value}")
    names = set()
    at = text.find(needle)
    while at != -1:
        line = text.rfind("\n", 0, at) + 1
        match = compiled.match(text, line)
        if match is not None:
            names.add(match.group(1))
        at = text.find(needle, at + 1)
    return names


def _text_aliases(text: str, spelling: str) -> set[str]:
    """The names *text* binds the module *spelling* to by an import statement or by
    ``alias = import_module("<dotted>")``."""
    head, _, leaf = spelling.rpartition(".")
    dotted = re.escape(spelling)
    aliases: set[str] = set()
    if spelling in text:
        aliases.update(_at(text, "import", rf"import\s+{dotted}\s+as\s+(\w+)", also=""))
        if "import_module" in text:
            importer = rf"[\w.]*import_module\(\s*['\"]{dotted}['\"]"
            aliases |= _bound_on_the_line(text, "import_module", importer)
    if head in text:
        from_import = rf"from\s+{re.escape(head)}\s+import\s+(\([^)]*\)|[^\n]*)"
        for block in _at(text, "from", from_import, also=""):
            for alias in re.findall(rf"\b{re.escape(leaf)}\b(?:\s+as\s+(\w+))?", block):
                aliases.add(alias or leaf)
    return aliases


def _text_patches_of(text: str, module: str, needle: str) -> set[str]:
    """The names *text* patches on the module the regular expression *module* spells.

    *needle* is the literal every spelling of the module starts with: an alias, or
    ``sys.modules[``.
    """
    names: set[str] = set()
    quoted = r"['\"](\w+)['\"]"
    namespace = rf"(?:vars\(\s*{module}\s*\)|{module}\s*\.\s*__dict__)"
    for verb in ("setattr", "delattr", "object"):
        if f"{verb}(" in text:
            names.update(_at(text, verb, rf"{verb}\(\s*{module}\s*,\s*{quoted}", also=""))
    if needle in text:
        names.update(_at(text, needle, rf"{module}\.(\w+)\s*=(?!=)"))
        if "del " in text:
            names.update(_at(text, "del", rf"del\s+{module}\.(\w+)", also=""))
    if "__dict__" in text or "vars(" in text:
        names.update(_at(text, "vars", rf"vars\(\s*{module}\s*\)\s*\[\s*{quoted}", also=""))
        names.update(_at(text, needle, rf"{module}\s*\.\s*__dict__\s*\[\s*{quoted}"))
        for verb in ("setitem", "delitem"):
            if f"{verb}(" in text:
                pattern = rf"{verb}\(\s*{namespace}\s*,\s*{quoted}"
                names.update(_at(text, verb, pattern, also=""))
        if "dict(" in text:
            for block in _at(
                text, "dict", rf"dict\(\s*{namespace}\s*,\s*\{{([^}}]*)\}}", before="."
            ):
                names.update(re.findall(rf"{quoted}\s*:", block))
    names.discard("__dict__")
    return names


#: Literal openings of the patch spellings the regular expression reader never reads,
#: whatever the target: ``patch.multiple`` keywords, ``functools.partial`` and ``getattr``
#: spellings of the patcher, a non-literal or relative ``import_module``,
#: ``sys.modules.get``, and the writing methods of a namespace dict.
_SYNTAX_ONLY = (
    (".multiple(", None),
    ("partial(", r"partial\(\s*[\w.]*(?:patch|mocker)\b"),
    ("getattr(", r"getattr\(\s*(?:mock|patch|mocker|unittest)\b"),
    ("import_module(", r"import_module\(\s*(?![\"'][^.\"'])"),
    ("sys.modules.get(", None),
    (
        "__dict__",
        r"__dict__\s*\.\s*(?:update|pop|setdefault|clear|popitem|__setitem__|__delitem__)\(",
    ),
    (
        "vars(",
        r"vars\([^()]*\)\s*\.\s*(?:update|pop|setdefault|clear|popitem|__setitem__|__delitem__)\(",
    ),
)


def _syntax_only(text: str) -> bool:
    for needle, pattern in _SYNTAX_ONLY:
        if needle not in text:
            continue
        if pattern is None:
            return True
        compiled = re.compile(pattern)
        at = text.find(needle)
        while at != -1:
            if compiled.match(text, at):
                return True
            at = text.find(needle, at + 1)
    return False


def _occurrences(text: str, needle: str) -> Iterator[int]:
    at = text.find(needle)
    while at != -1:
        yield at
        at = text.find(needle, at + 1)


def needs_syntax_tree(text: str, spellings: Mapping[str, str]) -> bool:
    """Whether *text* may patch a facade in a way :func:`text_patched_names` cannot read.

    The regular expressions read a quoted dotted target, an alias bound by an import (or by
    ``alias = import_module("<dotted>")`` / ``sys.modules["<dotted>"]``) and what is done to
    that alias. A file is sent to :func:`patch_events` when it also holds anything those
    cannot follow: a patch spelling in :data:`_SYNTAX_ONLY`; a quoted spelling of a facade
    that is not one of those forms (a module constant, an f-string or concatenation part,
    ``importorskip``, a ``.__dict__`` target); a bare ``import <dotted>``, a
    ``package.leaf`` attribute chain or one through an alias of the parent module
    (:func:`_alias_chain`); or an alias assigned on to another name.
    """
    mentioned = _mentioned(text, spellings)
    if not mentioned:
        return False
    words = _text_words(text) if _plain_leaves(tuple(spellings))[1] else None
    if _syntax_only(text):
        return True
    for spelling in mentioned:
        head, _, leaf = spelling.rpartition(".")
        dotted = re.escape(spelling)
        if spelling in text:
            quoted = targets = 0
            for at in _occurrences(text, spelling):
                if at == 0 or text[at - 1] not in _QUOTES:
                    line = text[text.rfind("\n", 0, at) + 1 : at]
                    rest = text[at + len(spelling) : at + len(spelling) + 8]
                    if re.fullmatch(r"\s*import\s+", line) and not re.match(r"\s+as\b", rest):
                        return True  # a bare ``import <dotted>``: patched as a dotted chain
                    continue
                after = text[at + len(spelling) : at + len(spelling) + 10]
                if after[:1] not in (".", "'", '"'):
                    continue
                quoted += 1
                if re.match(r"\.(?!__dict__['\"])\w", after):
                    targets += 1
            importer = rf"[\w.]*import_module\(\s*['\"]{dotted}['\"]\s*\)"
            bound = len(_bound_on_the_line(text, "import_module", importer)) if quoted else 0
            bound += len(_at(text, "sys.modules[", rf"(sys)\.modules\[\s*['\"]{dotted}['\"]\s*\]"))
            if quoted > targets + bound:
                return True
        head_leaf = head.rpartition(".")[2]
        chain = f"{head_leaf}.{leaf}"
        if head_leaf and chain in text:
            for at in _occurrences(text, chain):
                end = at + len(chain)
                line = text[text.rfind("\n", 0, at) + 1 : at].lstrip()
                if line.startswith(("from ", "import ")) or not _starts_a_name(text, at, ".'\""):
                    continue
                if end >= len(text) or not (text[end].isalnum() or text[end] == "_"):
                    return True
        if _alias_chain(text, spelling, words):
            return True  # ``<alias of the parent>.<leaf>``: patched as a dotted chain
        for alias in _text_aliases(text, spelling):
            if re.search(rf"=\s*{re.escape(alias)}\s*(?:#.*)?$", text, re.MULTILINE):
                return True
    return False


def global_reads(source: str, filename: str) -> set[str]:
    """Every name *source*'s compiled code reads or rebinds as a module global."""
    found: set[str] = set()
    stack = [compile(source, filename, "exec")]
    while stack:
        code = stack.pop()
        for instruction in dis.get_instructions(code):
            if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"}:
                found.add(instruction.argval)
        stack += [const for const in code.co_consts if isinstance(const, CodeType)]
    return found
