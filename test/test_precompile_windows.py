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


def test_precompiles_import_closure_as_relocatable_checked_hash_pyc(
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

    count, total_bytes = MODULE.precompile_import_closure(root, ["sample_startup"])

    assert count == 2
    assert total_bytes > 0
    for source in package.glob("*.py"):
        cache = Path(importlib.util.cache_from_source(str(source)))
        payload = cache.read_bytes()
        assert struct.unpack("<I", payload[4:8])[0] == 3  # checked-hash pyc
        code = importlib.machinery.SourcelessFileLoader(source.stem, str(cache)).get_code(
            source.stem
        )
        assert code is not None
        assert code.co_filename == source.relative_to(root).as_posix()
