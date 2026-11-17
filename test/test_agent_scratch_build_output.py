"""The scratch quarantine drops native build output and keeps the sources beside it.

A dead tree holding a repository is quarantined minus what tools rebuild
(``agent_scratch._strip_regenerable``). Native build output is known by the
build file beside it, or for Xcode DerivedData by what it holds
(``agent_scratch_build_output``), so a folder of the same name with no such
marker is kept, and a link is neither a marker nor followed.
"""

from __future__ import annotations

import ast
import builtins
import io
import os
import time
from pathlib import Path

import pytest
from test_agent_scratch import _GIT, _git, _snapshot

from kiro_crew import agent_scratch as sc
from kiro_crew.testing.links import make_dir_link

pytestmark = pytest.mark.skipif(_GIT is None, reason="needs a git binary")


@pytest.fixture
def scratch_root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    # Every owner is dead and no process uses a tree, whatever this host runs.
    monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: False)
    monkeypatch.setattr(sc.agent_scratch_users, "scratch_users", lambda root, keys: {})
    return home / "scratch"


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def _write(tree: Path, *relatives: str, text: str = "x\n") -> None:
    for relative in relatives:
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        (tree / relative).write_text(text)


def _dead_clone(upstream: Path) -> tuple[Path, Path]:
    path = sc.allocate_scratch("gitwork")
    sc.record_owner(path, 2**22 - 1)  # dead: the fixture stubs liveness
    _git(path, "clone", "-q", str(upstream), "clone")
    return path, path / "clone"


def _sweep() -> int:
    return sc.sweep_dead_scratch(now=time.time() + 2 * sc._UNOWNED_GRACE_SECONDS)


def _moved(path: Path) -> Path:
    assert not os.path.lexists(path)
    moved = [p for p in sc.quarantine_dir().iterdir() if p.name.endswith(f"-{path.name}")]
    assert len(moved) == 1, moved
    return moved[0]


def _kept_whole(path: Path) -> None:
    before = _snapshot(path)
    assert _sweep() == 0
    assert _snapshot(_moved(path)) == before


@pytest.mark.parametrize(
    ("marker", "outputs"),
    [
        ("Package.swift", (".build/debug/App.o", ".build/checkouts/dep/.git/HEAD")),
        ("build.gradle", (".gradle/8.5/fileHashes.bin", "build/classes/Main.class")),
        ("build.gradle.kts", ("build/libs/app.jar",)),
        ("settings.gradle", (".gradle/config.properties", "build/tmp/x")),
        ("settings.gradle.kts", (".gradle/config.properties", "build/tmp/x")),
        ("Cargo.toml", ("target/debug/app",)),
    ],
)
def test_build_output_beside_its_build_file_is_dropped(
    scratch_root: Path, upstream: Path, marker: str, outputs: tuple[str, ...]
) -> None:
    path, clone = _dead_clone(upstream)
    _write(clone, marker, "src/main.txt")
    kept = _snapshot(path)
    _write(clone, *outputs)

    assert _sweep() == 0
    assert _snapshot(_moved(path)) == kept


def test_build_dirs_without_their_marker_are_kept(scratch_root: Path, upstream: Path) -> None:
    path, clone = _dead_clone(upstream)
    _write(clone, "build/notes.txt", "target/plan.txt", ".build/x", ".gradle/y")
    # SourcePackages beside Build, outside any DerivedData folder.
    _write(clone, "module/SourcePackages/z", "module/Build/w", "module/draft.txt")
    _write(clone, "other/info.plist", "other/Build/w")

    _kept_whole(path)


def test_a_build_dir_that_is_a_repository_is_kept(scratch_root: Path, upstream: Path) -> None:
    path, clone = _dead_clone(upstream)
    _write(clone, "Cargo.toml")
    _git(clone, "clone", "-q", str(upstream), "target")

    _kept_whole(path)


def test_a_tree_whose_only_repositories_are_dependency_clones_is_deleted(
    scratch_root: Path,
) -> None:
    path = sc.allocate_scratch("gitwork")
    sc.record_owner(path, 2**22 - 1)
    _write(path, "app/Package.swift", "app/Sources/main.swift")
    (path / "app" / ".build" / "checkouts").mkdir(parents=True)
    _git(path / "app" / ".build" / "checkouts", "init", "-q", "dep")

    assert not sc.holds_git_repository(path)
    assert _sweep() == 1
    assert not path.exists()


