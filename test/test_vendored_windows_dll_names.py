"""The vendored Windows ggml DLLs carry private names.

Windows keeps one module per base name in a process: an import is bound to a
module of that name that is already loaded, before any directory is searched
and even when the importing DLL was opened by full path. Under upstream's
names, a second ``ggml.dll`` in the process (a source-built whisper.cpp) and the
vendored one were therefore one module, whichever loaded first.

``scripts/rename_vendored_win_dlls.py`` renames the three ggml files and points
the import tables at the new names. The tests here read the committed files as
bytes on every platform; the last group loads them, and runs on Windows only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import embeddings_native
from kiro_crew._llama_lib_path import _LIB_PATH_ENV, _REQUIRED_VENDORED_LIBS
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "rename_vendored_win_dlls.py"
_VENDOR = Path(kiro_crew.__file__).resolve().parent / "_vendor"
_LIBS = _VENDOR / "llama_cpp_libs" / "win_amd64"
_README = _REPO_ROOT / "src" / "kiro_crew" / "_vendor" / "README.md"

_spec = importlib.util.spec_from_file_location("rename_vendored_win_dlls", _SCRIPT)
assert _spec is not None and _spec.loader is not None
rename = importlib.util.module_from_spec(_spec)
# Registered before it runs: its dataclasses look their own module up by name.
sys.modules[_spec.name] = rename
_spec.loader.exec_module(rename)

_PUBLIC = frozenset(rename.PUBLIC_NAMES)
_HASH_NAMED = re.compile(r"^ggml(-base|-cpu)?-([0-9a-f]{8})\.dll$")
_README_ROW = re.compile(
    r"^\| `([\w.-]+)` ([0-9a-f]{64}) \| `([\w.-]+)` ([0-9a-f]{64}) \|$", re.MULTILINE
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _committed() -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(_LIBS.glob("*.dll"))}


def _readme_hashes() -> dict[str, tuple[str, str, str]]:
    """``{upstream name: (sha256 before, shipped name, sha256 after)}`` from the README table."""
    text = _README.read_text(encoding="utf-8")
    section = text.split("### Private ggml names on Windows", 1)[1].split("\nTo upgrade:", 1)[0]
    return {old: (before, new, after) for old, before, new, after in _README_ROW.findall(section)}


def test_no_upstream_ggml_name_is_a_file_name_or_an_import() -> None:
    files = _committed()
    assert set(files) == set(_REQUIRED_VENDORED_LIBS["win_amd64"])
    assert not set(files) & _PUBLIC, "win_amd64 ships an upstream ggml file name"
    lowered = {name.lower() for name in files}
    for name, data in files.items():
        image = rename.read_image(data)
        imported = [entry.name.lower() for entry in image.imports]
        assert not set(imported) & _PUBLIC, f"{name} imports an upstream ggml name"
        for dependency in imported:
            if dependency.startswith(("ggml", "llama")):
                assert dependency in lowered, f"{name} imports {dependency}, absent from win_amd64"
        if name.startswith("ggml"):
            assert _HASH_NAMED.match(name), f"{name} is not hash-named"


def test_every_ggml_import_was_renamed() -> None:
    """The five import descriptors upstream's files carry, each under its private name."""
    files = _committed()
    private = {name[: name.rindex("-")]: name for name in files if name.startswith("ggml")}
    ggml_imports = {
        name[: name.rindex("-")] if name.startswith("ggml") else name: sorted(
            entry.name
            for entry in rename.read_image(data).imports
            if entry.name.lower().startswith("ggml")
        )
        for name, data in files.items()
    }
    assert ggml_imports == {
        "llama.dll": sorted([private["ggml"], private["ggml-base"]]),
        "ggml": sorted([private["ggml-cpu"], private["ggml-base"]]),
        "ggml-cpu": [private["ggml-base"]],
        "ggml-base": [],
    }


def test_the_files_stay_ones_the_rename_script_accepts() -> None:
    """No signature to break, and no import table the script leaves under the old names."""
    for name, data in _committed().items():
        directories = rename.read_image(data).directories
        for index, what in (
            (4, "an Authenticode signature"),
            (11, "bound imports"),
            (13, "delay-load imports"),
        ):
            assert directories[index] == (0, 0), f"{name} has {what}"


def test_restoring_gives_the_upstream_files_the_readme_records() -> None:
    """The committed files are upstream's plus exactly the documented rename."""
    files = _committed()
    restored = rename.restored_files(_LIBS)
    recorded = _readme_hashes()
    assert set(restored) == {"llama.dll", *_PUBLIC} == set(recorded)
    names = {}
    for upstream, (before, shipped, after) in recorded.items():
        assert (
            _sha256(restored[upstream]) == before
        ), f"restored {upstream} is not the recorded upstream file"
        assert _sha256(files[shipped]) == after, f"{shipped} is not the file the README records"
        if upstream in _PUBLIC:
            assert shipped == rename.private_name(upstream, restored[upstream])
            names[upstream] = shipped
    for upstream, data in restored.items():
        assert rename.patch(data, names) == files[names.get(upstream, upstream)], upstream


def test_the_rename_leaves_the_exports_alone() -> None:
    files = _committed()
    for upstream, data in rename.restored_files(_LIBS).items():
        shipped = rename.private_name(upstream, data) if upstream in _PUBLIC else upstream
        exported, addresses = rename.export_table(files[shipped])
        assert exported, f"{shipped} exports nothing"
        assert (exported, addresses) == rename.export_table(
            data
        ), f"{shipped} exports differ from upstream's"


