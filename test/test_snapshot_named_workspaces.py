"""Named workspace folders (`workspace-<name>/`) ride a snapshot when asked for.

A workspace made with `kirocrew workspace create <name>` keeps its files in
`workspace-<name>/` under the data home. The `named-workspaces` component carries those
folders, but only when `--components` names it: one folder can be gigabytes, and a nightly
backup that took them by default could cross the 4 GiB bound it is under today. Restore
takes its trees from the bundle's own top-level `workspace-<name>/` folders, and only from
a bundle whose manifest declares the component, so an older bundle restores as before.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from conftest import make_dir_link
from kiro_crew import platform_compat
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_archive

NAMED = "named-workspaces"
EVERYTHING = ",".join(snap.COMPONENTS)


def _home(tmp_path, monkeypatch, workspaces: dict | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _setup_fake_kirocrew(home)
    if workspaces is None:
        workspaces = {"proj": {"dir": "workspace-proj"}}
    (home / "config.json").write_text(json.dumps({"workspaces": workspaces}))
    proj = home / "workspace-proj"
    (proj / "src").mkdir(parents=True)
    (proj / "notes.md").write_text("working file\n")
    (proj / "src" / "main.py").write_text("print('hi')\n")
    return home


def _snapshot(tmp_path, *components: str, out: Path | None = None) -> Path:
    out = out or tmp_path / "out"
    argv = [str(out), *(("--components", ",".join(components)) if components else ())]
    assert snap.snapshot_main([*argv, *unpinnable_argv()]) == 0
    return max(out.glob("kirocrew-snapshot-*.tar.gz"))


def _restore(bundle: Path, mode: str, *components: str) -> int:
    argv = [str(bundle), "--mode", mode, "--force"]
    if components:
        argv += ["--components", ",".join(components)]
    return snap.restore_main([*argv, *unpinnable_argv()])


def _names(bundle: Path) -> list[str]:
    with tarfile.open(bundle) as tf:
        return sorted("/".join(Path(m.name).parts[1:]) for m in tf.getmembers() if m.isfile())


def _manifest(bundle: Path) -> dict:
    with tarfile.open(bundle) as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("/MANIFEST.json"))
        return json.loads(tf.extractfile(member).read())


def _crafted(tmp_path: Path, manifest: dict | None, files: dict[str, bytes]) -> Path:
    """A hand-built bundle: `MANIFEST.json` (when given) and *files* under its root."""
    root = "kirocrew-snapshot-20260101T000000Z"
    bundle = tmp_path / "crafted.tar.gz"
    entries = dict(files)
    if manifest is not None:
        entries["MANIFEST.json"] = json.dumps(manifest).encode()
    with tarfile.open(bundle, "w:gz") as tf:
        for rel, data in entries.items():
            info = tarfile.TarInfo(f"{root}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return bundle


def test_the_component_is_declared_opt_in_and_restorable():
    assert snap.NAMED_WORKSPACES == NAMED
    assert NAMED in snap.COMPONENTS
    assert snap.COMPONENTS[NAMED].trees == () and snap.COMPONENTS[NAMED].files == ()
    assert snap.COMPONENTS[NAMED].policy is snap.SecretPolicy.UNRESOLVED
    assert NAMED in snap._OPT_IN_COMPONENTS
    assert NAMED in snap._WHOLE_TREE_COMPONENTS
    assert NAMED not in snap._REPLACE_ONLY_COMPONENTS
    assert NAMED not in snap.resolve_components(None, snap.Purpose.BACKUP)
    doc = (Path(snap.__file__).parent / "docs" / "snapshot-and-restore.md").read_text(
        encoding="utf-8"
    )
    assert f"| {NAMED} |" in doc, doc


@pytest.mark.parametrize(
    ("name", "carried"),
    [
        ("workspace-proj", True),
        ("workspace-a.b c", True),
        ("workspace", False),
        ("workspace-", False),
        ("workspace-. ", False),
        ("workspace-a:b", False),
        ("workspace-a\\b", False),
        ("members", False),
        ("apps", False),
    ],
)
def test_only_a_workspace_prefixed_single_folder_is_a_named_tree(name, carried):
    """The prefix keeps a bundle's tree off every folder the product owns."""
    assert snap.is_named_workspace_tree(name) is carried


