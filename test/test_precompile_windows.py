from __future__ import annotations

import importlib.machinery
import importlib.util
import struct
import sys
from pathlib import Path

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