def test_the_script_renames_a_directory_of_upstream_files(tmp_path: Path) -> None:
    for name, data in rename.restored_files(_LIBS).items():
        (tmp_path / name).write_bytes(data)

    names = rename.rename_in_place(tmp_path)

    assert set(names) == _PUBLIC
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == _committed()
    with pytest.raises(rename.UnsupportedImage, match="already renamed"):
        rename.rename_in_place(tmp_path)


def test_a_signed_file_is_refused() -> None:
    data = bytearray(rename.restored_files(_LIBS)["llama.dll"])
    image = rename.read_image(bytes(data))
    # A non-empty security directory is what a signed file has.
    data[image.optional_offset + 112 + 8 * 4 : image.optional_offset + 112 + 8 * 4 + 8] = (
        b"\x01" * 8
    )
    with pytest.raises(rename.UnsupportedImage, match="signature"):
        rename.patch(bytes(data), {"ggml.dll": "ggml-00000000.dll"})


#: Loads the vendored runtime and a second, upstream-named set in the order
#: given, then prints every ggml and llama module the process has mapped.
_LOAD_BOTH = r"""
import ctypes, json, os, sys
from ctypes import wintypes
from kiro_crew import embeddings_native

order, foreign = sys.argv[1], sys.argv[2]
kept = []


def load_vendored():
    if embeddings_native._load_llama_class() is None:
        raise SystemExit("the vendored llama.cpp runtime did not load")


def load_foreign(name):
    kept.append(ctypes.WinDLL(os.path.join(foreign, name)))


if order == "foreign-ggml-first":
    load_foreign("ggml.dll")
    load_vendored()
else:
    load_vendored()
    load_foreign("llama.dll")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.K32EnumProcessModules.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
kernel32.K32EnumProcessModules.restype = wintypes.BOOL
kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
kernel32.GetModuleFileNameW.restype = wintypes.DWORD

process = kernel32.GetCurrentProcess()
count = 1024
while True:
    handles = (wintypes.HMODULE * count)()
    needed = wintypes.DWORD()
    if not kernel32.K32EnumProcessModules(process, handles, ctypes.sizeof(handles), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if needed.value <= ctypes.sizeof(handles):
        break
    count = needed.value // ctypes.sizeof(wintypes.HMODULE) + 64

mapped = []
path = ctypes.create_unicode_buffer(32768)
for handle in handles[: needed.value // ctypes.sizeof(wintypes.HMODULE)]:
    if handle and kernel32.GetModuleFileNameW(handle, path, len(path)):
        if os.path.basename(path.value).lower().startswith(("ggml", "llama")):
            mapped.append(path.value)
print("MAPPED:" + json.dumps(sorted(mapped)), flush=True)
"""


def _mapped(order: str, foreign: Path) -> set[tuple[str, str]]:
    """``(which set, file name)`` of every ggml and llama module a child maps."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    # An operator's directory would replace the vendored one under test.
    env.pop(_LIB_PATH_ENV, None)
    done = subprocess.run(
        [sys.executable, "-c", _LOAD_BOTH, order, str(foreign)],
        cwd=foreign.parent,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    (line,) = [line for line in done.stdout.splitlines() if line.startswith("MAPPED:")]
    found = set()
    for path in json.loads(line[len("MAPPED:") :]):
        folder = os.path.dirname(path)
        owner = (
            "vendored"
            if os.path.samefile(folder, _LIBS)
            else "foreign" if os.path.samefile(folder, foreign) else path
        )
        found.add((owner, os.path.basename(path).lower()))
    return found


@pytest.mark.skipif(sys.platform != "win32", reason="loads Windows DLLs")
class TestTwoGgmlSetsInOneProcess:
    """The upstream files, restored into a directory of their own, stand in for a
    user's build: same names as upstream's, different files from the vendored ones."""

    @pytest.fixture
    def foreign(self, tmp_path: Path) -> Path:
        missing = embeddings_native._missing_windows_msvc_runtime()
        if missing:
            pytest.skip(f"the Microsoft Visual C++ runtime is not installed: {', '.join(missing)}")
        folder = tmp_path / "foreign"
        folder.mkdir()
        for name, data in rename.restored_files(_LIBS).items():
            (folder / name).write_bytes(data)
        return folder

    def _vendored(self) -> set[tuple[str, str]]:
        return {("vendored", name.lower()) for name in _REQUIRED_VENDORED_LIBS["win_amd64"]}

    def test_a_ggml_loaded_first_does_not_serve_the_vendored_llama(self, foreign: Path) -> None:
        assert _mapped("foreign-ggml-first", foreign) == self._vendored() | {
            ("foreign", "ggml.dll"),
            ("foreign", "ggml-base.dll"),
            ("foreign", "ggml-cpu.dll"),
        }

    def test_a_llama_loaded_afterwards_maps_its_own_ggml(self, foreign: Path) -> None:
        assert _mapped("vendored-first", foreign) == self._vendored() | {
            ("foreign", "llama.dll"),
            ("foreign", "ggml.dll"),
            ("foreign", "ggml-base.dll"),
            ("foreign", "ggml-cpu.dll"),
        }