def test_native_output_wins_over_a_venv_marker(scratch_root: Path, upstream: Path) -> None:
    # Judged as build output, it is not searched as a venv for the repository in it.
    path = sc.allocate_scratch("gitwork")
    sc.record_owner(path, 2**22 - 1)
    _write(path, "app/Cargo.toml", "app/target/pyvenv.cfg")
    (path / "app" / "target" / "deps").mkdir()
    _git(path / "app" / "target" / "deps", "init", "-q", "dep")

    assert not sc.holds_git_repository(path)


def test_a_linked_build_file_does_not_count(scratch_root: Path, upstream: Path) -> None:
    path, clone = _dead_clone(upstream)
    _write(clone, "real.toml", "target/plan.txt")
    try:
        (clone / "Cargo.toml").symlink_to("real.toml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    _kept_whole(path)


def test_a_linked_build_dir_is_neither_followed_nor_removed(
    scratch_root: Path, upstream: Path, tmp_path: Path
) -> None:
    path, clone = _dead_clone(upstream)
    outside = tmp_path / "outside-target"
    _write(outside, "debug/app")
    _write(clone, "Cargo.toml")
    make_dir_link(clone / "target", outside)

    _kept_whole(path)
    assert (outside / "debug" / "app").exists()


def test_the_module_imports_no_plist_or_xml_parser() -> None:
    # Repository bytes never reach a deserializer, so none may be imported.
    source = Path(sc.__file__).with_name("agent_scratch_build_output.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"plistlib", "xml", "pyexpat", "lxml", "defusedxml"}, imported


def _shared_offset_bplist(count: int) -> bytes:
    """A binary plist under 64 KiB: one array holding *count* references to one string."""
    header = b"bplist00"
    array = bytes([0xAF, 0x11]) + count.to_bytes(2, "big") + b"\x01" * count  # refs to object 1
    string = b"\x5f\x10\x10" + b"x" * 16
    body = header + array + string
    offsets = [len(header), len(header) + len(array)]
    table = b"".join(o.to_bytes(4, "big") for o in offsets)
    trailer = (
        b"\x00" * 6
        + bytes([4, 1])
        + (2).to_bytes(8, "big")
        + (0).to_bytes(8, "big")
        + len(body).to_bytes(8, "big")
    )
    return body + table + trailer


def _derived(tree: Path, name: str = "DerivedData/App-abc") -> Path:
    """A DerivedData tree at *name* under *tree*: info.plist plus Build/."""
    _write(tree, f"{name}/info.plist", f"{name}/Build/Products/App.app/App")
    return tree / name


@pytest.mark.parametrize("output", ["Build", "Index.noindex", "Logs", "SourcePackages"])
@pytest.mark.parametrize("name", ["DerivedData/App-abc", "DerivedData"])
def test_a_derived_data_tree_is_dropped_by_shape(
    scratch_root: Path, upstream: Path, output: str, name: str
) -> None:
    path, clone = _dead_clone(upstream)
    if name != "DerivedData":
        (clone / "DerivedData").mkdir()  # the home stays, emptied
    kept = _snapshot(path)
    _write(clone, f"{name}/info.plist", f"{name}/{output}/x")

    assert _sweep() == 0
    assert _snapshot(_moved(path)) == kept


def test_dependency_clones_in_derived_data_are_not_work(scratch_root: Path, upstream: Path) -> None:
    path, clone = _dead_clone(upstream)
    dd = _derived(clone)
    (dd / "SourcePackages" / "checkouts").mkdir(parents=True)
    _git(dd / "SourcePackages" / "checkouts", "init", "-q", "dep")

    assert _sweep() == 0
    assert not (_moved(path) / "clone" / "DerivedData" / "App-abc").exists()


@pytest.mark.parametrize(
    "content",
    [_shared_offset_bplist(10_000), b"\x00\xff garbage <plist", b""],
    ids=["binary", "garbage", "empty"],
)
def test_the_info_plist_is_never_read(
    scratch_root: Path, upstream: Path, monkeypatch, content: bytes
) -> None:
    # The decision is by name and kind: whatever the file holds, it is not opened.
    path, clone = _dead_clone(upstream)
    dd = _derived(clone)
    (dd / "info.plist").write_bytes(content)
    os.chmod(dd / "info.plist", 0)
    opened: list[str] = []

    def _guard(real):
        def _watch(target, *args, **kwargs):
            if isinstance(target, (str, bytes, os.PathLike)) and os.fsdecode(target).endswith(
                "info.plist"
            ):
                opened.append(os.fsdecode(target))
                raise AssertionError("info.plist was opened")
            return real(target, *args, **kwargs)

        return _watch

    # Every way Python opens a file by path goes through one of these.
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", _guard(os.open))
            patch.setattr(builtins, "open", _guard(builtins.open))
            patch.setattr(io, "open", _guard(io.open))
            assert sc.native_build_output(dd)
            assert _sweep() == 0
    finally:
        if dd.exists():
            os.chmod(dd / "info.plist", 0o600)
    assert opened == []
    assert not (_moved(path) / "clone" / "DerivedData" / "App-abc").exists()


@pytest.mark.parametrize("name", ["dd", "Xcode-out/App-abc", "DerivedData/App/nested"])
def test_derived_data_shape_outside_a_derived_data_folder_is_kept(
    scratch_root: Path, upstream: Path, name: str
) -> None:
    path, clone = _dead_clone(upstream)
    _write(clone, f"{name}/info.plist", f"{name}/Build/w")
    _kept_whole(path)


@pytest.mark.parametrize(
    "extra",
    ["notes.txt", "Sources/main.swift"],
)
def test_a_derived_data_folder_holding_anything_else_is_kept(
    scratch_root: Path, upstream: Path, extra: str
) -> None:
    path, clone = _dead_clone(upstream)
    dd = _derived(clone)
    _write(dd, extra)

    _kept_whole(path)


def test_a_derived_data_folder_without_info_plist_or_output_is_kept(
    scratch_root: Path, upstream: Path
) -> None:
    path, clone = _dead_clone(upstream)
    _write(clone, "DerivedData/A/Build/w", "DerivedData/B/info.plist")
    _write(clone, "DerivedData/C/Info.plist", "DerivedData/C/Build/w")
    _write(clone, "DerivedData/D/info.plist", "DerivedData/D/build/w")

    _kept_whole(path)


@pytest.mark.parametrize(
    ("entry", "kind"),
    [
        ("Logs", "file"),
        ("ModuleCache.noindex", "file"),
        (".DS_Store", "dir"),
        ("info.plist", "dir"),
    ],
)
def test_an_allowed_entry_of_the_wrong_kind_is_kept(
    scratch_root: Path, upstream: Path, entry: str, kind: str
) -> None:
    path, clone = _dead_clone(upstream)
    _write(clone, "DerivedData/A/Build/w")
    if entry != "info.plist":
        _write(clone, "DerivedData/A/info.plist")
    if kind == "file":
        _write(clone, f"DerivedData/A/{entry}")
    else:
        _write(clone, f"DerivedData/A/{entry}/x")

    _kept_whole(path)


def test_a_linked_derived_data_entry_does_not_count(
    scratch_root: Path, upstream: Path, tmp_path: Path
) -> None:
    # A junction needs no privilege, so this runs on every Windows runner too.
    path, clone = _dead_clone(upstream)
    (tmp_path / "elsewhere").mkdir()
    _write(clone, "DerivedData/A/info.plist")
    make_dir_link(clone / "DerivedData" / "A" / "Build", tmp_path / "elsewhere")

    _kept_whole(path)


def test_a_linked_info_plist_does_not_count(
    scratch_root: Path, upstream: Path, tmp_path: Path
) -> None:
    path, clone = _dead_clone(upstream)
    _write(tmp_path, "real.plist")
    _write(clone, "DerivedData/A/Build/w")
    try:
        (clone / "DerivedData" / "A" / "info.plist").symlink_to(tmp_path / "real.plist")
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable")

    _kept_whole(path)
