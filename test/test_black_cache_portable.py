"""Black's cache must survive a checkout that moves, and nothing else.

``scripts/black_cache_portable.py`` carries Black's cache between main-ratchet-audit
runs whose checkout root differs every build. These tests pin the properties that
keep the cached gate's verdict equal to a cold run's: only a file whose bytes still
match what Black found formatted is handed back to Black, Black writes and reads its
own cache file, and anything that could change Black's answer (another Black
version, another mode, a malformed entry, a ``black.cache`` without the expected
API) starts cold instead of crashing.

The tests seed and read Black's cache through ``black.cache.Cache`` itself, the
same API the script uses, so neither side handles Black's file format directly.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

import black
import black.cache as black_cache
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "black_cache_portable.py"
WORKFLOW = ROOT / ".github" / "workflows" / "main-ratchet-audit.yml"

SPEC = importlib.util.spec_from_file_location("black_cache_portable", SCRIPT)
assert SPEC and SPEC.loader
portable = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portable)

PYPROJECT = '[tool.black]\nline-length = 100\nforce-exclude = "/_vendor/"\n'


@pytest.fixture(autouse=True)
def cache_env(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The floor's own MonkeyPatch (D11), so a test's monkeypatch.undo() cannot
    # lift the pin and point Black at the real user cache.
    _floor_monkeypatch.setenv("BLACK_CACHE_DIR", str(tmp_path / "cache"))


def _checkout(root: Path, files: dict[str, str], pyproject: str = PYPROJECT) -> Path:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "scripts" / "check_black_formatting.py", root / "scripts")
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bytes, not text: text mode writes \r\n on Windows, and the recorded
        # digests below are of the "\n" bytes.
        path.write_bytes(text.encode("utf-8"))
    return root.resolve()


def _black_cache_for(root: Path) -> black_cache.Cache:
    _cls, mode, version_dir = portable._black(root)
    return black_cache.Cache.read(mode, version_dir)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _saved(tmp_path: Path, root: Path, entries: dict, **override: object) -> Path:
    payload = {
        "format": portable.FORMAT,
        "black": black.__version__,
        "cache_file": Path(_black_cache_for(root).cache_file).name,
        "files": entries,
        **override,
    }
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps(payload))
    return saved


def test_the_mode_is_the_gates_own() -> None:
    # The gate passes --target-version py310 and pyproject sets line-length 100;
    # the cache file this module reads and writes is the one that run uses.
    _cls, mode, _dir = portable._black(ROOT)
    assert mode.target_versions == {black.TargetVersion.PY310}
    assert mode.line_length == 100


def test_round_trip_re_roots_entries_to_the_new_checkout(tmp_path: Path) -> None:
    files = {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"}
    old_root = _checkout(tmp_path / "build-1" / "repo", files)
    _black_cache_for(old_root).write([old_root / p for p in files])
    saved = tmp_path / "saved.json"
    assert portable.export(saved, root=old_root) == 0
    assert json.loads(saved.read_text())["files"] == {
        "src/a.py": _digest("a = 1\n"),
        "src/b.py": _digest("b = 1\n"),
    }

    Path(_black_cache_for(old_root).cache_file).unlink()
    new_root = _checkout(tmp_path / "build-2" / "repo", {**files, "src/b.py": "b  =  2\n"})
    assert portable.restore(saved, root=new_root) == 0
    # b.py's bytes changed, so Black is not told it is formatted: it re-checks it.
    assert set(_black_cache_for(new_root).file_data) == {str(new_root / "src" / "a.py")}


def test_export_drops_entries_outside_the_checkout(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "repo", {"src/a.py": "a = 1\n"})
    other = tmp_path / "elsewhere.py"
    other.write_text("b = 1\n")
    _black_cache_for(root).write([root / "src" / "a.py", other])
    saved = tmp_path / "saved.json"
    portable.export(saved, root=root)
    assert set(json.loads(saved.read_text())["files"]) == {"src/a.py"}


def test_another_black_version_starts_cold(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "repo", {"a.py": "a = 1\n"})
    saved = _saved(tmp_path, root, {"a.py": _digest("a = 1\n")}, black="25.9.0")
    assert portable.restore(saved, root=root) == 0
    assert _black_cache_for(root).file_data == {}


def test_a_black_config_change_starts_cold(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "repo", {"a.py": "a = 1\n"})
    saved = _saved(tmp_path, root, {"a.py": _digest("a = 1\n")})
    # A second checkout, not an edit in place: Black memoises the parsed
    # pyproject.toml per path for the life of the process (lru_cache).
    changed = _checkout(
        tmp_path / "repo-2", {"a.py": "a = 1\n"}, "[tool.black]\nline-length = 99\n"
    )
    assert portable.restore(saved, root=changed) == 0
    assert _black_cache_for(changed).file_data == {}
    assert _black_cache_for(root).file_data == {}


def test_an_unmapped_black_setting_turns_the_cache_off(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "repo", {"a.py": "a = 1\n"}, PYPROJECT + "preview = true\n")
    with pytest.raises(portable.ColdStart, match="preview"):
        portable._black(root)
    saved = tmp_path / "out.json"
    assert portable.export(saved, root=root) == 0
    assert json.loads(saved.read_text())["files"] == {}


@pytest.mark.parametrize("case", ["absolute", "dot-dot", "backslash", "empty"])
def test_paths_outside_the_checkout_are_dropped(tmp_path: Path, case: str) -> None:
    root = _checkout(tmp_path / "repo", {})
    text = "a = 1\n"
    # Each key names a REAL file holding the recorded bytes, so only the path
    # check can be what refuses it, not a missing file or a hash mismatch.
    outside = tmp_path / "a.py"
    outside.write_bytes(text.encode())
    # On Windows the backslash is a separator, so this is src/a.py there.
    backslashed = root / "src\\a.py"
    backslashed.parent.mkdir(parents=True, exist_ok=True)
    backslashed.write_bytes(text.encode())
    relative = {
        "absolute": str(outside),
        "dot-dot": "../a.py",
        "backslash": "src\\a.py",
        "empty": "",
    }[case]
    assert case == "empty" or (root / relative).is_file()
    saved = _saved(tmp_path, root, {relative: _digest(text)})
    assert portable.restore(saved, root=root) == 0
    assert _black_cache_for(root).file_data == {}


@pytest.mark.parametrize(
    "body",
    [None, "not json", json.dumps({"format": 1, "files": {"a.py": "x"}}), json.dumps([])],
)
def test_a_missing_or_unreadable_saved_cache_starts_cold(tmp_path: Path, body: str | None) -> None:
    root = _checkout(tmp_path / "repo", {"a.py": "a = 1\n"})
    saved = tmp_path / "saved.json"
    if body is not None:
        saved.write_text(body)
    assert portable.restore(saved, root=root) == 0
    assert not (tmp_path / "cache").exists()


def _changed_black_cache(change: str, recorded: dict[str, str]) -> tuple[types.ModuleType, list]:
    """A stand-in for a future ``black.cache`` whose API changed shape.

    Its cache already holds *recorded* (absolute path -> digest), so an export
    that ignores the change would produce entries, and every write through it
    is recorded rather than raised, so a swallowed error cannot hide one.
    """
    fake = types.ModuleType("black.cache")
    fake.get_cache_dir = black_cache.get_cache_dir
    written: list = []

    class Cache:
        def __init__(self, cache_file: Path) -> None:
            self.cache_file = cache_file
            self.file_data: object = {
                key: types.SimpleNamespace(hash=digest) for key, digest in recorded.items()
            }

        @classmethod
        def read(cls, mode, cache_dir):
            return cls(cache_dir / f"cache.{mode.get_cache_key()}.pickle")

        def write(self, sources):
            written.append(list(sources))

        @staticmethod
        def hash_digest(path):
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    if change == "no read":
        del Cache.read
    elif change == "read takes one argument":
        Cache.read = classmethod(lambda cls, mode: cls(Path("/x")))  # type: ignore[assignment]
    elif change == "write renamed":
        Cache.store = Cache.write  # type: ignore[attr-defined]
        del Cache.write
    elif change == "file_data is a list":
        original = Cache.__init__

        def init(self, cache_file):
            original(self, cache_file)
            self.file_data = list(self.file_data)

        Cache.__init__ = init  # type: ignore[method-assign]
    fake.Cache = Cache
    return fake, written


@pytest.mark.parametrize(
    "change", ["no read", "read takes one argument", "write renamed", "file_data is a list"]
)
def test_a_changed_black_cache_api_starts_cold_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    root = _checkout(tmp_path / "repo", {"a.py": "a = 1\n"})
    saved = _saved(tmp_path, root, {"a.py": _digest("a = 1\n")})
    fake, written = _changed_black_cache(change, {str(root / "a.py"): _digest("a = 1\n")})
    monkeypatch.setitem(sys.modules, "black.cache", fake)
    monkeypatch.setattr(black, "cache", fake, raising=False)

    assert portable.restore(saved, root=root) == 0
    assert written == []

    out = tmp_path / "exported.json"
    assert portable.export(out, root=root) == 0
    exported = json.loads(out.read_text())["files"]
    if change == "write renamed":
        # export never writes, so an API whose read still works is still read.
        assert exported == {"a.py": _digest("a = 1\n")}
    else:
        assert exported == {}


def test_a_missing_black_cache_module_starts_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path / "repo", {"a.py": "a = 1\n"})
    saved = _saved(tmp_path, root, {"a.py": _digest("a = 1\n")})
    monkeypatch.setitem(sys.modules, "black.cache", None)
    monkeypatch.delattr(black, "cache", raising=False)
    assert portable.restore(saved, root=root) == 0
    out = tmp_path / "exported.json"
    assert portable.export(out, root=root) == 0
    assert json.loads(out.read_text())["files"] == {}


def test_the_script_never_handles_blacks_file_format_itself() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "import pickle" not in source and "pickle." not in source


def test_the_audit_saves_from_main_pushes_only() -> None:
    # Only main may write the entry main restores; every other ref reads it.
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["ratchet-gates"]["steps"]
    writers = [
        s
        for s in steps
        if "cache/save" in str(s.get("uses", ""))
        or "black_cache_portable.py export" in str(s.get("run", ""))
    ]
    assert len(writers) == 2
    for step in writers:
        condition = str(step.get("if", ""))
        assert "github.event_name == 'push'" in condition
        assert "github.ref == 'refs/heads/main'" in condition


def test_the_cached_path_is_the_same_string_on_every_runner() -> None:
    # actions/cache folds the path string into the entry's version. A path built
    # from runner.temp expands to the CodeBuild build's own random root, so every
    # save landed under a version no later build asked for: measured as a miss on
    # the exact key the previous run had just saved.
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["ratchet-gates"]["steps"]
    paths = {s["with"]["path"] for s in steps if "actions/cache/" in str(s.get("uses", ""))}
    assert len(paths) == 1
    (path,) = paths
    assert "${{" not in path and not path.startswith("/")
    runs = [
        str(s.get("run", "")) for s in steps if "black_cache_portable.py" in str(s.get("run", ""))
    ]
    assert len(runs) == 2 and all(run.endswith(f" {path}") for run in runs)


def test_the_black_gate_reads_the_rehydrated_cache() -> None:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["ratchet-gates"]["steps"]
    names = [s.get("name") for s in steps]
    gate = names.index("Check formatting (black, baselined)")
    assert (
        names.index("Rehydrate black's cache for this checkout")
        < gate
        < names.index("Export black's cache")
    )
    dirs = {
        s["env"]["BLACK_CACHE_DIR"]
        for s in steps
        if "black_cache_portable.py" in str(s.get("run", "")) or s.get("name") == names[gate]
    }
    assert len(dirs) == 1
