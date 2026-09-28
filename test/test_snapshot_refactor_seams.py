"""A patch on ``kiro_crew.snapshot`` of a helper a test replaces reaches every call site.

The owner modules read each helper, driver and limit in ``LATE_BOUND`` through
``snapshot_components._facade()`` when they use it, so a patch of one of those names on the
facade reaches the owners' call sites too, ``kiro_crew.snapshot`` stays a plain module, and
undoing a patch is plain attribute assignment. Every other name an owner uses resolves in
that owner's own globals, so a patch of it on the facade reaches the facade's code only.

Each seam below is patched on the facade ONLY and driven through a path whose call site
lives in an owner, so a patch that stops reaching that call site fails here by name rather
than turning a failure-injection test elsewhere into a pass that proves nothing. The tests
after them pin the late binding itself: which names go through the facade, that every use
of them does, and that the facade stays a plain module with nothing to mirror. The last
ones scan ``test/`` and every ``tests`` package under ``src/kiro_crew`` and fail on a patch
no call site sees: a facade patch of a name an owner reads itself, or an owner patch of a
``LATE_BOUND`` name.
"""

from __future__ import annotations

import ast
import functools
import importlib
import io
import os
import re
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from test_snapshot import _make_snapshot, _setup_fake_kirocrew, unpinnable_argv

from conftest import requires_o_nofollow
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_archive, snapshot_components, snapshot_merge, snapshot_restore
from kiro_crew.jsonl_util import OversizedRecord

_OWNERS = (snapshot_components, snapshot_archive, snapshot_restore, snapshot_merge)
_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_SRC = _REPO_ROOT / "src"

_LIVE_RECORD = b'{"ts":"2026-02-01T00:00:00Z","msg":"local"}\n'
_BUNDLE_RECORD = b'{"ts":"2026-03-01T00:00:00Z","msg":"snap"}\n'


def _caller_module() -> str:
    """The module whose code called the sentinel that is asking."""
    return str(sys._getframe(2).f_globals.get("__name__", "?"))


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """Replace ``snapshot.<name>`` with a pass-through recording the module of each caller."""
    real = getattr(snap, name)
    callers: list[str] = []

    def spy(*args, **kwargs):
        callers.append(_caller_module())
        return real(*args, **kwargs)

    monkeypatch.setattr(snap, name, spy)
    return callers


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    _setup_fake_kirocrew(d)
    return d


@pytest.fixture
def bundle(home: Path, tmp_path: Path) -> Path:
    """A complete bundle of *home*, written before any seam is patched."""
    return _make_snapshot(home, tmp_path / "out")


def _replace(bundle: Path) -> int:
    return snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])