def test_a_default_snapshot_leaves_the_folders_out_and_names_them(tmp_path, monkeypatch, capsys):
    """Opt-in: a nightly backup with no `--components` is the size it was before."""
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path)
    out = capsys.readouterr().out
    assert not any(n.startswith("workspace-proj/") for n in _names(bundle))
    assert NAMED not in _manifest(bundle)["components"]
    assert f"proj: {home / 'workspace-proj'}" in out, out


def test_naming_the_component_carries_each_configured_folder(tmp_path, monkeypatch, capsys):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    home = _home(
        tmp_path,
        monkeypatch,
        {
            "proj": {"dir": "workspace-proj"},
            "abs": {"dir": str(tmp_path / "home" / "workspace-abs")},
            "ext": {"dir": str(outside)},
            "deep": {"dir": "projects/workspace-deep"},
        },
    )
    (home / "workspace-abs").mkdir()
    (home / "workspace-abs" / "a.md").write_text("absolute but inside\n")
    # A folder no workspace names is not carried, whatever it is called.
    (home / "workspace-stray").mkdir()
    (home / "workspace-stray" / "s.md").write_text("not configured\n")

    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    out = capsys.readouterr().out
    names = _names(bundle)
    assert "workspace-proj/notes.md" in names and "workspace-proj/src/main.py" in names
    assert "workspace-abs/a.md" in names
    assert not any(n.startswith("workspace-stray/") for n in names)
    assert _manifest(bundle)["components"][NAMED] == "unresolved"
    # Every default component plus the opt-in one is still a complete bundle.
    with tarfile.open(bundle) as tf:
        assert tf.getmembers()[0].name.split("/")[0].startswith("kirocrew-snapshot-")
    # Placed anywhere else, it is still named for copying by hand.
    assert f"ext: {outside}" in out and "deep:" in out, out
    assert "proj:" not in out and "abs:" not in out, out


def test_a_folder_past_the_bound_refuses_only_the_run_that_asked_for_it(
    tmp_path, monkeypatch, capsys
):
    """The archive bound is unchanged: a default run still fits it, and a run that names
    the component is refused whole, with nothing written and nothing pruned."""
    home = _home(tmp_path, monkeypatch)
    out = tmp_path / "out"
    older = _snapshot(tmp_path, out=out)
    defaults = snap.resolve_components(None, snap.Purpose.BACKUP)
    default_bytes, _ = snap._estimate_selected_bytes(home, defaults)
    monkeypatch.setattr(snap, "_MAX_ARCHIVE_BYTES", default_bytes + 64 * 1024)
    (home / "workspace-proj" / "big.bin").write_bytes(b"\0" * (256 * 1024))

    # The default selection still fits. Its own directory: two runs in one second share
    # a bundle name, and `out` must hold only `older` for the prune check below.
    _snapshot(tmp_path, out=tmp_path / "out-default")
    capsys.readouterr()
    rc = snap.snapshot_main(
        [str(out), "--components", EVERYTHING, "--keep", "1", *unpinnable_argv()]
    )
    printed = capsys.readouterr().out
    assert rc == 1, printed
    assert "No bundle was written and nothing was pruned" in printed, printed
    assert older.exists()


def test_replace_round_trips_the_folder_and_leaves_others_alone(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    proj = home / "workspace-proj"
    (proj / "notes.md").write_text("changed after the snapshot\n")
    (proj / "new.md").write_text("made after the snapshot\n")
    (proj / "src" / "main.py").unlink()
    other = home / "workspace-other"
    other.mkdir()
    (other / "keep.md").write_text("not in the bundle\n")

    assert _restore(bundle, "replace") == 0
    assert (proj / "notes.md").read_text() == "working file\n"
    assert (proj / "src" / "main.py").read_text() == "print('hi')\n"
    assert not (proj / "new.md").exists(), "replace swaps the carried folder whole"
    assert (other / "keep.md").read_text() == "not in the bundle\n"
    saved = [p for p in home.glob("pre-restore-*/workspace-proj/new.md")]
    assert saved, "the replaced folder is kept in the pre-restore backup"


def test_a_replace_that_fails_midway_puts_the_folder_back(tmp_path, monkeypatch, capsys):
    """The carried folder is one of the replace's rollback targets."""
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    proj = home / "workspace-proj"
    (proj / "notes.md").write_text("live copy\n")
    real_copytree = snap._copytree_safe
    failed = False

    def fail_on_the_folder(src, dst, **kwargs):
        nonlocal failed
        # Only the install from the bundle; the rollback's own copy back must work.
        if Path(dst) == proj and Path(src).parent.name.startswith("kirocrew-") and not failed:
            failed = True
            raise OSError("disk full")
        return real_copytree(src, dst, **kwargs)

    monkeypatch.setattr(snap, "_copytree_safe", fail_on_the_folder)
    rc = _restore(bundle, "replace", NAMED)
    out = capsys.readouterr().out
    assert rc == 1 and failed, out
    assert "Your previous state was put back" in out, out
    assert (proj / "notes.md").read_text() == "live copy\n"


def test_merge_adds_what_the_folder_is_missing_and_keeps_the_rest(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    proj = home / "workspace-proj"
    (proj / "notes.md").write_text("newer here\n")
    (proj / "src" / "main.py").unlink()

    assert _restore(bundle, "merge", NAMED) == 0
    assert (proj / "notes.md").read_text() == "newer here\n"
    assert (proj / "src" / "main.py").read_text() == "print('hi')\n"


def test_a_restore_onto_a_fresh_home_lands_the_folder_in_the_data_home(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(fresh))
    assert _restore(bundle, "replace", "config", NAMED) == 0
    assert (fresh / "workspace-proj" / "notes.md").read_text() == "working file\n"
    assert (home / "workspace-proj" / "notes.md").exists()


def test_this_installs_workspace_dir_is_kept(tmp_path, monkeypatch):
    """The pointer names a folder on the machine that made the snapshot, so it stays."""
    home = _home(tmp_path, monkeypatch)
    (home / "workspace_dir").write_text(str(tmp_path / "source-root"))
    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    (home / "workspace_dir").write_text(str(tmp_path / "this-root"))
    shutil.rmtree(home / "workspace-proj")

    assert _restore(bundle, "replace") == 0
    assert (home / "workspace_dir").read_text() == str(tmp_path / "this-root")
    assert (home / "workspace-proj" / "notes.md").read_text() == "working file\n"
    assert not (tmp_path / "source-root").exists()


def test_the_bundles_agent_config_in_the_folder_is_held_back(tmp_path, monkeypatch, capsys):
    """A session starts in the folder, so its MCP and hook config is not installed."""
    home = _home(tmp_path, monkeypatch)
    proj = home / "workspace-proj"
    (proj / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "evil"}}}')
    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    (proj / ".mcp.json").write_text('{"mcpServers": {}}')

    assert _restore(bundle, "replace") == 0
    out = capsys.readouterr().out
    assert (proj / ".mcp.json").read_text() == '{"mcpServers": {}}'
    assert "\n     workspace-proj/.mcp.json\n" in out, out

    (proj / ".mcp.json").unlink()
    assert _restore(bundle, "merge", NAMED) == 0
    assert not (proj / ".mcp.json").exists(), "merge must not add the bundle's copy"


def test_an_old_bundle_restores_no_folder(tmp_path, monkeypatch):
    """A bundle that does not declare the component (any bundle written before it, or a
    pre-v3 one with no component map) leaves every live and archived folder alone."""
    home = _home(tmp_path, monkeypatch)
    proj = home / "workspace-proj"
    files = {"workspace-proj/notes.md": b"from the archive\n", "crons.json": b'{"jobs": []}'}
    for manifest in ({"version": 4, "components": {"crons": "unresolved"}}, {"version": 2}):
        bundle = _crafted(tmp_path, manifest, files)
        assert _restore(bundle, "replace") == 0
        assert (proj / "notes.md").read_text() == "working file\n"
        assert _restore(bundle, "merge") == 0
        assert (proj / "notes.md").read_text() == "working file\n"
    # Naming the component does not make an old bundle carry it.
    bundle = _crafted(tmp_path, {"version": 4, "components": {"crons": "unresolved"}}, files)
    assert _restore(bundle, "replace", NAMED) == 1
    assert (proj / "notes.md").read_text() == "working file\n"


def test_a_bundle_tree_without_the_prefix_is_never_restored(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    bundle = _crafted(
        tmp_path,
        {"version": 4, "components": {NAMED: "unresolved"}},
        {"apps/planted.txt": b"x", "workspace-proj/notes.md": b"from the archive\n"},
    )
    assert _restore(bundle, "replace") == 0
    assert not (home / "apps" / "planted.txt").exists()
    assert (home / "workspace-proj" / "notes.md").read_text() == "from the archive\n"


def test_an_unsafe_entry_in_a_carried_folder_refuses_the_restore(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    root = "kirocrew-snapshot-20260101T000000Z"
    bundle = tmp_path / "linked.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        data = json.dumps({"version": 4, "components": {NAMED: "unresolved"}}).encode()
        info = tarfile.TarInfo(f"{root}/MANIFEST.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"{root}/workspace-proj/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    assert _restore(bundle, "replace") == 1
    assert "cannot be extracted safely" in capsys.readouterr().out
    assert (home / "workspace-proj" / "notes.md").read_text() == "working file\n"


class _AsOnWindows:
    """`platform_compat` as `snapshot_archive` sees it on Windows, everything else as is."""

    IS_WINDOWS = True

    def __getattr__(self, name):
        return getattr(platform_compat, name)


def _backslash_link_bundle(tmp_path: Path, component: str, member: str) -> Path:
    """A bundle declaring *component* whose only entry is a link named *member*."""
    root = "kirocrew-snapshot-20260101T000000Z"
    bundle = tmp_path / "backslash.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        data = json.dumps({"version": 4, "components": {component: "unresolved"}}).encode()
        info = tarfile.TarInfo(f"{root}/MANIFEST.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"{root}/{member}")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    return bundle


@pytest.mark.parametrize(
    ("component", "member", "live"),
    [
        (NAMED, "workspace-proj\\notes.md", "workspace-proj/notes.md"),
        (NAMED, "WORKSPACE-PROJ/notes.md", "workspace-proj/notes.md"),
        ("artifacts", "artifacts\\report.md", "artifacts/report.md"),
        ("artifacts", "Artifacts/report.md", "artifacts/report.md"),
    ],
)
def test_a_dropped_entry_spelled_the_windows_way_refuses_the_restore(
    tmp_path, monkeypatch, capsys, component, member, live
):
    """On Windows `\\` is a separator and names compare without case, so a dropped link
    spelled that way is inside the carried tree and the bundle is incomplete there."""
    home = _home(tmp_path, monkeypatch)
    (home / live).parent.mkdir(parents=True, exist_ok=True)
    (home / live).write_text("live copy\n")
    monkeypatch.setattr(snapshot_archive, "platform_compat", _AsOnWindows())
    bundle = _backslash_link_bundle(tmp_path, component, member)

    assert _restore(bundle, "replace") == 1
    assert "cannot be extracted safely" in capsys.readouterr().out
    assert (home / live).read_text() == "live copy\n"


def test_a_live_folder_that_links_outside_is_refused_and_nothing_is_written(
    tmp_path, monkeypatch, capsys
):
    """A restore never writes outside a validated folder of the data home."""
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("not ours\n")
    shutil.rmtree(home / "workspace-proj")
    make_dir_link(home / "workspace-proj", outside)

    for mode in ("replace", "merge"):
        assert _restore(bundle, mode, NAMED) == 1
        assert "do not resolve inside the data home" in capsys.readouterr().out
        assert sorted(p.name for p in outside.iterdir()) == ["notes.md"]
        assert (outside / "notes.md").read_text() == "not ours\n"


def test_a_linked_folder_is_recorded_as_skipped_not_followed(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    outside = tmp_path / "big-disk"
    outside.mkdir()
    (outside / "secret.md").write_text("elsewhere\n")
    shutil.rmtree(home / "workspace-proj")
    make_dir_link(home / "workspace-proj", outside)

    bundle = _snapshot(tmp_path, *snap.COMPONENTS)
    assert not any(n.startswith("workspace-proj/") for n in _names(bundle))
    assert {"reason": snap.SKIP_LINKED_TREE_ROOT, "path": "workspace-proj"} in _manifest(bundle)[
        "skipped"
    ]
