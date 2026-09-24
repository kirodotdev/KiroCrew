"""The display redaction battery reads no mutable state beyond ``_exempt_exact_hosts``.

``kiro_crew.dashboard.chat_utils._redact_for_display`` memoizes the output of
``redact_exfiltration_urls`` followed by ``redact_credentials``, keyed on the
input text and the exempt-host set. That key is only sound while the battery's
OUTPUT is a pure function of exactly those two inputs. Nothing on the
``security/`` side is told its output is memoized, so a battery change that
starts reading a second mutable input -- an operator file, the clock, the
environment, a memo that a later call can repopulate differently -- would make
every retained entry silently stale.

This module pins that contract on the ``security/`` SOURCE: every function the
battery reaches, statically, reads no mutable module global and performs no
I/O or clock read, with two named carve-outs:

* ``_exempt_exact_hosts`` -- the ONE mutable read the cache key covers.
* ``_slack_manifest_re_slot`` -- a write-once memo of a bundled package
  resource that is constant for the life of the process; a fresh process
  starts with an empty cache anyway.

The OAuth operator-file carve-out lives on the same per-URL classifier, behind
the ``allow_oauth_entropy`` flag. The battery never opens that flag, so the
guard pins both halves: no battery-reachable caller passes the flag, and the
one call that reads the operator file sits under that flag's guard.
"""

from __future__ import annotations

import ast
import inspect
from collections import deque

import pytest

import kiro_crew.dashboard.chat_utils as chat_utils
from kiro_crew.security import exfil, redaction

# The two battery entry points, as the display cache composes them.
BATTERY_ROOTS: tuple[tuple[object, str], ...] = (
    (exfil, "redact_exfiltration_urls"),
    (redaction, "redact_credentials"),
)

# The one mutable read the cache key accounts for.
ALLOWED_MUTABLE_READER = "_exempt_exact_hosts"

# Module globals a reachable function may read although they are mutable
# containers, each with the reason the cache key need not cover it.
ALLOWED_MUTABLE_GLOBALS: dict[str, str] = {
    "_slack_manifest_re_slot": (
        "write-once memo of the bundled Slack manifest template, a package "
        "resource fixed for the life of the process"
    ),
}

# The classifier flag that opens the OAuth operator-file carve-out, and the
# function behind it. The battery must leave the flag closed.
OAUTH_GATE_FLAG = "allow_oauth_entropy"
OAUTH_GATE_KEYWORDS = frozenset({OAUTH_GATE_FLAG, "assume_oauth_endpoint"})
OAUTH_FILE_READER = "_approved_oauth_authorization_endpoint"
GATED_CLASSIFIER = "_exfil_url_warning"

# Names and attributes whose presence means a function reads state the cache
# key does not cover: filesystem, environment, clock, randomness, the
# platform context, or the operator OAuth extension machinery.
STATE_READ_TOKENS = frozenset(
    {
        OAUTH_FILE_READER,
        "_load_operator_oauth_endpoints",
        "_emit_oauth_extension_used_event",
        "_OAUTH_EXTENSION_MEMO",
        "_OAUTH_EXTENSION_AUDITED",
        "oauth_endpoints_path",
        "config_loader",
        "installed_context",
        "resolve_context",
        "PlatformContext",
        "open",
        "read_text",
        "read_bytes",
        "stat",
        "environ",
        "getenv",
        "now",
        "time",
        "monotonic",
        "perf_counter",
        "random",
        "urandom",
        "token_bytes",
    }
)

MUTABLE_CONSTRUCTORS = frozenset(
    {"dict", "set", "list", "OrderedDict", "defaultdict", "deque", "bytearray"}
)

# Method names that mutate a container in place. A module global built as a
# container but never written through any of these, never subscript-assigned,
# never deleted from and never rebound is a constant lookup table, and reading
# one is not reading state.
MUTATOR_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "add",
        "update",
        "clear",
        "pop",
        "popitem",
        "setdefault",
        "remove",
        "discard",
        "move_to_end",
    }
)


def _module_tree(module: object) -> ast.Module:
    return ast.parse(inspect.getsource(module))