# ── The replace transaction ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "_copytree_safe",
        "_do_replace_mutations",
        "_refuse_unsafe_destination_roots",
        "_backup_and_copy",
        "_allocate_rollback_dir",
        "hold_stores_for_replace",
    ],
)
def test_a_facade_spy_sees_the_replace_transaction_call_it(
    name: str, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    callers = _spy(monkeypatch, name)
    assert _replace(bundle) == 0
    assert "kiro_crew.snapshot_restore" in callers, f"snapshot.{name} never reached the owner"


def test_a_facade_failure_injection_reaches_the_rollback(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_a, **_k) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(snap, "_do_replace_mutations", fail)
    callers = _spy(monkeypatch, "_restore_everything_from_rollback")
    assert _replace(bundle) == 1
    assert callers == ["kiro_crew.snapshot_restore"]
    assert "Your previous state was put back" in capsys.readouterr().out


def test_a_facade_clock_names_the_rollback_directory(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    callers: list[str] = []

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            callers.append(_caller_module())
            return datetime.now(tz)

    monkeypatch.setattr(snap, "datetime", _Clock)
    assert _replace(bundle) == 0
    assert "kiro_crew.snapshot_restore" in callers


def test_a_facade_gateway_probe_is_the_one_restore_asks(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deterministic override still says "not running", so a missed patch restores."""
    asked: list[bool] = []

    def running() -> bool:
        asked.append(True)
        return True

    monkeypatch.setattr(snap, "_is_gateway_running", running)
    rc = snap.restore_main([str(bundle), "--mode", "replace", *unpinnable_argv()])
    assert rc == 1
    assert asked == [True]
    assert "Gateway is running" in capsys.readouterr().out


# ── Staging a snapshot ───────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["_chain_is_link_free", "_staging_is_pinned"])
def test_a_facade_spy_sees_the_staging_call_it(
    name: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    callers = _spy(monkeypatch, name)
    assert snap.snapshot_main([str(tmp_path / "out"), *unpinnable_argv()]) == 0
    assert "kiro_crew.snapshot_archive" in callers, f"snapshot.{name} never reached the owner"


def test_a_facade_sqlite_driver_captures_the_databases(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = snap.sqlite3
    callers: list[str] = []

    class _Driver:
        def __getattr__(self, attr: str):
            return getattr(real, attr)

        def connect(self, *args, **kwargs):
            callers.append(_caller_module())
            return real.connect(*args, **kwargs)

    monkeypatch.setattr(snap, "sqlite3", _Driver())
    assert snap.snapshot_main([str(tmp_path / "out"), *unpinnable_argv()]) == 0
    assert "kiro_crew.snapshot_archive" in callers


@pytest.mark.parametrize("copy", ["_copytree_safe", "_copy_tree_no_overwrite"])
def test_a_facade_skip_reporter_is_the_one_a_tree_copy_reports_through(
    copy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive's default reporter and the merge's, each named in its owner's globals."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "kept.txt").write_text("kept", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.write_text("not for the copy", encoding="utf-8")
    try:
        os.link(secret, src / "alias.txt")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")
    reported: list[str] = []
    monkeypatch.setattr(snap, "_report_skip", lambda _reason, path: reported.append(path))

    getattr(snap, copy)(src, tmp_path / "dst", allow_unpinned=True)

    assert [Path(p).name for p in reported] == ["alias.txt"]
    assert not (tmp_path / "dst" / "alias.txt").exists()


def test_a_facade_data_home_bounds_the_tree_root_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "configured"))
    home = tmp_path / "patched"
    (home / "workspace").mkdir(parents=True)
    callers: list[str] = []

    def mc_dir() -> Path:
        callers.append(_caller_module())
        return home

    monkeypatch.setattr(snap, "_mc_dir", mc_dir)
    assert snap.safe_tree_root(home / "workspace", what="component root") == home / "workspace"
    assert callers == ["kiro_crew.snapshot_components"]


# ── Reading an archive ───────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["_MAX_ARCHIVE_MEMBERS", "_MAX_ARCHIVE_BYTES"])
def test_a_facade_archive_bound_is_the_one_enforced(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "two-files.tar"
    with tarfile.open(archive, "w") as tf:
        for member in ("a.txt", "b.txt"):
            payload = b"0123456789"
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

    monkeypatch.setattr(snap, name, 1)
    with tarfile.open(archive) as tf, pytest.raises(snap._ArchiveTooLarge):
        snap._refuse_oversized_archive(tf)


# ── Notification records ─────────────────────────────────────────────────────


def _notification_files(tmp_path: Path, bundle_bytes: bytes) -> tuple[Path, Path]:
    src = tmp_path / "snap-notifications.jsonl"
    dst = tmp_path / "live-notifications.jsonl"
    src.write_bytes(bundle_bytes)
    dst.write_bytes(_LIVE_RECORD)
    return src, dst


@pytest.mark.parametrize("name", ["_notification_key", "strict_raw_records"])
def test_a_facade_spy_sees_the_notification_merge_call_it(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    callers = _spy(monkeypatch, name)
    snap._merge_notifications(*_notification_files(tmp_path, _BUNDLE_RECORD))
    assert "kiro_crew.snapshot_merge" in callers, f"snapshot.{name} never reached the owner"


def test_a_facade_record_cap_is_the_one_the_merge_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snap, "_NOTIFICATION_RECORD_CAP", 64)
    oversized = b'{"ts":"2026-03-06T00:00:00Z","msg":"' + b"x" * 200 + b'"}\n'
    with pytest.raises(OversizedRecord):
        snap._merge_notifications(*_notification_files(tmp_path, _BUNDLE_RECORD + oversized))


@requires_o_nofollow
def test_a_facade_source_cap_is_the_one_the_copy_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snap, "_NOTIFICATION_SOURCE_CAP", 64)
    src = tmp_path / "snap-notifications.jsonl"
    src.write_bytes(_BUNDLE_RECORD * 4)
    dst = tmp_path / "home" / "notifications.jsonl"
    dst.parent.mkdir()
    with pytest.raises(OSError) as caught:
        snap._copy_notifications(src, dst)
    assert "64" in str(caught.value)
    assert not dst.exists()


# ── The late binding itself ──────────────────────────────────────────────────

#: The names the owner modules read through the facade when they use them: every helper,
#: driver and limit a test replaces on ``kiro_crew.snapshot`` and an owner calls or reads.
LATE_BOUND = frozenset(
    {
        "_MAX_ARCHIVE_BYTES",
        "_MAX_ARCHIVE_MEMBERS",
        "_NOTIFICATION_RECORD_CAP",
        "_NOTIFICATION_SOURCE_CAP",
        "_allocate_rollback_dir",
        "_backup_and_copy",
        "_chain_is_link_free",
        "_copytree_safe",
        "_do_replace_mutations",
        "_mc_dir",
        "_notification_key",
        "_refuse_unsafe_destination_roots",
        "_report_skip",
        "_restore_everything_from_rollback",
        "_staging_is_pinned",
        "datetime",
        "hold_stores_for_replace",
        "sqlite3",
        "strict_raw_records",
    }
)


def _owner_trees() -> list[tuple[str, ast.Module]]:
    return [(m.__name__, ast.parse(Path(m.__file__).read_text(encoding="utf-8"))) for m in _OWNERS]


def _is_facade_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_facade"
        and not node.args
    )


def test_every_owner_use_of_a_late_bound_name_goes_through_the_facade() -> None:
    """No owner reads one of these from its own globals, where a facade patch cannot reach."""
    direct: list[str] = []
    for module, tree in _owner_trees():
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in LATE_BOUND
                ):
                    direct.append(f"{module}:{node.lineno} {node.id}")
    assert direct == [], f"read from the owner's own globals, not the facade: {direct}"


def _holds_the_facade(node: ast.AST) -> bool:
    """``_facade()``, or the local ``facade`` a function binds to it before its loops."""
    return _is_facade_call(node) or (isinstance(node, ast.Name) and node.id == "facade")


def test_the_owners_read_only_the_declared_names_through_the_facade() -> None:
    read = {
        node.attr
        for _, tree in _owner_trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and _holds_the_facade(node.value)
    }
    assert read == LATE_BOUND
    assert all(hasattr(snap, name) for name in LATE_BOUND)


def test_a_local_facade_is_bound_once_from_the_resolver_before_any_use() -> None:
    """``facade`` is the first statement of the function that holds it: ``facade = _facade()``."""
    wrong: list[str] = []
    for module, tree in _owner_trees():
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            uses = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "facade"]
            if not uses and all(a.arg != "facade" for a in ast.walk(fn) if isinstance(a, ast.arg)):
                continue
            body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
            first = body[0]
            bound = (
                isinstance(first, ast.Assign)
                and [ast.dump(t) for t in first.targets]
                == [ast.dump(ast.Name("facade", ast.Store()))]
                and _is_facade_call(first.value)
            )
            stores = [n for n in uses if not isinstance(n.ctx, ast.Load)]
            if (
                not bound
                or len(stores) != 1
                or any(a.arg == "facade" for a in ast.walk(fn) if isinstance(a, ast.arg))
            ):
                wrong.append(f"{module}.{fn.name}")
    assert wrong == []


def _per_iteration_facade_calls(tree: ast.AST) -> list[int]:
    """Lines where ``_facade()`` runs once per iteration of a loop or comprehension."""
    lines: list[int] = []
    for loop in ast.walk(tree):
        if isinstance(loop, (ast.For, ast.AsyncFor)):
            repeated: list[ast.AST] = list(loop.body)
        elif isinstance(loop, ast.While):
            repeated = [loop.test, *loop.body]
        elif isinstance(loop, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            elements = [loop.key, loop.value] if isinstance(loop, ast.DictComp) else [loop.elt]
            repeated = [
                *elements,
                *(test for gen in loop.generators for test in gen.ifs),
                *(gen.iter for gen in loop.generators[1:]),
            ]
        else:
            continue
        lines += [n.lineno for node in repeated for n in ast.walk(node) if _is_facade_call(n)]
    return lines


def test_no_owner_resolves_the_facade_once_per_record() -> None:
    """A function resolves the facade once, before its loops, never once per iteration."""
    hot = [
        f"{module}:{line}"
        for module, tree in _owner_trees()
        for line in _per_iteration_facade_calls(tree)
    ]
    assert hot == []


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("for x in y:\n    _facade().a(x)\n", [2]),
        ("while _facade().a:\n    pass\n", [1]),
        ("[_facade().a(x) for x in y]\n", [1]),
        ("[x for x in y if _facade().a(x)]\n", [1]),
        ("for x in _facade().a():\n    pass\n", []),
        ("[x for x in _facade().a()]\n", []),
        ("facade = _facade()\nfor x in y:\n    facade.a(x)\n", []),
    ],
    ids=[
        "for-body",
        "while-test",
        "comprehension",
        "comprehension-if",
        "for-iter",
        "first-iter",
        "hoisted",
    ],
)
def test_the_per_iteration_check_tells_a_loop_body_from_a_loop_header(
    source: str, lines: list[int]
) -> None:
    assert _per_iteration_facade_calls(ast.parse(source)) == lines


def test_the_facade_resolver_looks_the_facade_up_and_never_imports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_facade()`` reads ``sys.modules`` only: no owner depends on the facade by import.

    The facade imports every owner and is the only way into them, so it is loaded before any
    owner function runs; with no facade loaded there is nothing to resolve, and the lookup
    says so instead of importing it.
    """
    stand_in = ModuleType("kiro_crew.snapshot")
    monkeypatch.setitem(sys.modules, "kiro_crew.snapshot", stand_in)
    with mock.patch("importlib.import_module", side_effect=AssertionError("imported")):
        assert snapshot_components._facade() is stand_in
    monkeypatch.delitem(sys.modules, "kiro_crew.snapshot")
    with pytest.raises(KeyError):
        snapshot_components._facade()


def test_the_facade_is_a_plain_module_the_type_checker_sees_whole() -> None:
    """No module ``__getattr__`` and no class swap: there is nothing to mirror and no stale copy.

    Every name the facade exposed before the split is bound in its namespace by an import or
    a definition, so mypy resolves ``kiro_crew.snapshot.<name>`` against the real object with
    no ``TYPE_CHECKING`` guard.
    """
    from test_snapshot_refactor_contracts import FACADE_NAMES

    assert type(snap) is ModuleType
    assert "__getattr__" not in vars(snap)
    assert sorted(name for name in FACADE_NAMES if name not in vars(snap)) == []


# ── A patch on the facade that its owners cannot see ─────────────────────────

_FACADE = snap.__name__
_DYNAMIC = "<dynamic>"
_PATCHERS = ("unittest.mock.patch", "mock.patch")
#: ``patch.multiple`` keywords that configure the patch rather than name an attribute.
_MULTIPLE_OPTIONS = frozenset({"target", "spec", "create", "spec_set", "autospec", "new_callable"})
_MISSING = object()


@functools.lru_cache(maxsize=None)
def _names_owners_read_directly() -> frozenset[str]:
    """Facade names an owner function reads from its own globals, bound to the same object.

    A patch of one of these on ``kiro_crew.snapshot`` rebinds the facade's name only: the
    owner keeps calling what its own global holds.
    """
    names: set[str] = set()
    for owner, (_, tree) in zip(_OWNERS, _owner_trees()):
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in vars(owner)
                    and vars(snap).get(node.id, _MISSING) is vars(owner)[node.id]
                ):
                    names.add(node.id)
    return frozenset(names - LATE_BOUND)


#: The modules a snapshot test patches: the facade and the four owners, by name.
_PATCHABLE: dict[str, ModuleType] = {_FACADE: snap, **{m.__name__: m for m in _OWNERS}}
_STEMS = {name.rpartition(".")[2]: name for name in _PATCHABLE}


@functools.lru_cache(maxsize=None)
def _patched_module(dotted: str) -> str | None:
    """The facade or owner module *dotted* names, directly or as a module attribute."""
    if dotted in _PATCHABLE:
        return dotted
    parts = dotted.split(".")
    module = _STEMS.get(parts[-1])
    if module is None or (parts[0] != "kiro_crew" and parts[0] not in sys.modules):
        return None
    for cut in range(len(parts) - 1, 0, -1):
        try:
            obj: object = importlib.import_module(".".join(parts[:cut]))
        except Exception:  # noqa: BLE001 - an unimportable prefix names no snapshot module
            continue
        for attr in parts[cut:]:
            obj = getattr(obj, attr, None)
        return module if obj is _PATCHABLE[module] else None
    return None


class _FacadePatches(ast.NodeVisitor):
    """Every patch in one module's source that rebinds a name on the facade or an owner.

    ``hits`` holds ``(enclosing function, line, module, name)``, the name being
    ``<dynamic>`` for a patch whose attribute is not a constant the scan can resolve. The
    target is read from AST nodes: a module alias bound by any import form, by ``importlib.import_module``,
    ``pytest.importorskip`` or ``sys.modules[...]``, or by simple assignment from one of
    those, resolved to a fixed point; and a string target given as a constant, a module-level
    string constant, an f-string over those and ``<alias>.__name__``, or a concatenation of
    them. An attribute given as a local name of the enclosing function resolves to every
    constant that name is assigned, ``a if c else b`` included. The patches covered are ``unittest.mock.patch`` with ``.object`` and ``.multiple``
    under any alias, called or used as a decorator; ``monkeypatch.setattr``/``delattr`` in
    object and string form. ``target=``, ``attribute=`` and ``name=`` keyword
    forms count like their positional ones.
    """

    def __init__(self, tree: ast.Module) -> None:
        self.hits: list[tuple[str, int, str, str]] = []
        self._scope: list[str] = []
        self._functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self._constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self._aliases: dict[str, str] = {}
        self._bind_aliases(tree)
        self.visit(tree)

    # Resolving names ------------------------------------------------------------------

    def _bind_aliases(self, tree: ast.Module) -> None:
        bindings = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))
        ]
        changed = True
        while changed:
            changed = False
            for node in bindings:
                for name, dotted in self._binds(node):
                    current = self._aliases.get(name)
                    if current is None or (
                        _patched_module(current) is None and _patched_module(dotted) is not None
                    ):
                        if current != dotted:
                            self._aliases[name] = dotted
                            changed = True

    def _binds(self, node: ast.AST) -> list[tuple[str, str]]:
        if isinstance(node, ast.Import):
            return [
                (a.asname, a.name) if a.asname else (a.name.split(".")[0], a.name.split(".")[0])
                for a in node.names
            ]
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                return []
            return [(a.asname or a.name, f"{node.module}.{a.name}") for a in node.names]
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        else:
            return []
        dotted = self._dotted(value) if value is not None else None
        return [(t, dotted) for t in targets] if dotted else []

    def _dotted(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, ast.Call) and node.args:
            if self._dotted(node.func) in {"importlib.import_module", "pytest.importorskip"}:
                return self._text(node.args[0])
        if isinstance(node, ast.Subscript) and self._dotted(node.value) == "sys.modules":
            return self._text(node.slice)
        return None

    def _module_of(self, node: ast.AST | None) -> str | None:
        dotted = self._dotted(node) if node is not None else None
        return _patched_module(dotted) if dotted is not None else None

    def _text(self, node: ast.AST) -> str | None:
        """The full constant string *node* evaluates to, or ``None``."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return self._constants.get(node.id)
        if isinstance(node, ast.JoinedStr):
            parts = [self._part(v) for v in node.values]
            return None if None in parts else "".join(p for p in parts if p is not None)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self._text(node.left), self._text(node.right)
            return None if left is None or right is None else left + right
        return None

    def _texts(self, node: ast.AST, *, local: bool = True) -> set[str] | None:
        """Every constant string *node* can evaluate to, or ``None`` when one is unknown.

        Beyond :meth:`_text`, this follows ``a if c else b`` and a local name of the
        enclosing function that is only ever assigned such values.
        """
        full = self._text(node)
        if full is not None:
            return {full}
        if isinstance(node, ast.IfExp):
            body, orelse = self._texts(node.body, local=local), self._texts(
                node.orelse, local=local
            )
            return None if body is None or orelse is None else body | orelse
        if local and isinstance(node, ast.Name) and self._functions:
            return self._local_values(self._functions[-1], node.id)
        return None

    def _local_values(
        self, fn: ast.FunctionDef | ast.AsyncFunctionDef, name: str
    ) -> set[str] | None:
        if any(a.arg == name for a in ast.walk(fn) if isinstance(a, ast.arg)):
            return None
        stores = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load)
        ]
        assigns = [
            a
            for a in ast.walk(fn)
            if isinstance(a, ast.Assign)
            and len(a.targets) == 1
            and isinstance(a.targets[0], ast.Name)
            and a.targets[0].id == name
        ]
        if not assigns or len(stores) != len(assigns):
            return None
        values: set[str] = set()
        for assign in assigns:
            found = self._texts(assign.value, local=False)
            if found is None:
                return None
            values |= found
        return values

    def _part(self, value: ast.AST) -> str | None:
        if isinstance(value, ast.Constant):
            return str(value.value)
        if not isinstance(value, ast.FormattedValue) or value.conversion != -1 or value.format_spec:
            return None
        inner = value.value
        if isinstance(inner, ast.Attribute) and inner.attr == "__name__":
            dotted = self._dotted(inner.value)
            return (_patched_module(dotted) or dotted) if dotted else None
        return self._text(inner)

    def _prefix(self, node: ast.AST) -> str:
        """The leading part of a string target that resolves to a constant."""
        full = self._text(node)
        if full is not None:
            return full
        if isinstance(node, ast.JoinedStr):
            lead = ""
            for value in node.values:
                part = self._part(value)
                if part is None:
                    break
                lead += part
            return lead
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._text(node.left)
            return self._prefix(node.left) if left is None else left + self._prefix(node.right)
        return ""

    # Recording patches ----------------------------------------------------------------

    def _record(self, node: ast.AST, module: str, name: str) -> None:
        scope = ".".join(self._scope) or "<module>"
        self.hits.append((scope, getattr(node, "lineno", 0), module, name))

    def _object_target(self, node: ast.AST, obj: ast.AST | None, attr: ast.AST | None) -> None:
        module = self._module_of(obj)
        if module is None:
            return
        names = self._texts(attr) if attr is not None else None
        for name in sorted(names) if names is not None else [_DYNAMIC]:
            self._record(node, module, name)

    def _string_target(self, node: ast.AST, target: ast.AST | None) -> None:
        if target is None:
            return
        full = self._text(target)
        if full is not None:
            owner, _, name = full.rpartition(".")
            module = _patched_module(owner) if owner else None
            if module is not None:
                self._record(node, module, name)
            return
        lead = self._prefix(target)
        for module in _PATCHABLE:
            if lead.startswith(module + ".") and "." not in lead[len(module) + 1 :]:
                self._record(node, module, _DYNAMIC)

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self._scope.append(node.name)
        if isinstance(node, ast.ClassDef):
            self.generic_visit(node)
        else:
            self._functions.append(node)
            self.generic_visit(node)
            self._functions.pop()
        self._scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        func = self._dotted(node.func)
        args = node.args
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if func in _PATCHERS:
            self._string_target(node, args[0] if args else kw.get("target"))
        elif func in {f"{p}.object" for p in _PATCHERS}:
            self._object_target(
                node,
                args[0] if args else kw.get("target"),
                args[1] if len(args) > 1 else kw.get("attribute"),
            )
        elif func in {f"{p}.multiple" for p in _PATCHERS}:
            target = args[0] if args else kw.get("target")
            text = self._text(target) if target is not None else None
            module = self._module_of(target) or (text if text in _PATCHABLE else None)
            if module is not None:
                for keyword in node.keywords:
                    if keyword.arg is None:
                        self._record(node, module, _DYNAMIC)
                    elif keyword.arg not in _MULTIPLE_OPTIONS:
                        self._record(node, module, keyword.arg)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {"setattr", "delattr"}:
            # ``monkeypatch.setattr(obj, name, value)`` or ``(dotted, value)``; ``delattr``
            # takes ``(obj, name)`` or ``(dotted)``.
            object_form = len(args) >= (3 if node.func.attr == "setattr" else 2) or "name" in kw
            if object_form:
                self._object_target(
                    node,
                    args[0] if args else kw.get("target"),
                    args[1] if len(args) > 1 else kw.get("name"),
                )
            else:
                self._string_target(node, args[0] if args else kw.get("target"))
        self.generic_visit(node)


def _wrong_target(hits: list[tuple[str, int, str, str]]) -> list[tuple[str, int, str, str]]:
    """The patches no call site sees.

    On the facade: a name an owner reads from its own globals. On an owner: a ``LATE_BOUND``
    name, which every owner reads through the facade instead. Either way, a patch whose name
    the scan cannot resolve.
    """
    direct = _names_owners_read_directly()
    return [
        hit
        for hit in hits
        if hit[3] == _DYNAMIC or hit[3] in (direct if hit[2] == _FACADE else LATE_BOUND)
    ]


def _scanned_files() -> list[Path]:
    roots = [_REPO_ROOT / "test", *(_REPO_SRC / "kiro_crew").rglob("tests")]
    return sorted({f for root in roots if root.is_dir() for f in root.rglob("*.py")})


#: The facade patches whose attribute is a parameter, not a constant, keyed by (path,
#: enclosing function): the seam spy and the parametrized archive-bound test above. Each
#: is driven by ``LATE_BOUND`` names only and asserts that an owner saw the patch, so a
#: name outside that set fails there by name.
_DYNAMIC_FACADE_PATCHES = frozenset(
    {
        ("test/test_snapshot_refactor_seams.py", "_spy"),
        ("test/test_snapshot_refactor_seams.py", "test_a_facade_archive_bound_is_the_one_enforced"),
    }
)

#: The one deliberate owner patch of a ``LATE_BOUND`` name, keyed by (path, enclosing test
#: function, module, name): the ownership test that patches it to prove the owner's own
#: binding is not consulted.
_PREMISE_PATCHES = frozenset(
    {
        (
            "test/test_snapshot_refactor_ownership.py",
            "test_a_helper_patched_on_the_facade_is_the_one_its_owner_calls",
            "kiro_crew.snapshot_restore",
            "_do_replace_mutations",
        ),
    }
)


def test_no_patch_targets_a_module_whose_call_sites_do_not_read_it() -> None:
    """A patch of a name no call site reads from the patched module proves nothing.

    Scans every Python file under ``test/`` and every ``tests`` package in ``src/kiro_crew``
    that can name the facade or an owner: one that spells ``kiro_crew.snapshot`` or
    ``kiro_crew.snapshot_<owner>``, imports such a name, or reads it as an attribute. On the
    facade, a patch of a name an owner reads from its own globals fails; so does, on an
    owner, a patch of a ``LATE_BOUND`` name, which the owners read through the facade. A
    patch whose name the scan cannot resolve is allowed only at the sites in
    ``_DYNAMIC_FACADE_PATCHES``, the one premise test in ``_PREMISE_PATCHES`` is exempt, and
    the raw scan must equal exactly those two lists.
    """
    word = r"snapshot(?:_(?:archive|components|merge|restore))?\b"
    mentions = re.compile(
        rf"kiro_crew\.{word}|\.{word}|\bimport\s+[^\n(]*\b{word}|\bimport\s*\([^)]*\b{word}"
    )
    found: list[tuple[str, str, int, str, str]] = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not mentions.search(text):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:  # pragma: no cover - a fixture that is not Python source
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        found += [
            (rel, fn, line, module, name)
            for fn, line, module, name in _wrong_target(_FacadePatches(tree).hits)
        ]
    assert {(rel, fn, module, name) for rel, fn, _line, module, name in found} == {
        (rel, fn, _FACADE, _DYNAMIC) for rel, fn in _DYNAMIC_FACADE_PATCHES
    } | _PREMISE_PATCHES, found


def test_the_names_owners_read_directly_include_the_tree_root_check() -> None:
    direct = _names_owners_read_directly()
    assert "safe_tree_root" in direct
    assert direct.isdisjoint(LATE_BOUND)


_ALIAS = "from kiro_crew import snapshot as snap\nfrom unittest import mock\n"


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        (_ALIAS + 'monkeypatch.setattr(snap, "safe_tree_root", None)\n', ["safe_tree_root"]),
        (
            _ALIAS + 'monkeypatch.setattr(target=snap, name="safe_tree_root", value=None)\n',
            ["safe_tree_root"],
        ),
        (
            _ALIAS + 'monkeypatch.setattr("kiro_crew.snapshot.safe_tree_root", None)\n',
            ["safe_tree_root"],
        ),
        (_ALIAS + 'monkeypatch.delattr(snap, "safe_tree_root")\n', ["safe_tree_root"]),
        (_ALIAS + 'mock.patch.object(snap, "safe_tree_root")\n', ["safe_tree_root"]),
        (
            _ALIAS + 'mock.patch.object(target=snap, attribute="safe_tree_root")\n',
            ["safe_tree_root"],
        ),
        (_ALIAS + 'mock.patch(target="kiro_crew.snapshot.safe_tree_root")\n', ["safe_tree_root"]),
        (
            "from unittest.mock import patch as P\n"
            '@P("kiro_crew.snapshot.safe_tree_root")\ndef test_x():\n    pass\n',
            ["safe_tree_root"],
        ),
        (
            "import unittest.mock as um\n"
            'um.patch.multiple("kiro_crew.snapshot", safe_tree_root=None, create=True)\n',
            ["safe_tree_root"],
        ),
        (
            "from unittest import mock as M\nimport kiro_crew.snapshot as s\n"
            'M.patch.object(s, "safe_tree_root", create=True)\n',
            ["safe_tree_root"],
        ),
        (
            "import kiro_crew.snapshot\nfrom unittest import mock\n"
            'mock.patch(f"{kiro_crew.snapshot.__name__}.safe_tree_root")\n',
            ["safe_tree_root"],
        ),
        (_ALIAS + 'mock.patch(f"{snap.__name__}.safe_tree_root")\n', ["safe_tree_root"]),
        (
            'FACADE = "kiro_crew.snapshot"\nfrom unittest import mock\n'
            'mock.patch(f"{FACADE}.safe_tree_root")\nmock.patch(FACADE + ".safe_tree_root")\n',
            ["safe_tree_root", "safe_tree_root"],
        ),
        (
            "import importlib\n"
            's = importlib.import_module("kiro_crew.snapshot")\nt = s\n'
            'monkeypatch.setattr(t, "safe_tree_root", None)\n',
            ["safe_tree_root"],
        ),
        (
            'import sys\ns = sys.modules["kiro_crew.snapshot"]\n'
            'monkeypatch.setattr(s, "safe_tree_root", None)\n',
            ["safe_tree_root"],
        ),
        (
            "from kiro_crew.apps.builtins.aws_control.backend import backup\nbk = backup\n"
            'monkeypatch.setattr(bk.snapshot, "safe_tree_root", None)\n',
            ["safe_tree_root"],
        ),
        (
            _ALIAS + "def test_x(monkeypatch, fast):\n"
            '    method = "safe_tree_root" if fast else "restore_main"\n'
            "    monkeypatch.setattr(snap, method, None)\n",
            ["safe_tree_root"],
        ),
        (
            _ALIAS + "def test_x(monkeypatch, name):\n"
            "    method = name\n    monkeypatch.setattr(snap, method, None)\n",
            [_DYNAMIC],
        ),
        (_ALIAS + "monkeypatch.setattr(snap, name, None)\n", [_DYNAMIC]),
        (_ALIAS + 'mock.patch(f"kiro_crew.snapshot.{name}")\n', [_DYNAMIC]),
        (_ALIAS + "mock.patch.multiple(snap, **values)\n", [_DYNAMIC]),
        (
            "from kiro_crew import snapshot_archive\n"
            'monkeypatch.setattr(snapshot_archive, "_copytree_safe", None)\n',
            ["_copytree_safe"],
        ),
        (
            "from unittest import mock\n"
            'mock.patch("kiro_crew.snapshot_restore._do_replace_mutations")\n',
            ["_do_replace_mutations"],
        ),
        (
            "import kiro_crew.snapshot_merge as m\nfrom unittest import mock\n"
            'mock.patch.object(m, "strict_raw_records")\n',
            ["strict_raw_records"],
        ),
        (
            "from kiro_crew import snapshot_restore\n"
            "monkeypatch.setattr(snapshot_restore, name, None)\n",
            [_DYNAMIC],
        ),
    ],
)
def test_the_patch_scan_flags_a_patch_no_call_site_sees(source: str, flagged: list[str]) -> None:
    hits = _wrong_target(_FacadePatches(ast.parse(source)).hits)
    assert [name for _fn, _line, _module, name in hits] == flagged


@pytest.mark.parametrize(
    "source",
    [
        _ALIAS + 'monkeypatch.setattr(snap, "_copytree_safe", None)\n',
        _ALIAS + "def test_x(monkeypatch, fast):\n"
        '    method = "_copytree_safe" if fast else "_restage_databases"\n'
        "    monkeypatch.setattr(snap, method, None)\n",
        _ALIAS + 'mock.patch("kiro_crew.snapshot._MAX_ARCHIVE_MEMBERS", 1, create=True)\n',
        _ALIAS + 'mock.patch.object(snap, "restore_main")\n',
        "from kiro_crew import snapshot_restore\n"
        'monkeypatch.setattr(snapshot_restore, "safe_tree_root", None)\n',
        _ALIAS + 'monkeypatch.setattr(snap.pinned_fs, "open_dir", None)\n',
        _ALIAS + 'mock.patch("kiro_crew.snapshot.os.open")\n',
        _ALIAS + "monkeypatch.setattr(other, name, None)\n",
        _ALIAS + 'mock.patch(f"kiro_crew.{name}")\n',
        'from unittest import mock\nmock.patch("kiro_crew.snapshot_restore.safe_tree_root")\n',
    ],
)
def test_the_patch_scan_leaves_a_patch_that_reaches_its_call_site(source: str) -> None:
    assert _wrong_target(_FacadePatches(ast.parse(source)).hits) == []
