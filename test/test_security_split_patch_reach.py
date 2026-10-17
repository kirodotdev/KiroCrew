"""A name a test patches on ``kiro_crew.security`` or its shell reader reaches every reader.

Two splits shaped this family. ``kiro_crew.security`` is a forwarding facade: ``_EXPORTS``
names the module that owns each re-exported name, and a write on the package lands in that
owner. The controls the package defined itself live in the owners it imports at its foot
(``deny_gate``, ``display_safety``, ``memory_audit``, ``text_redaction``).
``kiro_crew.security.shell_normalizer`` keeps argv attribution and re-imports what its
callers name from the lexical owners below it (``shell_normalizer_*.py``).

A patch reaches a reader only when the reader looks the name up where the patch writes it.
A function reads a module global from its own module, and ``alias.<name>`` or
``_submodule("<leaf>").<name>`` -- the alias bound to ``_submodule("<leaf>")`` or to an
imported submodule -- reads it from the module named. So the rule held here is the one these
splits had to keep, for every name the test trees patch through either module: every reader of
it in the family that reads it as a module global sits in the module each such write lands in,
and every attribute read of it anywhere in the package goes to that module. A module outside
the family that imported its own copy of a name (``argv_floor``, ``inline_payload``,
``perm_verb_mention``, ``exfil``, ``credential_sources``) reads that copy, as it did before
these splits; a test reaching it patches that module. The patched names are scanned from the
test trees rather than listed, so a new patch site is covered on the commit that adds it;
``_KNOWN_SEAMS`` keeps the scan from passing over nothing, and each detector is pinned on a
synthetic source it must flag.
"""

from __future__ import annotations

import ast
import functools
import importlib
import re
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew.security as security

pytestmark = pytest.mark.xdist_group(name="tree_scan_security_split_patch_reach")

PACKAGE = "kiro_crew.security"
READER = "kiro_crew.security.shell_normalizer"
SECURITY_DIR = Path(security.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()
#: Text every test file that binds either module must hold: a dotted spelling (an import,
#: a string target, an ``import_module`` or ``sys.modules`` key) or an import from the
#: ``kiro_crew`` package, one line or parenthesized. The scan parses only files holding it.
_BINDS_THE_PACKAGE = re.compile(
    r"kiro_crew\.security\b|from kiro_crew import[^\n]*\bsecurity\b|from kiro_crew import \("
)

#: One of each seam the two splits had to keep, which the scan must find: the audit log
#: and executor the deny gate's emitters read, the emitters and view builder the gate calls,
#: the stream holdback bounds, ``redact`` beside the stream redactor that defaults to it,
#: the shell-reader names the argv floor reads late through ``_shell_normalizer``, the
#: payload-walk helpers the nested-payload extractor calls, a name only a loop over string
#: constants patches, and the glob pair patched on the reader directly.
_KNOWN_SEAMS: dict[str, frozenset[str]] = {
    PACKAGE: frozenset(
        {
            "SecurityEventLog",
            "maintenance_executor",
            "_emit_deny_event",
            "_emit_deny_exception_event",
            "_deny_segment_views",
            "_STREAM_HOLDBACK_MAX",
            "_STREAM_DISCARD_MAX",
            "redact",
            "_program_basename",
            "_normalize_operand",
            "normalize_shell_command",
            "_shell_tokens",
            "_data_consumer_exempt",
            "_next_stop_indexes",
            "_output_redirect_scan",
            "_decode_shell_quoted_literals",
        }
    ),
    READER: frozenset({"_brace_pairs", "_glob_to_regex"}),
}


def _facade_owners() -> tuple[str, ...]:
    """The modules the facade imports at its foot, after it installs its module class."""
    tree = ast.parse((SECURITY_DIR / "__init__.py").read_text(encoding="utf-8"))
    installed = False
    owners: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Attribute) and target.attr == "__class__"
            for target in node.targets
        ):
            installed = True
        elif installed and isinstance(node, ast.ImportFrom) and node.level == 1:
            assert node.module is None, ast.unparse(node)
            owners += [alias.name for alias in node.names]
    return tuple(owners)


def _reader_owners() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in SECURITY_DIR.glob("shell_normalizer_*.py")))