def _function_defs(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _relative_imports(tree: ast.Module) -> dict[str, str]:
    """Map a name imported from a sibling ``security`` module to that module's name."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
    return out


def _is_mutable_binding(value: ast.AST | None) -> bool:
    """Is a module-level assignment a mutable container?

    A ``re.compile``, a ``frozenset``, a literal, a tuple or a type alias is
    immutable and cannot change between two calls. A dict, set or list -- built
    by literal, comprehension or constructor -- can, and reading one is reading
    state the cache key does not cover.
    """
    if value is None:
        return True
    if isinstance(value, (ast.Dict, ast.Set, ast.List, ast.ListComp, ast.SetComp, ast.DictComp)):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name in MUTABLE_CONSTRUCTORS
    return False


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _written_globals(tree: ast.Module) -> set[str]:
    """Module globals some function body mutates in place, rebinds or deletes from."""
    out: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Global):
                out.update(node.names)
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, (ast.Subscript, ast.Attribute)):
                        root = _root_name(target)
                        if root:
                            out.add(root)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    root = _root_name(target)
                    if root:
                        out.add(root)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in MUTATOR_METHODS
            ):
                root = _root_name(node.func.value)
                if root:
                    out.add(root)
    return out


def _mutable_globals(tree: ast.Module) -> set[str]:
    """Module globals that are mutable containers AND have a writer in the module."""
    return _constructed_mutable_globals(tree) & _written_globals(tree)


def _constructed_mutable_globals(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value: ast.AST | None = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        if _is_mutable_binding(value):
            out.update(targets)
    return out


def _called_names(fn: ast.FunctionDef) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _tokens(fn: ast.FunctionDef) -> set[str]:
    """Every bare name and every attribute name a function body mentions."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


class _Graph:
    def __init__(self) -> None:
        self.trees = {m.__name__.rsplit(".", 1)[-1]: _module_tree(m) for m in (exfil, redaction)}
        self.defs = {name: _function_defs(tree) for name, tree in self.trees.items()}
        self.imports = {name: _relative_imports(tree) for name, tree in self.trees.items()}
        self.mutable = {name: _mutable_globals(tree) for name, tree in self.trees.items()}

    def resolve(self, module: str, name: str) -> tuple[str, str] | None:
        if name in self.defs[module]:
            return module, name
        target = self.imports[module].get(name)
        if target in self.defs and name in self.defs[target]:
            return target, name
        return None

    def reachable(self) -> set[tuple[str, str]]:
        """Functions the battery reaches, minus the one edge closed by the OAuth flag."""
        seen: set[tuple[str, str]] = set()
        todo = deque(
            (m.__name__.rsplit(".", 1)[-1], root) for m, root in BATTERY_ROOTS  # type: ignore[attr-defined]
        )
        while todo:
            key = todo.popleft()
            if key in seen:
                continue
            seen.add(key)
            module, name = key
            for callee in _called_names(self.defs[module][name]):
                if name == GATED_CLASSIFIER and callee == OAUTH_FILE_READER:
                    continue
                resolved = self.resolve(module, callee)
                if resolved is not None:
                    todo.append(resolved)
        return seen


@pytest.fixture(scope="module")
def graph() -> _Graph:
    return _Graph()


def test_battery_reaches_the_one_allowed_mutable_reader(graph: _Graph) -> None:
    """The guard measures a real graph: the allowed reader is in it, the file reader is not."""
    reached = graph.reachable()
    names = {name for _, name in reached}
    assert ("exfil", ALLOWED_MUTABLE_READER) in reached
    assert ("exfil", GATED_CLASSIFIER) in reached
    assert ("redaction", "redact_credentials") in reached
    assert OAUTH_FILE_READER not in names
    assert "_load_operator_oauth_endpoints" not in names
    assert len(reached) >= 8, sorted(reached)


def test_battery_reads_no_mutable_state_beyond_the_exempt_hosts(graph: _Graph) -> None:
    """No reachable function reads a mutable global, the clock, the environment or a file."""
    violations: list[str] = []
    for module, name in sorted(graph.reachable()):
        if name == ALLOWED_MUTABLE_READER:
            continue
        fn = graph.defs[module][name]
        tokens = _tokens(fn)
        for token in sorted(tokens & STATE_READ_TOKENS):
            # The one call the flag guards; its guard is pinned by its own test.
            if name == GATED_CLASSIFIER and token == OAUTH_FILE_READER:
                continue
            violations.append(f"{module}.{name} reads {token}")
        for token in sorted(tokens & graph.mutable[module]):
            if token not in ALLOWED_MUTABLE_GLOBALS:
                violations.append(f"{module}.{name} reads mutable global {token}")
        if any(isinstance(node, ast.Global) for node in ast.walk(fn)):
            violations.append(f"{module}.{name} declares global")
    assert violations == [], "\n".join(violations)


def test_written_globals_are_detected(graph: _Graph) -> None:
    """The writer scan sees the memo and the OAuth machinery, so a constant table is a real verdict."""
    assert {"_slack_manifest_re_slot", "_OAUTH_EXTENSION_MEMO", "_OAUTH_EXTENSION_AUDITED"} <= (
        graph.mutable["exfil"]
    )
    assert "_STRUCTURAL_VALIDATORS" not in graph.mutable["exfil"]


def test_allowed_mutable_globals_are_still_mutable_and_still_read(graph: _Graph) -> None:
    """Each carve-out names a binding that exists and is read, so a stale entry is caught."""
    for global_name in ALLOWED_MUTABLE_GLOBALS:
        module = next(m for m, names in graph.mutable.items() if global_name in names)
        readers = {
            name
            for mod, name in graph.reachable()
            if mod == module and global_name in _tokens(graph.defs[mod][name])
        }
        assert readers, f"{global_name} is allowed yet no reachable function reads it"


def test_battery_leaves_the_oauth_gate_closed(graph: _Graph) -> None:
    """No battery-reachable caller opens the OAuth carve-out on the classifier."""
    offenders: list[str] = []
    for module, name in sorted(graph.reachable()):
        for node in ast.walk(graph.defs[module][name]):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != GATED_CLASSIFIER:
                continue
            passed = {kw.arg for kw in node.keywords if kw.arg} & OAUTH_GATE_KEYWORDS
            if passed:
                offenders.append(f"{module}.{name} passes {sorted(passed)}")
    assert offenders == []


def test_operator_file_read_sits_under_the_oauth_flag(graph: _Graph) -> None:
    """Inside the classifier, the only file-reading call is guarded by the flag.

    Pinning the guard is what lets the reachability walk skip that one edge: a
    refactor that lifts the call out from under the flag is caught here, and a
    refactor that renames the flag is caught by the parameter check.
    """
    fn = graph.defs["exfil"][GATED_CLASSIFIER]
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    assert OAUTH_GATE_FLAG in params
    guarded_calls = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.And):
            continue
        if not (isinstance(node.values[0], ast.Name) and node.values[0].id == OAUTH_GATE_FLAG):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == OAUTH_FILE_READER
            ):
                guarded_calls += 1
    all_calls = sum(
        1
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == OAUTH_FILE_READER
    )
    assert all_calls >= 1
    assert guarded_calls == all_calls


