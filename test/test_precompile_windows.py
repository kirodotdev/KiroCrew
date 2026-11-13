from __future__ import annotations

import importlib.machinery
import importlib.util
import py_compile
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

SCRIPT = Path(__file__).parents[1] / "packaging" / "precompile_windows.py"
SPEC = importlib.util.spec_from_file_location("precompile_windows", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
_PREVIOUS_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    SPEC.loader.exec_module(MODULE)
finally:
    sys.dont_write_bytecode = _PREVIOUS_DONT_WRITE_BYTECODE


def test_precompiles_import_closure_as_relocatable_unchecked_hash_pyc(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "runtime"
    package = root / "sample_startup"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from . import dependency\n", encoding="utf-8")
    (package / "dependency.py").write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in ("sample_startup", "sample_startup.dependency"):
        sys.modules.pop(name, None)
    # The packaging step writes each pyc at ``cache_from_source()``, i.e. into a
    # ``__pycache__`` BESIDE the shipped source -- that is what "relocatable"
    # means for it. The suite's rootdir conftest sets ``sys.pycache_prefix`` so
    # the checkout's own imports cache elsewhere, which would send this test's
    # pycs into a per-user cache tree mirroring ``tmp_path``'s absolute path:
    # a new orphaned tree on the operator's disk every run, and not the layout
    # under test. Clear the prefix for this test so the layout is the shipped one.
    monkeypatch.setattr(sys, "pycache_prefix", None)

    count, total_bytes = MODULE.precompile_import_closure(root, ["sample_startup"])

    assert count == 2
    assert total_bytes > 0
    for source in package.glob("*.py"):
        cache = Path(importlib.util.cache_from_source(str(source)))
        assert cache.is_relative_to(root), f"pyc landed outside the runtime tree: {cache}"
        assert cache.parent == package / "__pycache__"
        payload = cache.read_bytes()
        flags = struct.unpack("<I", payload[4:8])[0]
        # Bit 0 = hash-based, which is what survives extraction restamping the
        # sources; a TIMESTAMP pyc would look stale on the user's machine and be
        # rewritten in place. Bit 1 = check_source, which must stay CLEAR: with
        # it set the loader reads and hashes every .py in addition to the .pyc,
        # measured at 43.55 MB and 1639 extra cold file opens per Windows boot
        # (median 16718 ms vs 4192 ms, n=5 per arm) for byte-identical code.
        assert flags & 0b01, "pyc must be hash-based to survive extraction restamping"
        assert not flags & 0b10, "source-check must be off: it costs ~12.5s per cold boot"
        assert flags == 0b01  # UNCHECKED_HASH exactly, not some future third mode
        code = importlib.machinery.SourcelessFileLoader(source.stem, str(cache)).get_code(
            source.stem
        )
        assert code is not None
        assert code.co_filename == source.relative_to(root).as_posix()


def _sample_closure(root: Path, name: str, monkeypatch) -> Path:
    """A two-module package under ``root`` whose imports leave nothing behind."""

    package = root / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from . import dependency\n", encoding="utf-8")
    (package / "dependency.py").write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    # Same reason as the test above: the shipped layout is a ``__pycache__``
    # beside the source, not the suite's redirected cache tree.
    monkeypatch.setattr(sys, "pycache_prefix", None)
    for module in (name, f"{name}.dependency"):
        monkeypatch.delitem(sys.modules, module, raising=False)
    return package


def test_checked_hash_mode_ships_caches_the_loader_revalidates(tmp_path: Path, monkeypatch) -> None:
    # The macOS lane asks for CHECKED_HASH: the loader hashes the source before
    # trusting the cache, so an edited source is never run from stale bytecode.
    root = tmp_path / "runtime"
    package = _sample_closure(root, "sample_checked", monkeypatch)

    count, _ = MODULE.precompile_import_closure(
        root, ["sample_checked"], MODULE.INVALIDATION_MODES["checked-hash"]
    )

    assert count == 2
    for source in package.glob("*.py"):
        payload = Path(importlib.util.cache_from_source(str(source))).read_bytes()
        assert struct.unpack("<I", payload[4:8])[0] == 0b11, "hash-based AND source-checked"
        assert payload[8:16] == importlib.util.source_hash(source.read_bytes())


def test_verify_names_every_closure_module_without_a_valid_cache(
    tmp_path: Path, monkeypatch
) -> None:
    # The macOS coverage gate. Each gap it must name is one the runtime would
    # pay for by compiling in memory on every launch: no cache, a cache in the
    # other mode, and a cache built from bytes that are not the shipped source.
    root = tmp_path / "runtime"
    package = _sample_closure(root, "sample_verified", monkeypatch)
    checked = MODULE.INVALIDATION_MODES["checked-hash"]
    unchecked = MODULE.INVALIDATION_MODES["unchecked-hash"]
    entry = ["sample_verified"]
    verify = ["--root", str(root), "--module", entry[0], "--verify"]
    verify += ["--invalidation-mode", "checked-hash"]

    assert len(MODULE.uncovered_import_closure(root, entry, checked)) == 2

    MODULE.precompile_import_closure(root, entry, checked)
    assert MODULE.uncovered_import_closure(root, entry, checked) == []
    assert MODULE.main(verify) == 0

    init_cache = Path(importlib.util.cache_from_source(str(package / "__init__.py")))
    init_cache.unlink()
    dependency = package / "dependency.py"
    py_compile.compile(
        str(dependency),
        cfile=importlib.util.cache_from_source(str(dependency)),
        invalidation_mode=unchecked,
    )
    problems = MODULE.uncovered_import_closure(root, entry, checked)
    assert problems == [
        "sample_verified/__init__.py: no cache",
        "sample_verified/dependency.py: compiled with flags 0b01, expected 0b11",
    ]
    assert MODULE.main(verify) == 1

    MODULE.precompile_import_closure(root, entry, checked)
    dependency.write_text("VALUE = 43\n", encoding="utf-8")
    assert MODULE.uncovered_import_closure(root, entry, checked) == [
        "sample_verified/dependency.py: compiled from different source bytes"
    ]


def _child_writes_bytecode(site_packages: Path, project: Path) -> bool:
    # ``-I`` is the environment an MCP stub launched by the agent runtime gets:
    # no PYTHONDONTWRITEBYTECODE (and no PYTHONPYCACHEPREFIX) reaches it, so
    # only something inside the interpreter's own site-packages can stop it.
    probe = (
        "import site, sys; "
        f"site.addsitedir({str(site_packages)!r}); "
        f"sys.path.insert(0, {str(project)!r}); "
        "import ban_probe_module"
    )
    subprocess.run([sys.executable, "-I", "-c", probe], check=True, timeout=FALLBACK_TIMEOUT_SECS)
    return (project / "__pycache__").exists()


def test_the_write_ban_stops_an_interpreter_that_never_saw_the_environment_variable(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "ban_probe_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert _child_writes_bytecode(site_packages, project), "control: an unbarred child writes"
    shutil.rmtree(project / "__pycache__")

    installed = MODULE.install_bytecode_write_ban(site_packages)

    assert installed.parent == site_packages
    assert not _child_writes_bytecode(site_packages, project)


def test_the_write_ban_runs_before_any_other_pth_in_its_directory() -> None:
    # ``site`` executes .pth files in name order, and one that imports a
    # site-packages module ahead of the ban would still cache that module.
    neighbours = ["_virtualenv.pth", "distutils-precedence.pth", "easy-install.pth", "a.pth"]
    assert sorted([*neighbours, MODULE.WRITE_BAN_PTH])[0] == MODULE.WRITE_BAN_PTH
