"""The crew sidecar's backup module keeps every patch seam whole across its owners.

``container/sidecar/backup.py`` is a facade over three sibling owners -- ``backup_record``,
``backup_durable`` and ``backup_commit`` -- and keeps for itself the code that reads the names
the crew container suite patches on it: the enumeration, the data phase and ``run_cycle``.
A patch rebinds ONE namespace. ``monkeypatch.setattr(backup_mod, "time", proxy)`` changes the
facade's ``time`` and nothing else, so an owner function that read ``time`` as its own global
would keep the real clock while the test believed it had replaced it. So owner code reads
every name that suite patches on the facade only through the facade module, at call time
(``backup.<name>``), never as a bare global and never through a binding of its own, and the
facade binds it.

The checks read SOURCE, not modules. Importing ``container.sidecar.backup`` imports
``container.front``, whose dependencies (fastapi, httpx) the application's own test
environment deliberately does not carry, and the lane that does carry them runs
``container_tests/`` alone; this file sits in ``test/`` so every backend shard runs it. The
guarded names are derived from that suite's own patch calls, so a test that starts patching
another facade name is covered without editing this file, and a patch whose name this scan
cannot read fails here rather than going unguarded.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src" / "kiro_crew" / "apps" / "builtins" / "aws_control" / "crew" / "runtime"
SIDECAR = RUNTIME / "container" / "sidecar"
FACADE = SIDECAR / "backup.py"
CORPUS = RUNTIME / "container_tests"
FACADE_MODULE = "container.sidecar.backup"
#: The logger every part of the backup cycle writes to; a log filter or capture keys on it.
LOGGER_NAME = "smc.sidecar.backup"

#: Every name the facade bound at the top level before its owners moved out -- its own
#: definitions and what it imported from the rest of the container package -- plus ``os`` and
#: ``time``, the two standard-library modules the container suite reaches through it.
BASE_NAMES = (
    "BACKUP_ATTEMPT_COST_SECS",
    "BACKUP_PER_OBJECT_BUDGET_SECS",
    "BackupIncomplete",
    "BackupSet",
    "CycleResult",
    "DurableState",
    "Fingerprint",
    "MAX_OBJECT_BYTES",
    "ObjectStore",
    "PreconditionFailed",
    "RefusedEntry",
    "Settings",
    "SidecarSuperseded",
    "Snapshot",
    "StoreUnusable",
    "UploadCancelled",
    "UploadDeadlineExceeded",
    "_ARCHIVE_STATE_CAP",
    "_ArchiveWalkSink",
    "_DIGEST_CHUNK_BYTES",
    "_DIR_FLAGS",
    "_DigestDeadlineExceeded",
    "_IDENTIFIER_MAX_CHARS",
    "_OPEN_FLAGS",
    "_REFUSAL_REASON_MAX_CHARS",
    "_RESULT_SAMPLE_CAP",
    "_TRANSCRIPT_INDEX_MAX_BYTES",
    "_TRANSCRIPT_INDEX_MAX_ENTRIES",
    "_UNREACHED_REMAINDER_NAME",
    "_UNREACHED_SAMPLE_CAP",
    "__all__",
    "_already_durable",
    "_archived_segments",
    "_bounded_identifier",
    "_bounded_reason",
    "_commit_authority",
    "_commit_generation",
    "_commit_one",
    "_commit_transcript_index",
    "_descend",
    "_digest_of",
    "_evict_prior_live_blob",
    "_evict_superseded_generation",
    "_gone_stem_is_durable",
    "_is_shape_error",
    "_live_transcripts",
    "_merge_transcript_index",
    "_name_unreached_remainder",
    "_open_within",
    "_pair_unchanged",
    "_prune_departed_live_blobs",
    "_record_durable",
    "_record_is_due",
    "_referenced_by_captured_index",
    "_reserve_for_authority",
    "_slots_named_by",
    "_time_for_one_more",
    "_upload_phase",
    "generation",
    "keys",
    "log",
    "new_incarnation",
    "objects_to_back_up",
    "open_snapshot",
    "os",
    "run_cycle",
    "startup_incarnation",
    "time",
    "transcript_stem",
)

#: The facade's ``__all__`` as it stood before the split: ``import *`` still exports each of them.
BASE_ALL = (
    "Fingerprint",
    "Snapshot",
    "BackupSet",
    "CycleResult",
    "BackupIncomplete",
    "SidecarSuperseded",
    "open_snapshot",
    "objects_to_back_up",
    "run_cycle",
    "new_incarnation",
    "startup_incarnation",
)

#: Exceptions whose module a traceback prints, so they stay defined in the facade.
FACADE_EXCEPTIONS = ("BackupIncomplete", "RefusedEntry", "SidecarSuperseded")

#: The attribute names through which a test module reaches the facade on another object, such
#: as the sidecar entrypoint's ``backup_mod`` or a helper module's re-export of it.
_FACADE_ATTRIBUTES = frozenset({"backup_mod"})

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _owners() -> list[Path]:
    """Every owner beside the facade, discovered rather than listed, so a new one is covered."""
    return sorted(SIDECAR.glob("backup_*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_facade(node: ast.expr, aliases: set[str]) -> bool:
    """Whether *node* evaluates to the facade module in a test module with *aliases* bound."""
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        if node.attr in _FACADE_ATTRIBUTES or node.attr in aliases:
            return True
        return ast.unparse(node) in (FACADE_MODULE, "sidecar.backup")
    return False


def _facade_aliases(tree: ast.Module, helper_aliases: dict[str, set[str]]) -> set[str]:
    """The names a container test binds to the facade module, directly, via a helper or by copy."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if module == "container.sidecar" and alias.name == "backup":
                    aliases.add(bound)
                elif alias.name in helper_aliases.get(module.rsplit(".", 1)[-1], set()):
                    aliases.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == FACADE_MODULE and alias.asname:
                    aliases.add(alias.asname)
    copied = True
    while copied:  # ``mod = backup_mod`` makes ``mod`` the facade too
        copied = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_facade(node.value, aliases):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        copied = True
    return aliases