def test_display_cache_memoizes_exactly_the_guarded_battery() -> None:
    """The consumer composes exactly the two roots this guard walks, in order."""
    fn = ast.parse(inspect.getsource(chat_utils._redact_for_display)).body[0]
    assert isinstance(fn, ast.FunctionDef)
    redactor_calls = [
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("redact_")
    ]
    assert redactor_calls == [root for _, root in BATTERY_ROOTS]


def test_display_battery_never_touches_the_operator_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavioural half of the guard: the display battery never asks the operator file.

    A consent URL at a builtin OAuth endpoint with an over-length query is the
    exact input the carve-out exists for. Under the display battery it is judged
    on the strict path, so neither the endpoint check nor the file loader runs.
    """

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the display battery must not consult the OAuth operator file")

    monkeypatch.setattr(exfil, OAUTH_FILE_READER, forbidden)
    monkeypatch.setattr(exfil, "_load_operator_oauth_endpoints", forbidden)
    seen = {"n": 0}
    real = exfil._exempt_exact_hosts

    def counted() -> frozenset[str]:
        seen["n"] += 1
        return real()

    monkeypatch.setattr(exfil, ALLOWED_MUTABLE_READER, counted)
    chat_utils._clear_display_redaction_cache()
    consent = (
        "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state="
        + "A" * 700
        + "&code_challenge="
        + "B" * 43
    )
    for text in (consent, "key AKIAIOSFODNN7EXAMPLE here", "plain prose with no url"):
        chat_utils._redact_for_display(text)
    assert seen["n"] >= 3, "the cache key and the battery both read the exempt set"