def _family() -> dict[str, Path]:
    """Dotted name -> source of the facade, the shell reader and every owner of either."""
    family = {PACKAGE: SECURITY_DIR / "__init__.py", READER: SECURITY_DIR / "shell_normalizer.py"}
    for stem in _facade_owners() + _reader_owners():
        family[f"{PACKAGE}.{stem}"] = SECURITY_DIR / f"{stem}.py"
    return family


def _package_modules() -> dict[str, Path]:
    """Every module of the security package, for the late-read rule, which is universal."""
    return {
        (PACKAGE if path.stem == "__init__" else f"{PACKAGE}.{path.stem}"): path
        for path in sorted(SECURITY_DIR.glob("*.py"))
    }


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Spelling -> the target module it names in a test file's syntax tree."""
    aliases = {PACKAGE: PACKAGE, READER: READER}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in (PACKAGE, READER) and alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in ("kiro_crew", PACKAGE):
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if dotted in (PACKAGE, READER):
                    aliases[alias.asname or alias.name] = dotted
    return aliases


def loop_patched_names(source: str | ast.Module) -> dict[str, set[str]]:
    """Target -> names a loop over string constants patches through it.

    ``for name in ("a", "b"): monkeypatch.setattr(security, name, ...)`` binds the name
    through a loop, which the corpus scan reads as dynamic, so the constants are read here.
    """
    tree = ast.parse(source) if isinstance(source, str) else source
    aliases = _module_aliases(tree)
    found: dict[str, set[str]] = {}
    for loop in ast.walk(tree):
        if not (
            isinstance(loop, ast.For)
            and isinstance(loop.target, ast.Name)
            and isinstance(loop.iter, (ast.Tuple, ast.List))
            and loop.iter.elts
            and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in loop.iter.elts
            )
        ):
            continue
        constants = {e.value for e in loop.iter.elts if isinstance(e, ast.Constant)}
        for node in ast.walk(loop):
            if not (
                isinstance(node, ast.Call)
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id == loop.target.id
            ):
                continue
            func = node.func
            verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            target = aliases.get(ast.unparse(node.args[0]))
            if verb in ("setattr", "delattr", "object") and target is not None:
                found.setdefault(target, set()).update(constants)
    return found


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> dict[str, frozenset[str]]:
    """Target -> every name the test trees patch through it, scanned once per worker."""
    found: dict[str, set[str]] = {PACKAGE: set(), READER: set()}
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _BINDS_THE_PACKAGE.search(text):
            continue
        tree = ast.parse(text)
        names = facade_mirror_scan.patched_names(tree, {PACKAGE: PACKAGE, READER: READER})
        for target, patched in names.items():
            found[target] |= patched
        for target, patched in loop_patched_names(tree).items():
            found[target] |= patched
    return {target: frozenset(names) for target, names in found.items()}


def _write_target(target: str, name: str) -> str | None:
    """The module a patch of ``<target>.<name>`` writes, or ``None`` for no module global.

    A write on the package lands in the name's ``_EXPORTS`` owner, or on the package for a
    name it binds itself; a write on the reader lands on the reader. A name that holds a
    module is the head of a dotted patch of that module's own attribute (``security.os.open``),
    and a name nothing binds is a probe a test sets and deletes, so neither is a global any
    reader resolves.
    """
    module = importlib.import_module(target)
    if target == PACKAGE and name in security._EXPORTS:
        return f"{PACKAGE}.{security._EXPORTS[name]}"
    value = getattr(module, name, None)
    if value is None or isinstance(value, ModuleType):
        return None
    return target


def _patched_homes() -> dict[str, set[str]]:
    """Patched name -> every module a test's write of it lands in."""
    homes: dict[str, set[str]] = {}
    for target, names in _corpus_patched_names().items():
        for name in names:
            home = _write_target(target, name)
            if home is not None:
                homes.setdefault(name, set()).add(home)
    return homes


def bare_read_violations(sources: dict[str, str], homes: dict[str, set[str]]) -> list[str]:
    """``module reads name`` for each module that reads a patched name from its own globals
    although a write of it lands somewhere else."""
    out = []
    for module, source in sorted(sources.items()):
        reads = facade_mirror_scan.global_reads(source, module)
        for name in sorted(reads & set(homes)):
            if homes[name] != {module}:
                out.append(f"{module} reads {name}, written in {sorted(homes[name])}")
    return out


def _resolver_leaf(node: ast.expr) -> str | None:
    """The leaf a ``_submodule("<leaf>")`` call names, for a read chained straight off it."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_submodule"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _alias_leaves(tree: ast.Module) -> dict[str, set[str]]:
    """Name -> the package submodules it is bound to in *tree*: an import of the
    submodule, or an assignment from ``_submodule("<leaf>")``."""
    leaves: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            for alias in node.names:
                leaves.setdefault(alias.asname or alias.name, set()).add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == PACKAGE:
            for alias in node.names:
                if (SECURITY_DIR / f"{alias.name}.py").is_file():
                    leaves.setdefault(alias.asname or alias.name, set()).add(alias.name)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                isinstance(call.func, ast.Name)
                and call.func.id == "_submodule"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        leaves.setdefault(target.id, set()).add(call.args[0].value)
    return leaves


def late_read_violations(sources: dict[str, str], homes: dict[str, set[str]]) -> list[str]:
    """``module reads leaf.name`` for each attribute read of a patched name through a
    submodule alias whose module no write of that name lands in."""
    out = []
    for module, source in sorted(sources.items()):
        tree = ast.parse(source)
        leaves = _alias_leaves(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr in homes
            ):
                continue
            read_from = _resolver_leaf(node.value)
            if read_from is not None:
                targets = {read_from}
            elif isinstance(node.value, ast.Name) and node.value.id in leaves:
                targets = leaves[node.value.id]
            else:
                continue
            for leaf in sorted(targets):
                if homes[node.attr] != {f"{PACKAGE}.{leaf}"}:
                    out.append(
                        f"{module}:{node.lineno} reads {leaf}.{node.attr}, "
                        f"written in {sorted(homes[node.attr])}"
                    )
    return out


def _texts(modules: dict[str, Path]) -> dict[str, str]:
    return {name: path.read_text(encoding="utf-8") for name, path in modules.items()}


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass both reach rules over nothing."""
    found = _corpus_patched_names()
    missing = {target: sorted(seams - found[target]) for target, seams in _KNOWN_SEAMS.items()}
    assert missing == {PACKAGE: [], READER: []}


def test_the_family_is_the_facade_the_reader_and_every_owner() -> None:
    """The family is derived, so an owner added to either split is covered with no edit
    here; every module that reads the package's resolver is one the facade imports."""
    owners = _facade_owners()
    assert owners, "the facade imports no owner at its foot -- the derivation went blind"
    assert _reader_owners(), "no shell_normalizer_*.py owner -- the derivation went blind"
    resolver_readers = sorted(
        path.stem
        for path in SECURITY_DIR.glob("*.py")
        if path.stem != "__init__"
        and any(
            isinstance(node, ast.ImportFrom)
            and node.module == PACKAGE
            and any(alias.name == "_submodule" for alias in node.names)
            for node in ast.parse(path.read_text(encoding="utf-8")).body
        )
    )
    assert resolver_readers == sorted(owners)


def test_every_reader_of_a_patched_name_reads_it_where_the_patch_writes() -> None:
    """The reach the unsplit modules had, for a module-global read: in the facade, the
    reader and every owner, a function that reads a patched name from its own globals sits
    in the module the patch writes -- the emitters with ``SecurityEventLog``, the stream
    redactor with its bounds and ``redact``, the payload walk with its helpers."""
    homes = _patched_homes()
    assert bare_read_violations(_texts(_family()), homes) == []


def test_every_late_read_of_a_patched_name_goes_to_the_module_the_patch_writes() -> None:
    """The reach for a read through a submodule alias, held across the whole package:
    ``_shell_normalizer._program_basename`` in the argv floor and ``_shell._shell_tokens``
    in the deny gate read the module a patch of that name writes."""
    homes = _patched_homes()
    assert late_read_violations(_texts(_package_modules()), homes) == []


def test_the_bare_read_rule_flags_a_reader_away_from_the_write() -> None:
    homes = {"_emit_deny_event": {f"{PACKAGE}.deny_gate"}}
    sources = {
        f"{PACKAGE}.deny_gate": "def gate():\n    return _emit_deny_event()\n",
        f"{PACKAGE}.text_redaction": "def other():\n    return _emit_deny_event()\n",
    }
    assert bare_read_violations(sources, homes) == [
        f"{PACKAGE}.text_redaction reads _emit_deny_event, written in ['{PACKAGE}.deny_gate']"
    ]


def test_the_late_read_rule_flags_an_alias_of_another_module() -> None:
    homes = {"_program_basename": {READER}}
    source = (
        "from . import shell_normalizer as _shell_normalizer\n"
        "def floor(token):\n"
        "    _walk = _submodule('shell_normalizer_walk')\n"
        "    return _shell_normalizer._program_basename(token), _walk._program_basename(token)\n"
    )
    assert late_read_violations({f"{PACKAGE}.argv_floor": source}, homes) == [
        f"{PACKAGE}.argv_floor:4 reads shell_normalizer_walk._program_basename, "
        f"written in ['{READER}']"
    ]


def test_the_late_read_rule_reads_a_call_chained_off_the_resolver() -> None:
    homes = {"_shell_tokens": {READER}}
    source = (
        "def gate(cmd):\n"
        "    return _submodule('shell_normalizer')._shell_tokens(cmd), "
        "_submodule('shell_normalizer_walk')._shell_tokens(cmd)\n"
    )
    assert late_read_violations({f"{PACKAGE}.deny_gate": source}, homes) == [
        f"{PACKAGE}.deny_gate:2 reads shell_normalizer_walk._shell_tokens, "
        f"written in ['{READER}']"
    ]


def test_a_name_written_in_two_modules_has_no_reader_both_writes_reach() -> None:
    """Patched through the package into one owner and through the reader into another, a
    name is read where one write lands and the other misses -- so both rules flag it."""
    homes = {"_glob_to_regex": {READER, f"{PACKAGE}.shell_normalizer_walk"}}
    bare = {READER: "def f():\n    return _glob_to_regex('x')\n"}
    assert bare_read_violations(bare, homes) == [
        f"{READER} reads _glob_to_regex, written in "
        f"['{READER}', '{PACKAGE}.shell_normalizer_walk']"
    ]


def test_the_loop_reader_reads_the_constants_a_loop_patches() -> None:
    source = (
        "from kiro_crew import security as sec\n"
        "def test_x(monkeypatch):\n"
        "    for name in ('_shell_tokens', '_argv_programs'):\n"
        "        monkeypatch.setattr(sec, name, None)\n"
        "    for name in ('not_a_patch',):\n"
        "        print(name)\n"
    )
    assert loop_patched_names(source) == {PACKAGE: {"_shell_tokens", "_argv_programs"}}


def test_the_facade_defines_only_its_machinery() -> None:
    """Every control lives in an owner: the facade defines the resolver, the forwarding
    module class and its logger, and nothing a caller runs to decide anything."""
    tree = ast.parse((SECURITY_DIR / "__init__.py").read_text(encoding="utf-8"))
    defined = sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    assert defined == ["_ReExportModule", "__dir__", "__getattr__", "_owner", "_submodule"]


def test_the_shell_reader_owners_never_import_the_reader() -> None:
    """The lexical layer sits below attribution: an owner importing ``shell_normalizer``
    would make the pair a load-time cycle and let attribution leak into the lexer."""
    importing = {}
    for stem in _reader_owners():
        tree = ast.parse((SECURITY_DIR / f"{stem}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                (node.level == 1 and node.module == "shell_normalizer")
                or node.module == READER
                or (
                    node.level == 1
                    and node.module is None
                    and any(alias.name == "shell_normalizer" for alias in node.names)
                )
            ):
                importing.setdefault(stem, []).append(node.lineno)
    assert importing == {}


def test_the_facade_owners_take_only_the_resolver_and_the_logger_from_it() -> None:
    """An owner binding any other package name by ``from kiro_crew.security import`` would
    hold a copy no patch on the package reaches."""
    taken: dict[str, set[str]] = {}
    for stem in _facade_owners():
        tree = ast.parse((SECURITY_DIR / f"{stem}.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == PACKAGE:
                taken.setdefault(stem, set()).update(alias.name for alias in node.names)
    assert taken, "no owner imports from the package -- the scan went blind"
    assert {stem: names - {"_submodule", "logger"} for stem, names in taken.items()} == {
        stem: set() for stem in taken
    }


def test_the_family_logs_under_the_facade_name() -> None:
    """Moved code logs through the logger the facade always used, named for the package."""
    assert security.logger.name == PACKAGE
    holders = [importlib.import_module(name) for name in _family()]
    others = [
        module.__name__
        for module in holders
        if vars(module).get("logger", security.logger) is not security.logger
    ]
    assert others == []


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_security.py" in walked


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