def _string(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _patches_in(tree: ast.Module, aliases: set[str]) -> tuple[set[str], list[str]]:
    """(facade names *tree* patches, patches on the facade whose name cannot be read)."""
    found: set[str] = set()
    unreadable: list[str] = []

    def facade(node: ast.expr | None) -> bool:
        return node is not None and _is_facade(node, aliases)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            tail = ast.unparse(node.func).rsplit(".", 1)[-1]
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            args = list(node.args)
            target = args[0] if args else keywords.get("target")
            if tail in ("setattr", "delattr", "object") and facade(target):
                name_node = (
                    args[1] if len(args) >= 2 else keywords.get("name", keywords.get("attribute"))
                )
                name = _string(name_node)
                if name is None:
                    unreadable.append(f"line {node.lineno}: {ast.unparse(node)}")
                else:
                    found.add(name)
            if tail == "multiple" and (facade(target) or _string(target) == FACADE_MODULE):
                found.update(kw.arg for kw in node.keywords if kw.arg)
            if tail in ("setattr", "delattr", "patch"):
                module, _, name = (_string(target) or "").rpartition(".")
                if module == FACADE_MODULE:
                    found.add(name)
            if tail in ("dict", "setitem") and target is not None:
                inner = target.args[0] if isinstance(target, ast.Call) and target.args else None
                if facade(inner) or (
                    isinstance(target, ast.Attribute)
                    and target.attr == "__dict__"
                    and facade(target.value)
                ):
                    unreadable.append(f"line {node.lineno}: {ast.unparse(node)}")
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        for each in targets:
            for sub in ast.walk(each):
                if isinstance(sub, ast.Attribute) and facade(sub.value):
                    found.add(sub.attr)
    return found, unreadable


@functools.lru_cache(maxsize=None)
def _corpus_patches(corpus: Path = CORPUS) -> tuple[frozenset[str], tuple[str, ...]]:
    """Every facade name the crew container suite patches, read off its source, plus any patch
    of the facade whose name the scan could not read."""
    trees = {path: _parse(path) for path in sorted(corpus.glob("*.py"))}
    helper_aliases: dict[str, set[str]] = {}
    grew = True
    while grew:  # a helper may re-export the facade from another helper
        grew = False
        for path, tree in trees.items():
            if path.name.startswith("test_"):
                continue
            aliases = _facade_aliases(tree, helper_aliases)
            if aliases != helper_aliases.get(path.stem, set()):
                helper_aliases[path.stem] = aliases
                grew = True
    patched: set[str] = set()
    unreadable: list[str] = []
    for path, tree in trees.items():
        found, lost = _patches_in(tree, _facade_aliases(tree, helper_aliases))
        patched |= found
        unreadable.extend(f"{path.name} {entry}" for entry in lost)
    return frozenset(patched), tuple(unreadable)


def _patched_names(corpus: Path = CORPUS) -> frozenset[str]:
    return _corpus_patches(corpus)[0]


def _own_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> list[ast.AST]:
    """Every node *function*'s own call evaluates.

    Its body, plus what a nested ``def``/``lambda``/``class`` evaluates when the enclosing call
    reaches it -- decorators, defaults, base classes and a nested class's body -- but not a
    nested function's body, which runs at that function's own call and is visited on its own.
    """
    pending: list[ast.AST] = (
        [function.body] if isinstance(function, ast.Lambda) else list(function.body)
    )
    seen: list[ast.AST] = []
    while pending:
        node = pending.pop()
        seen.append(node)
        if isinstance(node, _FUNCTIONS):
            arguments = node.args
            pending.extend(arguments.defaults)
            pending.extend(default for default in arguments.kw_defaults if default is not None)
            if not isinstance(node, ast.Lambda):
                pending.extend(node.decorator_list)
            continue
        if isinstance(node, ast.ClassDef):
            pending.extend([*node.decorator_list, *node.bases, *node.body])
            pending.extend(kw.value for kw in node.keywords)
            continue
        pending.extend(ast.iter_child_nodes(node))
    return seen


def _bound_in(function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    """Names *function* binds locally: parameters, assignments, imports, nested definitions."""
    arguments = function.args
    bound = {arg.arg for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
    bound.update(extra.arg for extra in (arguments.vararg, arguments.kwarg) if extra is not None)
    declared_global: set[str] = set()
    for node in _own_nodes(function):
        if isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
    return bound - declared_global


def _unguarded_reads(source: str, guarded: frozenset[str] | set[str]) -> list[str]:
    """Each place *source* reads a guarded name other than through the facade, or binds it.

    Reported shapes: a bare read anywhere -- at module level, in a function (nested functions,
    lambdas, methods, and the defaults, decorators and class bodies a call evaluates), unless
    a parameter or local of an enclosing function shadows it; an import that binds a guarded
    name or reaches into one (``import time``, ``from .backup import time``,
    ``import time as clock``, ``from time import monotonic``); a ``global`` declaration of
    one; and ``globals()["name"]``.
    """
    problems: set[str] = set()

    def report(node: ast.AST, scope: frozenset[str]) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and not node.level and node.module in guarded:
                problems.add(f"line {node.lineno}: imports from {node.module!r}")
            for alias in node.names:
                if alias.name in guarded or (alias.asname or alias.name).split(".")[0] in guarded:
                    problems.add(
                        f"line {node.lineno}: imports {alias.name!r}, a binding no facade patch reaches"
                    )
        elif isinstance(node, ast.Global):
            for name in node.names:
                if name in guarded:
                    problems.add(f"line {node.lineno}: declares {name!r} global")
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "globals()":
            if _string(node.slice) in guarded:
                problems.add(f"line {node.lineno}: reads {_string(node.slice)!r} from globals()")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in guarded and node.id not in scope:
                problems.add(f"line {node.lineno}: reads {node.id!r} instead of through the facade")

    def walk(node: ast.AST, scope: frozenset[str]) -> None:
        report(node, scope)
        if isinstance(node, _FUNCTIONS):
            arguments = node.args
            evaluated: list[ast.AST] = [*arguments.defaults]
            evaluated += [default for default in arguments.kw_defaults if default is not None]
            if not isinstance(node, ast.Lambda):
                evaluated += node.decorator_list
            for child in evaluated:
                walk(child, scope)
            inner = scope | _bound_in(node)
            for child in [node.body] if isinstance(node, ast.Lambda) else node.body:
                walk(child, inner)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    for statement in ast.parse(source).body:
        walk(statement, frozenset())
    return sorted(problems)


def _top_level_bindings(tree: ast.Module) -> set[str]:
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
    return bound


def _unexplained_reexports(source: str, allowed: set[str]) -> list[str]:
    """Names a ``# noqa: F401`` import binds that neither *allowed* lists nor the module reads.

    The stand-in for F401 on the facade: those imports switch the check off, so a dead name
    added to one would otherwise go unnoticed.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    read = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    extra: list[str] = []
    for node in tree.body:
        if (
            isinstance(node, (ast.Import, ast.ImportFrom))
            and "noqa: F401" in lines[node.lineno - 1]
        ):
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                if name not in allowed and name not in read:
                    extra.append(name)
    return sorted(extra)


def test_the_guarded_names_come_from_the_container_suite_and_are_not_empty() -> None:
    """Non-vacuity: the corpus scan finds the patches the suite is known to make."""
    patched = _patched_names()
    for known in (
        "time",
        "open_snapshot",
        "MAX_OBJECT_BYTES",
        "_descend",
        "_reserve_for_authority",
    ):
        assert known in patched, f"the patch corpus no longer sees {known!r}: {sorted(patched)}"


def test_every_patch_the_container_suite_makes_on_the_facade_is_readable() -> None:
    """A patch whose name is not a literal cannot be guarded, so it fails here instead."""
    assert _corpus_patches()[1] == (), (
        "a container test patches the sidecar backup facade with a name this guard cannot "
        "read (a variable, an f-string, or the module's __dict__); spell the name as a string "
        f"literal so the guard covers it: {_corpus_patches()[1]}"
    )


def test_the_corpus_scan_reads_every_patch_shape(tmp_path: Path) -> None:
    """Each spelling of a patch is recognised, through a helper's re-export too."""
    (tmp_path / "first_helpers.py").write_text(
        "from container.sidecar import backup as backup_mod\n", encoding="utf-8"
    )
    (tmp_path / "second_helpers.py").write_text(
        "from .first_helpers import backup_mod as facade\n", encoding="utf-8"
    )
    (tmp_path / "test_shapes.py").write_text(
        "import container.sidecar.backup as direct\n"
        "import container.sidecar.backup\n"
        "from unittest import mock\n"
        "from .first_helpers import backup_mod\n"
        "from .second_helpers import facade\n"
        "from container.sidecar import __main__ as sidecar_main\n"
        "copy = backup_mod\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(backup_mod, 'via_setattr', 1)\n"
        "    monkeypatch.delattr(direct, 'via_delattr')\n"
        "    mock.patch.object(facade, 'via_object')\n"
        "    mock.patch.multiple(direct, via_multiple=1)\n"
        "    mock.patch.multiple('container.sidecar.backup', via_multiple_string=1)\n"
        "    backup_mod.via_assign = 2\n"
        "    del direct.via_del\n"
        "    monkeypatch.setattr(container.sidecar.backup, 'via_dotted', 1)\n"
        "    monkeypatch.setattr(sidecar_main.backup_mod, 'via_entrypoint', 1)\n"
        "    monkeypatch.setattr(copy, 'via_copy', 1)\n"
        "    monkeypatch.setattr(target=backup_mod, name='via_keywords', value=1)\n"
        "    monkeypatch.setattr(backup_mod.os, 'fwalk', None)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_strings_only.py").write_text(
        "from unittest import mock\n"
        "@mock.patch('container.sidecar.backup.via_decorator')\n"
        "def test_y(monkeypatch, _p):\n"
        "    monkeypatch.setattr('container.sidecar.backup.via_string', 1)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_unreadable.py").write_text(
        "from unittest import mock\n"
        "from container.sidecar import backup as backup_mod\n"
        "NAME = 'time'\n"
        "def test_z(monkeypatch):\n"
        "    monkeypatch.setattr(backup_mod, NAME, 1)\n"
        "    mock.patch.dict(vars(backup_mod), {'time': 1})\n"
        "    monkeypatch.setitem(backup_mod.__dict__, 'time', 1)\n",
        encoding="utf-8",
    )
    patched, unreadable = _corpus_patches(tmp_path)
    assert sorted(patched) == sorted(
        {
            "via_setattr",
            "via_delattr",
            "via_object",
            "via_multiple",
            "via_multiple_string",
            "via_assign",
            "via_del",
            "via_dotted",
            "via_entrypoint",
            "via_copy",
            "via_keywords",
            "via_decorator",
            "via_string",
        }
    )
    assert len(unreadable) == 3, unreadable


def test_no_owner_reads_a_patched_name_except_through_the_facade() -> None:
    """The seam itself: an owner reaches a patched name as ``backup.<name>`` at call time."""
    guarded = _patched_names()
    owners = _owners()
    assert owners, f"no backup_*.py owners beside {FACADE}"
    problems = {
        path.name: found
        for path in owners
        if (found := _unguarded_reads(path.read_text(encoding="utf-8"), guarded))
    }
    assert not problems, (
        "an owner reads a name the crew container suite patches on the facade as its own "
        "global, so that patch no longer reaches it. Read it through the facade at call time "
        f"(``from . import backup`` inside the function, then ``backup.<name>``): {problems}"
    )


def test_the_read_detector_catches_each_shape_it_guards() -> None:
    """Non-vacuity of the detector, on planted sources and on the facade's own readers."""
    guarded = {"time", "open_snapshot"}
    flagged = {
        "bare global": "import time\ndef f():\n    return time.monotonic()\n",
        "nested function": "def f():\n    def g():\n        return open_snapshot\n    return g\n",
        "lambda body": "f = lambda: time.monotonic()\n",
        "copied in": "def f():\n    from .backup import time\n    return time.monotonic()\n",
        "local import": "def f():\n    import time\n    return time.monotonic()\n",
        "method": "class C:\n    def m(self):\n        return open_snapshot(1)\n",
        "nested default": "def f():\n    def g(clock=time):\n        return clock\n    return g\n",
        "nested decorator": "def f():\n    @open_snapshot\n    def g():\n        pass\n",
        "class in a function": "def f():\n    class C:\n        clock = time\n    return C\n",
        "global declaration": "def f():\n    global time\n    return time\n",
        "globals lookup": "def f():\n    return globals()['time']\n",
        "module alias": "clock = time\ndef f():\n    return clock.monotonic()\n",
        "import as": "import time as clock\n",
        "from import": "from time import monotonic\n",
        "an import-time read": "LIMIT = open_snapshot(1) // 2\n",
    }
    for shape, source in flagged.items():
        assert _unguarded_reads(source, guarded), f"the detector missed a {shape} read"
    clean = {
        "through the facade": "def f():\n    from . import backup\n    return backup.time.monotonic()\n",
        "a parameter": "def f(time):\n    return time.monotonic()\n",
        "a nested parameter": "def f():\n    def g(time):\n        return time\n    return g\n",
    }
    for shape, source in clean.items():
        assert not _unguarded_reads(source, guarded), f"the detector flagged {shape}"
    # The facade itself is meant to read these names bare; finding them there proves the
    # detector sees the real code, not only the planted lines above.
    facade_reads = _unguarded_reads(FACADE.read_text(encoding="utf-8"), _patched_names())
    assert any("'time'" in problem for problem in facade_reads)
    assert any("'MAX_OBJECT_BYTES'" in problem for problem in facade_reads)


def test_the_facade_binds_every_name_the_suite_patches_and_every_base_name() -> None:
    """A patch target must exist where the readers look, and no pre-split name went missing."""
    tree = _parse(FACADE)
    bound = _top_level_bindings(tree)
    assert sorted(_patched_names() - bound) == []
    assert sorted(set(BASE_NAMES) - bound) == []
    declared = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
    )
    assert sorted(set(BASE_ALL) - set(ast.literal_eval(declared))) == []


def test_the_facade_reexports_nothing_beyond_its_base_names() -> None:
    """The F401 stand-in: every name a ``noqa: F401`` import binds is a BASE name or is read."""
    assert _unexplained_reexports(FACADE.read_text(encoding="utf-8"), set(BASE_NAMES)) == []
    planted = "from .owner import (  # noqa: F401\n    used,\n    stray,\n)\nused()\n"
    assert _unexplained_reexports(planted, set()) == ["stray"]


def test_traceback_exceptions_stay_defined_in_the_facade() -> None:
    """Their ``__module__`` is what a traceback and a log line print, so it does not move."""
    classes = {node.name for node in _parse(FACADE).body if isinstance(node, ast.ClassDef)}
    assert sorted(set(FACADE_EXCEPTIONS) - classes) == []


def _imports_the_facade(node: ast.stmt) -> bool:
    if isinstance(node, ast.ImportFrom):
        names = {alias.name for alias in node.names}
        if node.level == 1:
            return node.module == "backup" or (node.module is None and "backup" in names)
        return node.module == FACADE_MODULE or (
            node.module == "container.sidecar" and "backup" in names
        )
    if isinstance(node, ast.Import):
        return any(alias.name == FACADE_MODULE for alias in node.names)
    return False


def test_owners_reach_the_facade_only_at_call_time_and_log_under_its_name() -> None:
    """No owner imports the facade at module scope, and every logger is the facade's one.

    The facade imports its owners at the top, so an owner importing the facade at module
    scope would be an import cycle that only works for one import order.
    """
    for path in _owners():
        tree = _parse(path)
        for node in tree.body:
            assert not _imports_the_facade(
                node
            ), f"{path.name}:{node.lineno} imports the facade at module scope"
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func).rsplit(".", 1)[-1] == "getLogger"
            ):
                assert [ast.literal_eval(arg) for arg in node.args] == [
                    LOGGER_NAME
                ], f"{path.name}:{node.lineno} logs under a name other than {LOGGER_NAME!r}"


def test_only_the_facade_family_imports_an_owner() -> None:
    """The facade stays the one import path: nothing outside it names an owner module."""
    owner_names = {path.stem for path in _owners()}
    offenders = []
    sources = [*(RUNTIME / "container").rglob("*.py"), *CORPUS.glob("*.py")]
    for path in sorted(sources):
        if path.parent == SIDECAR and (path.name == "backup.py" or path.stem in owner_names):
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom):
                parts = (node.module or "").split(".")
                names = {alias.name for alias in node.names}
                if owner_names & set(parts) or owner_names & names:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if owner_names & set(alias.name.split(".")):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []
