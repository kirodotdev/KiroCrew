"""The namespace launcher program is one program, spliced from its facade and its parts.

``kiro_crew.sandbox_launcher_program`` keeps the program's head, its namespace entry and
stand-in root, and its run order; the rest of the program's text lives below the marker
line of each ``kiro_crew.sandbox_launcher_parts`` module. ``kiro_crew.sandbox_launcher``
splices the parts back in when it renders a spawn's launcher, and
:func:`kiro_crew._facade_compose.compose` runs the parts' functions on the facade's module
globals when the program is imported as a module. These tests pin what that promises:

* the program the renderer holds is the facade with every part's text at its marker, as a
  splice taken here from the files on disk spells it; it imports the standard library and
  nothing else, carries exactly one plan placeholder and no marker text, and every
  function it defines is, code object for code object, the one the module binds;
* below its marker a part holds definitions only, none of which reads the facade when it
  is defined, so each part imports on its own;
* every part function runs on the facade's globals, names the facade as its module, is
  the object the facade binds, and every global it reads -- nested code included -- is
  bound there;
* every name the test trees patch on the facade reaches every part function that reads
  it, and one patch per part is exercised for real;
* a part imports standard-library modules plainly and the facade only for type checking,
  the package defines nothing, nothing but the facade imports a part, and every AUTOSDE
  rule that names the facade names the parts;
* the facade composes through the shared helper, once, and imports nothing the family does
  not account for, which stands in for the F401 check ``setup.cfg`` switches off for it.
"""

from __future__ import annotations

import ast
import builtins
import dis
import functools
import importlib
import inspect
import pkgutil
import subprocess
import sys
import types
from pathlib import Path

import facade_mirror_scan
import pytest
import source_corpus
import yaml
from test_sandbox_launcher_program import RecordingLibc, launch, payload, program_on_disk, refusal

from kiro_crew import _facade_mirror, sandbox_launcher, sandbox_launcher_parts
from kiro_crew import sandbox_launcher_program as program
from kiro_crew._facade_compose import compose as facade_compose
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_sandbox_launcher_program_parts")

#: The stages run against real files with a stand-in libc, which only Linux launches.
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)

FACADE = program.__name__
PACKAGE = sandbox_launcher_parts.__name__
_PARTS_DIR = Path(sandbox_launcher_parts.__file__).resolve().parent
_SRC = Path(program.__file__).resolve().parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"})

#: The parts, by module stem, in the order their text runs in the program.
_PART_STEMS = ("pinned_mounts", "mask_stages", "exec_lockdown")
#: The line in a part that its program text starts below.
_TEXT_STARTS = "# -- the launcher program continues below this line --\n"
#: Text that marks where the program's pieces join, none of which the program may carry.
_MARKER_TEXT = (
    "# -- the launcher program continues in ",
    _TEXT_STARTS,
    "# -- the launcher program ends above this line --\n",
)

#: Names the test trees are known to patch on the facade: part functions other part
#: functions call (``_locked_mount_flags``, read by the stages, and ``_stage_is_fresh_mount``,
#: read by the unreadable-mask stand-in beside it), a builtin the program reads as a global
#: (``open``) and kept stages the facade calls. The scan below must find every one, so a
#: scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {"_locked_mount_flags", "_stage_is_fresh_mount", "enter_namespaces", "open", "run_child"}
)

#: Every name the program's own import statements bind, at its top: the standard library,
#: imported before the isolation the program builds.
_BASE_IMPORTS = frozenset({"ctypes", "os", "_plat", "stat", "_struct", "sys", "tempfile"})
#: The one name the facade's composition glue imports that no part defines.
_GLUE_IMPORTS = frozenset({"_facade_compose"})


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _part_paths() -> dict[str, Path]:
    return {path.stem: path for path in sorted(_PARTS_DIR.glob("*.py")) if path.stem != "__init__"}


def _parts() -> list[types.ModuleType]:
    """Every part module, imported through the package the facade composes."""
    return [
        importlib.import_module(f"{PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules([str(_PARTS_DIR)])
    ]


def _part_functions() -> list[tuple[str, types.FunctionType]]:
    """``(part.name, function)`` for every function a part's file defines."""
    found = []
    for part in _parts():
        short = part.__name__.rpartition(".")[2]
        for name, value in vars(part).items():
            if (
                isinstance(value, types.FunctionType)
                and value.__code__.co_filename == part.__file__
            ):
                found.append((f"{short}.{name}", value))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and the code objects nested in it read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _unbound_globals(fn: types.FunctionType) -> list[str]:
    namespace = fn.__globals__
    return sorted(
        {n for n in _global_names(fn.__code__) if n not in namespace and not hasattr(builtins, n)}
    )


def _part_text(path: Path) -> str:
    """The program text a part carries: everything below its marker line."""
    _header, marker, body = path.read_text(encoding="utf-8").partition(_TEXT_STARTS)
    assert marker, f"{path.name} marks where its program text starts"
    return body


# ── 1. the rendered program ──────────────────────────────────────────────────


def test_the_program_is_the_facade_with_every_part_spliced_in() -> None:
    """The renderer's own splice and an independent one over the files on disk agree."""
    assert sandbox_launcher.launcher_program_source() == program_on_disk()


def test_the_splice_here_sees_a_changed_part(tmp_path: Path) -> None:
    """Non-vacuity: the splice above reads the parts, so a changed part changes it."""
    package = tmp_path / "kiro_crew"
    (package / "sandbox_launcher_parts").mkdir(parents=True)
    (package / "sandbox_launcher_program.py").write_bytes(Path(program.__file__).read_bytes())
    for path in _part_paths().values():
        (package / "sandbox_launcher_parts" / path.name).write_bytes(path.read_bytes())
    part = package / "sandbox_launcher_parts" / "exec_lockdown.py"
    part.write_text(part.read_text(encoding="utf-8") + "PLANTED = 1\n", encoding="utf-8")
    spliced = program_on_disk(package)
    assert spliced != program_on_disk() and "PLANTED = 1\n" in spliced


def test_the_program_carries_no_marker_text_and_one_placeholder() -> None:
    source = sandbox_launcher.launcher_program_source()
    assert [marker for marker in _MARKER_TEXT if marker in source] == []
    assert source.count(sandbox_launcher.PLAN_PLACEHOLDER) == 1


def _foreign_imports(source: str) -> list[str]:
    """Every module *source* imports, at any depth, that is not the standard library."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = ["." * node.level + (node.module or "")]
        else:
            continue
        found += [n for n in names if n.split(".")[0] not in sys.stdlib_module_names]
    return found


def test_the_import_check_flags_a_project_or_relative_import() -> None:
    planted = "import os\nfrom kiro_crew import sandbox\nfrom . import x\nimport yaml\n"
    assert _foreign_imports(planted) == ["kiro_crew", ".", "yaml"]


def test_the_program_imports_the_standard_library_and_nothing_else() -> None:
    """The child runs the text as ``python -I -S`` with no package to import."""
    assert _foreign_imports(sandbox_launcher.launcher_program_source()) == []


def _code_shape(code: types.CodeType) -> tuple:
    """Everything about *code* that decides what it does: its bytecode, names, argument
    layout, flags and constants, nested code compared the same way. Not its file or line
    numbers, which differ between the one program text and the file a part lives in."""
    return (
        code.co_name,
        code.co_qualname,
        code.co_code,
        code.co_exceptiontable,
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_flags,
        tuple(
            _code_shape(c) if isinstance(c, types.CodeType) else (type(c).__name__, c)
            for c in code.co_consts
        ),
    )


def _program_functions(source: str) -> dict[str, types.CodeType]:
    """The code of every function *source* defines at module level or in a top-level
    class, keyed ``name`` or ``Class.name``, compiled from the one program text."""
    module = compile(source, "<launcher program>", "exec", dont_inherit=True)
    found: dict[str, types.CodeType] = {}
    for const in module.co_consts:
        if not isinstance(const, types.CodeType):
            continue
        if const.co_flags & inspect.CO_NEWLOCALS:
            found[const.co_name] = const
            continue
        # A class body: its methods are the functions it defines.
        for inner in const.co_consts:
            if isinstance(inner, types.CodeType) and inner.co_flags & inspect.CO_NEWLOCALS:
                found[f"{const.co_name}.{inner.co_name}"] = inner
    return found


def _bound_function(qualname: str) -> types.FunctionType:
    owner, _, name = qualname.rpartition(".")
    value = vars(getattr(program, owner))[name] if owner else vars(program)[qualname]
    assert isinstance(value, types.FunctionType), qualname
    return value


def test_every_function_of_the_program_is_the_one_the_module_binds() -> None:
    """The module the tests drive IS the program a child runs, kept code and parts alike."""
    expected = _program_functions(sandbox_launcher.launcher_program_source())
    defs = {
        node.name for node in ast.parse(program_on_disk()).body if isinstance(node, ast.FunctionDef)
    }
    assert defs <= set(expected) and "Launch.__init__" in expected
    differ = sorted(
        name
        for name, code in expected.items()
        if _code_shape(_bound_function(name).__code__) != _code_shape(code)
    )
    assert differ == []


def test_the_code_comparison_sees_a_changed_body() -> None:
    planted = "def f(x):\n    return x + 1\n"
    assert _code_shape(_program_functions(planted)["f"]) != _code_shape(
        _program_functions(planted.replace("+ 1", "+ 2"))["f"]
    )


# ── 2. what a part holds below its marker, and its own import ────────────────


def _not_definitions(body: str) -> list[str]:
    """Every top-level statement of *body* that is not a plain function definition, and
    every part of a ``def`` that would read a name when it is defined."""
    found = []
    for node in ast.parse(body).body:
        if not isinstance(node, ast.FunctionDef):
            found.append(f"{node.lineno}: {type(node).__name__}")
            continue
        args = node.args
        evaluated = [*node.decorator_list, *args.defaults, *args.kw_defaults, node.returns]
        evaluated += [a.annotation for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
        evaluated += [a.annotation for a in (args.vararg, args.kwarg) if a is not None]
        if any(e is not None and not isinstance(e, ast.Constant) for e in evaluated):
            found.append(f"{node.lineno}: {node.name} reads a name when defined")
    return found


@pytest.mark.parametrize(
    ("body", "flagged"),
    [
        ("def f(a, b=1, *, c=None):\n    return a\n", []),
        ("LIMIT = 3\n", ["1: Assign"]),
        ("class K:\n    pass\n", ["1: ClassDef"]),
        ("def f(a=_DEFAULT):\n    return a\n", ["1: f reads a name when defined"]),
        ("@wrap\ndef f():\n    return 1\n", ["2: f reads a name when defined"]),
        ("def f(a: Launch):\n    return a\n", ["1: f reads a name when defined"]),
    ],
    ids=["plain", "value", "class", "default", "decorator", "annotation"],
)
def test_the_definitions_check_can_fail(body: str, flagged: list[str]) -> None:
    assert _not_definitions(body) == flagged


def test_every_part_holds_only_definitions_below_its_marker() -> None:
    found = {
        stem: bad
        for stem, path in _part_paths().items()
        if (bad := _not_definitions(_part_text(path)))
    }
    assert found == {}


@pytest.mark.parametrize("stem", _PART_STEMS)
def test_each_part_imports_without_the_facade(stem: str) -> None:
    """A fresh interpreter imports the part alone: nothing in it needs the facade loaded."""
    probe = (
        "import sys, types\n"
        f"import {PACKAGE}.{stem} as part\n"
        "functions = sorted(n for n, v in vars(part).items()\n"
        "    if isinstance(v, types.FunctionType) and v.__code__.co_filename == part.__file__)\n"
        f"print(repr(({FACADE!r} in sys.modules, functions)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=source_corpus.repo_root(),
        capture_output=True,
        check=False,
        timeout=120,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    facade_loaded, functions = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert facade_loaded is False
    expected = sorted(
        node.name
        for node in ast.parse(_part_text(_part_paths()[stem])).body
        if isinstance(node, ast.FunctionDef)
    )
    assert functions == expected


# ── 3. every part function runs on the facade's namespace ────────────────────


def test_the_facade_composes_every_module_of_the_package_in_program_order() -> None:
    assert tuple(_part_paths()) == tuple(sorted(_PART_STEMS))
    assert program._PARTS == tuple(f"{PACKAGE}.{stem}" for stem in _PART_STEMS)
    assert sandbox_launcher._PARTS == tuple(f"{stem}.py" for stem in _PART_STEMS)
    facade = Path(program.__file__).read_text(encoding="utf-8")
    markers = [
        line.removeprefix(_MARKER_TEXT[0]).removesuffix(" --")
        for line in facade.splitlines()
        if line.startswith(_MARKER_TEXT[0])
    ]
    assert markers == [f"sandbox_launcher_parts/{stem}.py" for stem in _PART_STEMS]
    assert sorted(o.__name__ for o in _parts()) == sorted(program._PARTS)


def test_every_part_function_runs_on_the_facade_globals_as_the_facade_binds_it() -> None:
    functions = _part_functions()
    assert len(functions) >= len(_PART_STEMS)
    wrong = [
        label
        for label, fn in functions
        if fn.__globals__ is not vars(program)
        or fn.__module__ != FACADE
        or vars(program).get(fn.__name__) is not fn
    ]
    assert wrong == []


def test_the_sweep_reports_a_global_the_namespace_does_not_bind() -> None:
    module_code = compile(
        "def f():\n    def g():\n        return absent\n    return present, g\n", "probe", "exec"
    )
    code = next(c for c in module_code.co_consts if isinstance(c, types.CodeType))
    probe = types.FunctionType(code, {"__name__": "probe", "present": 1})
    assert _unbound_globals(probe) == ["absent"]


def test_every_global_a_part_function_reads_is_bound_on_the_facade() -> None:
    """Nested code included: ``_pin_mount_path`` defines its refusals inline."""
    missing = {label: names for label, fn in _part_functions() if (names := _unbound_globals(fn))}
    assert missing == {}


# ── 4. a patch of the facade reaches every part reader ───────────────────────


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


def _spellings() -> dict[str, str]:
    """The facade's dotted name, plus ``<test module>.<name>`` for each test module that
    binds the facade at module level -- another spelling a test can import and patch
    through (``sandbox_absent_ceiling_seal_helpers.program``)."""
    spellings = {FACADE: FACADE}
    test_root = source_corpus.repo_root() / "test"
    for path in sorted(test_root.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "sandbox_launcher_program" not in text:
            continue
        for node in ast.parse(text).body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew" and not node.level:
                for alias in node.names:
                    if alias.name == "sandbox_launcher_program":
                        spellings[f"{path.stem}.{alias.asname or alias.name}"] = FACADE
            elif (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Name)
                and node.value.id == "sandbox_launcher_program"
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        spellings[f"{path.stem}.{target.id}"] = FACADE
    return spellings


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch on the facade, scanned once per worker."""
    spellings = _spellings()
    # A file patches through a spelling only if it names the spelling's module.
    needles = {"sandbox_launcher_program"} | {
        spelling.split(".")[0] for spelling in spellings if spelling != FACADE
    }
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(needle in text for needle in needles):
            continue
        try:
            found |= facade_mirror_scan.patched_names(text, spellings).get(FACADE, set())
        except SyntaxError:
            continue
    return frozenset(found)


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


def test_the_spellings_include_a_helper_binding_of_the_facade() -> None:
    assert _spellings()["sandbox_absent_ceiling_seal_helpers.program"] == FACADE


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from kiro_crew import sandbox_launcher_program as program\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(program, 'A', 1)\n",
            {"A"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.sandbox_launcher_program.B'):\n        pass\n",
            {"B"},
        ),
        (
            "from sandbox_absent_ceiling_seal_helpers import program\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(program, 'C', 1)\n",
            {"C"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n"
            "    with patch('kiro_crew.sandbox_launcher_parts.mask_stages.D'):\n        pass\n",
            set(),
        ),
    ],
    ids=["monkeypatch", "mock-patch-string", "helper-binding", "part-string"],
)
def test_the_patch_scan_reads_every_spelling_and_nothing_else(
    source: str, expected: set[str]
) -> None:
    assert facade_mirror_scan.patched_names(source, _spellings()).get(FACADE, set()) == expected


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that found nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_every_name_the_tests_patch_on_the_facade_reaches_every_part_reader() -> None:
    """A patch of the facade reaches part code only where that code runs on its globals."""
    patched = _corpus_patched_names()
    readers = 0
    missed = []
    for label, fn in _part_functions():
        loaded = set(_global_names(fn.__code__)) & patched
        readers += bool(loaded)
        if loaded and fn.__globals__ is not vars(program):
            missed.append(f"{label}: {sorted(loaded)}")
    assert readers, "no part function reads a patched name -- the sweep went blind"
    assert missed == []


@_LINUX_ONLY
def test_a_facade_patch_reaches_the_pinned_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_verify_masked_name`` asks ``_mode_is_link`` through the facade's globals."""
    name = tmp_path / "masked"
    name.mkdir()
    run = launch(tmp_path)
    stand_in = (name.stat().st_dev, name.stat().st_ino)
    assert refusal(program._verify_masked_name, run, str(name).encode(), stand_in, "x") is None
    monkeypatch.setattr(program, "_mode_is_link", lambda _mode: True)
    assert "is a link now" in (
        refusal(program._verify_masked_name, run, str(name).encode(), stand_in, "x") or ""
    )


@_LINUX_ONLY
def test_a_facade_patch_reaches_the_mask_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``apply_carveouts`` mounts through the facade's ``_mount_or_warn``."""
    carve = tmp_path / "carve"
    carve.mkdir()
    seen: list[str] = []
    monkeypatch.setattr(program, "_mount_or_warn", lambda _launch, *a: seen.append(a[-1]))
    program.apply_carveouts(launch(tmp_path, payload(writable_dirs=[str(carve)])))
    assert seen == [f"writable carve-out bind for {carve}"]


@_LINUX_ONLY
def test_a_facade_patch_reaches_the_exec_lockdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``install_seccomp`` looks its syscall table up through the facade's globals."""
    monkeypatch.setattr(program._plat, "machine", lambda: "x86_64")
    monkeypatch.setattr(program, "seccomp_deny_table", lambda _machine: None)
    message = refusal(program.install_seccomp, launch(tmp_path, libc=RecordingLibc()))
    assert message is not None and "no seccomp syscall table" in message


# ── 5. what a part may import, and who may import it ─────────────────────────


def _runtime_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, module)`` of each import a part executes when it is imported."""
    found = []
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            pending += node.orelse
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            pending += [n for f in ("body", "orelse", "finalbody") for n in getattr(node, f, [])]
            pending += [n for h in getattr(node, "handlers", []) for n in h.body]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
    return sorted(found)


def _bad_part_imports(tree: ast.Module) -> list[str]:
    """Runtime imports of anything but the standard library, type-only imports from
    anywhere but the facade, and an ``import x.y`` or ``from x import y`` spelling of a
    standard-library module, which a patch of ``program.os.<name>`` still reaches but a
    patch of ``program.<module>`` would not."""
    bad = [
        f"{line}: {mod}"
        for line, mod in _runtime_imports(tree)
        if mod.split(".")[0] not in sys.stdlib_module_names
    ]
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module != "typing":
            bad.append(f"{node.lineno}: from {node.module}")
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.ImportFrom) and stmt.module != FACADE:
                    bad.append(f"{stmt.lineno}: type-only {stmt.module}")
                elif isinstance(stmt, ast.Import):
                    bad.append(f"{stmt.lineno}: type-only import")
    return bad


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("import os\nimport platform as _plat\nfrom typing import TYPE_CHECKING\n", False),
        ("if TYPE_CHECKING:\n    from kiro_crew.sandbox_launcher_program import _O_PATH\n", False),
        ("from kiro_crew.sandbox_launcher_program import _O_PATH\n", True),
        ("import kiro_crew.sandbox_launcher_program\n", True),
        ("from os import path\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.sandbox_plan import ConfinementPlan\n", True),
    ],
)
def test_the_part_import_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_bad_part_imports(ast.parse(source))) is flagged


def test_a_part_imports_the_facade_only_for_type_checking_and_no_project_module() -> None:
    bad = {
        stem: found
        for stem, path in _part_paths().items()
        if (found := _bad_part_imports(_tree(path)))
    }
    assert bad == {}


def _module_values(tree: ast.Module) -> list[str]:
    """Top-level statements that are neither the docstring, an import, the
    ``TYPE_CHECKING`` block nor a function definition."""
    found = []
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        found.append(f"{node.lineno}: {type(node).__name__}")
    return found


def test_the_value_check_can_fail() -> None:
    assert _module_values(ast.parse('"""doc"""\nimport os\nLIMIT = 3\nclass K: ...\n')) == [
        "3: Assign",
        "4: ClassDef",
    ]


def test_no_part_defines_a_module_level_value() -> None:
    """The program's constants stay on the facade: a test that rebinds one there rebinds
    what every composed stage reads, and a part copy would read stale."""
    found = {
        stem: values
        for stem, path in _part_paths().items()
        if (values := _module_values(_tree(path)))
    }
    assert found == {}


def test_the_package_init_defines_nothing() -> None:
    init = _PARTS_DIR / "__init__.py"
    assert _facade_mirror._defined_names(_tree(init)) == set()
    assert [n for n in _tree(init).body if not isinstance(n, ast.Expr)] == []


def _imports_a_part(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            if "sandbox_launcher_parts" in mod or any(
                a.name == "sandbox_launcher_parts" for a in node.names
            ):
                return True
        elif isinstance(node, ast.Import) and any(
            "sandbox_launcher_parts" in a.name for a in node.names
        ):
            return True
    return False


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from kiro_crew.sandbox_launcher_parts import mask_stages\n", True),
        ("from kiro_crew.sandbox_launcher_parts.pinned_mounts import _pin_mount_path\n", True),
        ("import kiro_crew.sandbox_launcher_parts.exec_lockdown\n", True),
        ("from kiro_crew import sandbox_launcher_parts\n", True),
        ("from .sandbox_launcher_parts import mask_stages\n", True),
        ("from kiro_crew.sandbox_launcher_program import _pin_mount_path\n", False),
    ],
)
def test_the_importer_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert _imports_a_part(source) is flagged


def test_nothing_but_the_facade_imports_a_part() -> None:
    """The renderer reads the parts as text and never imports one."""
    importers = sorted(
        str(path.relative_to(_SRC.parent))
        for path in sorted(_SRC.rglob("*.py"))
        if path.parent != _PARTS_DIR
        and path.name != "sandbox_launcher_program.py"
        and "sandbox_launcher_parts" in (text := path.read_text(encoding="utf-8"))
        and _imports_a_part(text)
    )
    assert importers == []


def test_every_autosde_rule_that_names_the_facade_names_the_parts() -> None:
    """The review rules that guard the launcher apply to the program's text in its parts."""
    autosde = yaml.safe_load(
        (source_corpus.repo_root() / "AUTOSDE.yaml").read_text(encoding="utf-8")
    )
    facade_rel = "src/kiro_crew/sandbox_launcher_program.py"
    rules = [
        rule for rule in autosde["custom-rules"] if facade_rel in (rule.get("file-patterns") or [])
    ]
    assert rules, "no AUTOSDE rule names the launcher program any more"
    missing = [
        rule["id"]
        for rule in rules
        if "src/kiro_crew/sandbox_launcher_parts/*.py" not in rule["file-patterns"]
    ]
    assert missing == []


# ── 6. the composition and the facade's imports ──────────────────────────────


def test_the_facade_composes_through_the_shared_helper_once() -> None:
    assert program._facade_compose is facade_compose
    source = Path(program.__file__).read_text(encoding="utf-8")
    assert source.count("\n_facade_compose(") == 1


def unexpected_imports(source: str, part_names: dict[str, set[str]], read: set[str]) -> list[str]:
    """Names *source* binds by a runtime import that nothing accounts for: the F401 check
    ``setup.cfg`` switches off for this module, kept as a test.

    A name is accounted for when the program imports it at its top (BASE), the composition
    glue imports it, a part defines it (for an import from that part), or the family reads
    it: the facade's own code or a composed part function. A new import any of them reads
    is accounted for without an edit here.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in part_names:
                if name not in part_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | read:
                found.append(name)
    return found


def _part_names() -> dict[str, set[str]]:
    return {
        f"{PACKAGE}.{stem}": _facade_mirror._defined_names(_tree(path))
        for stem, path in _part_paths().items()
    }


def _family_reads() -> set[str]:
    """Every global the facade's code and the composed part functions read."""
    facade = Path(program.__file__)
    read = set(_global_names(compile(facade.read_text(encoding="utf-8"), str(facade), "exec")))
    read |= {n for _label, fn in _part_functions() for n in _global_names(fn.__code__)}
    return read


def test_every_facade_import_is_accounted_for() -> None:
    source = Path(program.__file__).read_text(encoding="utf-8")
    assert unexpected_imports(source, _part_names(), _family_reads()) == []


def test_the_import_account_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: an import nothing reads, and a part import
    of a name that part does not define, are both reported."""
    planted = (
        Path(program.__file__).read_text(encoding="utf-8")
        + "\nimport pickle\nfrom kiro_crew.sandbox_launcher_parts.exec_lockdown import _pin_mount_path\n"
    )
    assert unexpected_imports(planted, _part_names(), _family_reads()) == [
        "pickle",
        "kiro_crew.sandbox_launcher_parts.exec_lockdown._pin_mount_path",
    ]


def test_a_fresh_import_composes_every_part() -> None:
    probe = (
        "import sys\n"
        f"import {FACADE} as program\n"
        f"loaded = sorted(m for m in sys.modules if m.startswith('{PACKAGE}.'))\n"
        "composed = all(getattr(program, n).__globals__ is vars(program)\n"
        "    for m in loaded for n, v in vars(sys.modules[m]).items()\n"
        "    if type(v).__name__ == 'function'\n"
        "    and v.__code__.co_filename == sys.modules[m].__file__)\n"
        "print(repr((loaded, composed)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=source_corpus.repo_root(),
        capture_output=True,
        check=False,
        timeout=120,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    loaded, composed = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert loaded == sorted(f"{PACKAGE}.{stem}" for stem in _PART_STEMS)
    assert composed is True
