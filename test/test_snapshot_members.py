"""Each Bot's own folder, `members/<id>/`, rides a snapshot.

A Bot's definition rides in `config`, its memory in `memory` and its team in
`crew-teams`, but its briefing and its Dashboard tab live in `members/<id>/`. Without a
component for that tree a restored host has the Bots and none of their briefings or
dashboards, and nothing says so. The component is `--mode replace` only, like the
artifact library: a Bot's folder is keyed by a slug two machines can share for
different Bots, so a file-by-file merge would mix them.
"""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from conftest import make_dir_link
from kiro_crew import pinned_fs, platform_compat
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_components
from kiro_crew.eventlog.service import LEGACY_MIGRATED_SUFFIX
from kiro_crew.members_parts.member_space import (
    ACTIVITY_FILE_NAME,
    BRIEFING_FILE_NAME,
    MEMBERS_DIR_NAME,
)


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _setup_fake_kirocrew(home)
    bot = home / MEMBERS_DIR_NAME / "naruto"
    (bot / "dashboard").mkdir(parents=True)
    (bot / BRIEFING_FILE_NAME).write_text("# priorities\nship the fix\n")
    (bot / "dashboard" / "index.html").write_text("<h1>naruto</h1>")
    return home


def _snapshot(tmp_path, *components: str) -> Path:
    out = tmp_path / "out"
    argv = [str(out), *(("--components", ",".join(components)) if components else ())]
    assert snap.snapshot_main([*argv, *unpinnable_argv()]) == 0
    return next(out.glob("kirocrew-snapshot-*.tar.gz"))


def _restore(bundle: Path, mode: str, *components: str) -> int:
    argv = [str(bundle), "--mode", mode, "--force"]
    if components:
        argv += ["--components", ",".join(components)]
    return snap.restore_main([*argv, *unpinnable_argv()])


def _members_in(bundle: Path) -> list[str]:
    with tarfile.open(bundle) as tf:
        return sorted(
            "/".join(Path(m.name).parts[1:])
            for m in tf.getmembers()
            if m.isfile() and Path(m.name).parts[1:2] == (MEMBERS_DIR_NAME,)
        )


def test_the_component_claims_the_members_tree():
    spec = snap.COMPONENTS["members"]
    assert spec.trees == (MEMBERS_DIR_NAME,), "the literal must track member_space"
    assert spec.files == ()
    assert spec.policy is snap.SecretPolicy.UNRESOLVED
    assert "members" in snap._WHOLE_TREE_COMPONENTS
    assert "members" in snap._REPLACE_ONLY_COMPONENTS


def test_the_documented_table_names_it():
    doc = (Path(snap.__file__).parent / "docs" / "snapshot-and-restore.md").read_text(
        encoding="utf-8"
    )
    assert "| members |" in doc, doc
    assert "Named workspace folders do not ride" in doc, "the gap must be documented"


def test_a_full_snapshot_carries_each_bots_folder(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path)
    assert _members_in(bundle) == [
        "members/naruto/briefing.md",
        "members/naruto/dashboard/index.html",
    ]
    with tarfile.open(bundle) as tf:
        manifest = next(m for m in tf.getmembers() if m.name.endswith("/MANIFEST.json"))
        data = json.loads(tf.extractfile(manifest).read())
    assert data["components"]["members"] == "unresolved"


def test_the_legacy_activity_files_never_ride(tmp_path, monkeypatch):
    """The event log imports these as trusted rows until a fenced marker under
    `crew-log/` says the fold is done. The marker does not ride, and the folder is
    agent-writable, so carrying them would let a restore forge trusted history."""
    home = _home(tmp_path, monkeypatch)
    bot = home / MEMBERS_DIR_NAME / "naruto"
    retired = ACTIVITY_FILE_NAME + LEGACY_MIGRATED_SUFFIX
    legacy = {ACTIVITY_FILE_NAME, ACTIVITY_FILE_NAME + ".1", retired, retired + ".1"}
    for name in legacy:
        assert snapshot_components._is_host_local(("members", "naruto", name)), name
    for name in sorted(legacy):
        (bot / name).write_text('{"kind": "forged"}\n')
    # Only the folder-level position is excluded, not the name anywhere in the tree.
    (bot / "dashboard" / "activity.jsonl").write_text("dashboard data\n")
    bundle = _snapshot(tmp_path, "members")
    assert _members_in(bundle) == [
        "members/naruto/briefing.md",
        "members/naruto/dashboard/activity.jsonl",
        "members/naruto/dashboard/index.html",
    ]


def test_a_crafted_bundle_cannot_land_a_legacy_activity_file(tmp_path, monkeypatch):
    """Restore drops the same names from a bundle it did not write, including spellings
    a case-insensitive or Win32 filesystem would land on the real file."""
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path, "members")
    crafted = tmp_path / "crafted"
    with tarfile.open(bundle) as tf:
        tf.extractall(crafted, filter="data")
    root = next(p for p in crafted.iterdir() if p.is_dir())
    planted = ["activity.jsonl", "Activity.JSONL.1", "activity.jsonl.migrated."]
    for name in planted:
        (root / MEMBERS_DIR_NAME / "naruto" / name).write_text('{"kind": "forged"}\n')
    forged = tmp_path / "forged.tar.gz"
    with tarfile.open(forged, "w:gz") as tf:
        tf.add(root, arcname=root.name)

    assert _restore(forged, "replace", "members") == 0
    bot = home / MEMBERS_DIR_NAME / "naruto"
    assert (bot / BRIEFING_FILE_NAME).is_file()
    left = sorted(p.name for p in bot.iterdir())
    assert not [n for n in left if n.casefold().startswith("activity.jsonl")], left


def test_restore_strips_on_disk_what_the_name_screen_let_through(tmp_path, monkeypatch):
    """With the extraction name screen switched off, only `restore_main`'s on-disk strip
    stands between a planted legacy file and the live Bot folder."""
    from kiro_crew import snapshot_archive

    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path, "members")
    crafted = tmp_path / "crafted"
    with tarfile.open(bundle) as tf:
        tf.extractall(crafted, filter="data")
    root = next(p for p in crafted.iterdir() if p.is_dir())
    (root / MEMBERS_DIR_NAME / "naruto" / "activity.jsonl").write_text("forged\n")
    forged = tmp_path / "forged.tar.gz"
    with tarfile.open(forged, "w:gz") as tf:
        tf.add(root, arcname=root.name)
    monkeypatch.setattr(snapshot_archive, "_never_ships", lambda _name: False)

    assert _restore(forged, "replace", "members") == 0
    bot = home / MEMBERS_DIR_NAME / "naruto"
    assert (bot / BRIEFING_FILE_NAME).is_file()
    assert not (bot / "activity.jsonl").exists()


def test_a_respelled_members_folder_is_matched_too():
    """A case-insensitive or Win32 filesystem lands `Members/` and `members./` on the
    live `members/`, so the folder is matched as loosely as the file name."""
    for folder in ("members", "Members", "MEMBERS", "members.", "members ", "members::$X"):
        assert snapshot_components._is_host_local((folder, "naruto", "activity.jsonl"))
    # NTFS writes a `::$DATA` stream name to the file itself.
    assert snapshot_components._is_host_local(("members", "naruto", "activity.jsonl::$DATA"))
    assert not snapshot_components._is_host_local(("members", "naruto", "briefing.md"))
    assert not snapshot_components._is_host_local(("workspace", "naruto", "activity.jsonl"))


def test_the_on_disk_strip_matches_whatever_name_reached_the_file(tmp_path):
    """The pinned strip reads the extracted folders, so a spelling the entry-name screen
    misses (a Windows `\\` separator, a stream suffix) is caught once the filesystem has
    turned it into a real file. It does not descend trees that cannot hold a match."""
    from kiro_crew.portability_vetting import _strip_host_local_store_state

    snap_root = tmp_path / "work" / "bundle-root"
    bot = snap_root / "Members" / "naruto"
    bot.mkdir(parents=True)
    (bot / "Activity.JSONL").write_text("forged\n")
    (bot / "activity.jsonl.migrated.1").write_text("forged\n")
    (bot / BRIEFING_FILE_NAME).write_text("kept\n")
    (bot / "drafts").mkdir()
    (bot / "drafts" / "activity.jsonl").write_text("a Bot's own draft\n")
    (snap_root / "workspace").mkdir()
    (snap_root / "workspace" / "activity.jsonl").write_text("not a Bot folder\n")

    _strip_host_local_store_state(snap_root)
    assert sorted(p.name for p in bot.iterdir()) == [BRIEFING_FILE_NAME, "drafts"]
    assert (bot / "drafts" / "activity.jsonl").is_file()
    assert (snap_root / "workspace" / "activity.jsonl").is_file()


def test_a_respelled_memory_stores_cannot_plant_the_signing_key(tmp_path):
    """On a case-insensitive filesystem `Memory_Stores/` IS the live `memory_stores/`, so
    the on-disk strip removes the member signing key under any spelling of the folder."""
    from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
    from kiro_crew.portability_vetting import _strip_host_local_store_state

    for folder in ("Memory_Stores", "MEMORY_STORES.", MEMORY_STORES_DIR_NAME):
        assert snapshot_components._is_host_local((folder, ".member-api-key")), folder
    snap_root = tmp_path / "work" / "bundle-root"
    stores = snap_root / "Memory_Stores"
    (stores / "notes").mkdir(parents=True)
    (stores / ".member-api-key").write_text("planted\n")
    _strip_host_local_store_state(snap_root)
    assert not (stores / ".member-api-key").exists()
    assert (stores / "notes").is_dir()


def test_the_dashboard_import_drops_legacy_activity_too(tmp_path, monkeypatch):
    """The dashboard's zip import replaces through `_do_replace`, which installs
    `members/`, so a hand-built archive carrying the legacy files is swept there too."""
    from kiro_crew.portability import apply_import_zip, create_export_zip

    home = _home(tmp_path, monkeypatch)
    exported = zipfile.ZipFile(io.BytesIO(create_export_zip()[0]))
    root = exported.namelist()[0].split("/", 1)[0]
    crafted = tmp_path / "crafted.zip"
    with zipfile.ZipFile(crafted, "w") as out:
        for item in exported.infolist():
            out.writestr(item, exported.read(item))
        out.writestr(f"{root}/members/naruto/{BRIEFING_FILE_NAME}", "imported\n")
        out.writestr(f"{root}/members/naruto/activity.jsonl", '{"kind": "forged"}\n')
    bot = home / MEMBERS_DIR_NAME / "naruto"

    apply_import_zip(crafted, mode="replace")
    assert not (bot / "activity.jsonl").exists()
    assert (bot / BRIEFING_FILE_NAME).read_text() == "imported\n"


def test_a_home_with_no_bots_still_snapshots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _setup_fake_kirocrew(home)
    bundle = _snapshot(tmp_path, "members")
    assert _members_in(bundle) == []


def test_replace_puts_the_bots_folder_back_whole(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path)
    bot = home / MEMBERS_DIR_NAME / "naruto"
    (bot / BRIEFING_FILE_NAME).write_text("overwritten")
    (bot / "stray.txt").write_text("not in the bundle")

    assert _restore(bundle, "replace", "members") == 0
    assert (bot / BRIEFING_FILE_NAME).read_text() == "# priorities\nship the fix\n"
    assert (bot / "dashboard" / "index.html").read_text() == "<h1>naruto</h1>"
    assert not (bot / "stray.txt").exists(), "replace swaps the tree whole"


def test_merge_skips_the_bots_folders_by_name(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path)
    briefing = home / MEMBERS_DIR_NAME / "naruto" / BRIEFING_FILE_NAME
    briefing.write_text("mine")

    assert _restore(bundle, "merge") == 0
    out = capsys.readouterr().out
    assert "members: SKIPPED" in out, out
    assert briefing.read_text() == "mine", "a skipped component is left untouched"


def test_a_merge_of_only_the_bots_folders_is_refused(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path)
    assert _restore(bundle, "merge", "members") != 0
    assert "--mode replace" in capsys.readouterr().out


def test_the_snapshot_names_each_named_workspace_it_leaves_out(tmp_path, monkeypatch, capsys):
    """A named workspace's folder is config-driven, so no component carries it
    yet. The snapshot has to say so, with the path to copy, rather than restore a
    Bot whose working files silently stayed behind."""
    home = _home(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    workspaces = {
        "default": {"dir": "workspace"},
        "inside": {"dir": "workspace/sub"},
        "proj": {"dir": "workspace-proj"},
        "ext": {"dir": str(outside)},
    }
    (home / "config.json").write_text(json.dumps({"workspaces": workspaces}))
    (home / "workspace-proj").mkdir()
    (home / "workspace-proj" / "notes.md").write_text("working file\n")

    bundle = _snapshot(tmp_path)
    out = capsys.readouterr().out
    assert "Named workspaces are not in this snapshot" in out, out
    assert f"proj: {home / 'workspace-proj'}" in out, out
    assert f"ext: {outside}" in out, out
    # A workspace inside a tree a component carries is in the bundle, so it is not named.
    assert "default:" not in out and "inside:" not in out, out
    with tarfile.open(bundle) as tf:
        assert not any("workspace-proj" in m.name for m in tf.getmembers())


def test_the_notice_names_a_workspace_the_selection_leaves_out(tmp_path, monkeypatch, capsys):
    """`--components members` does not carry `workspace/`, so a workspace there is named."""
    home = _home(tmp_path, monkeypatch)
    (home / "config.json").write_text(json.dumps({"workspaces": {"proj": {"dir": "workspace"}}}))
    _snapshot(tmp_path, "members")
    assert f"proj: {home / 'workspace'}" in capsys.readouterr().out


def test_a_workspace_dir_that_cannot_place_does_not_end_the_snapshot(tmp_path, monkeypatch, capsys):
    """`Path("~nosuchuser/p").expanduser()` raises on Python 3.12. The notice runs after
    the bundle is written and before `--keep` prunes, so it must name the workspace by its
    raw `dir` and let retention run, or every nightly run would pile up bundles."""
    home = _home(tmp_path, monkeypatch)
    out = tmp_path / "out"
    older = out / "kirocrew-snapshot-20200101T000000Z.tar.gz"
    out.mkdir()
    older.write_bytes(b"")
    gone = "~kirocrew-no-such-user/project"
    (home / "config.json").write_text(json.dumps({"workspaces": {"gone": {"dir": gone}}}))
    # Windows guesses a profile folder for an unknown `~user` instead of raising, so the
    # raise is made explicit to hold the same path on every platform.
    from kiro_crew.config import loader

    def _cannot_place(_entry):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(loader, "workspace_dir_from_entry", _cannot_place)

    rc = snap.snapshot_main([str(out), "--keep", "1", *unpinnable_argv()])
    printed = capsys.readouterr().out
    assert rc == 0, printed
    assert f"gone: {gone}" in printed, printed
    assert not older.exists(), "retention must still run"
    assert len(list(out.glob("kirocrew-snapshot-*.tar.gz"))) == 1


def test_no_notice_when_every_workspace_rides(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    _snapshot(tmp_path)
    assert "Named workspaces" not in capsys.readouterr().out


def _link_members_outside(home: Path, tmp_path: Path) -> Path:
    """`members/` as a link to a folder outside the data home, which `member_folder`
    supports (``members_is_link``)."""
    target = tmp_path / "bots-elsewhere"
    (target / "naruto").mkdir(parents=True)
    (target / "naruto" / BRIEFING_FILE_NAME).write_text("linked\n")
    real = home / MEMBERS_DIR_NAME
    if real.exists():
        import shutil

        shutil.rmtree(real)
    make_dir_link(real, target)
    return target


def test_a_linked_members_root_is_skipped_not_refused(tmp_path, monkeypatch, capsys):
    """A default snapshot on a host whose `members/` is a link is still written; the link
    is recorded as skipped and never followed."""
    home = _home(tmp_path, monkeypatch)
    _link_members_outside(home, tmp_path)
    bundle = _snapshot(tmp_path)
    assert _members_in(bundle) == []
    with tarfile.open(bundle) as tf:
        manifest = next(m for m in tf.getmembers() if m.name.endswith("/MANIFEST.json"))
        data = json.loads(tf.extractfile(manifest).read())
    assert {"reason": snap.SKIP_LINKED_TREE_ROOT, "path": MEMBERS_DIR_NAME} in data[
        "skipped"
    ], data["skipped"]
    assert pinned_fs.omits_wanted_data(snap.SKIP_LINKED_TREE_ROOT)


def test_retention_keeps_the_last_bundle_that_holds_the_bots_folders(tmp_path, monkeypatch):
    """Once `members/` becomes a link, a `--keep 1` run must not prune the last bundle
    that still carries every Bot's folder, and `--strict` reports the gap."""
    home = _home(tmp_path, monkeypatch)
    out = tmp_path / "out"
    assert snap.snapshot_main([str(out), "--keep", "1", *unpinnable_argv()]) == 0
    (full,) = list(out.glob("kirocrew-snapshot-*.tar.gz"))
    assert _members_in(full), "the first bundle must carry the folders"
    full.rename(out / "kirocrew-snapshot-20200101T000000Z.tar.gz")
    _link_members_outside(home, tmp_path)

    rc = snap.snapshot_main([str(out), "--keep", "1", "--strict", *unpinnable_argv()])
    assert rc == snap.EXIT_INCOMPLETE
    assert (out / "kirocrew-snapshot-20200101T000000Z.tar.gz").exists()


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_a_linked_members_root_does_not_refuse_a_restore_that_leaves_it_alone(
    tmp_path, monkeypatch, mode
):
    """A default restore of a bundle without `members/` neither refuses nor touches a
    linked live `members/`. The bundle comes from a home with no Bots, so the snapshot
    side is not what this exercises."""
    import shutil

    home = _home(tmp_path, monkeypatch)
    shutil.rmtree(home / MEMBERS_DIR_NAME)
    bundle = _snapshot(tmp_path)
    _link_members_outside(home, tmp_path)
    (home / "workspace" / "memory" / "preferences.md").write_text("changed\n")

    assert snap.restore_main([str(bundle), "--mode", mode, "--force", *unpinnable_argv()]) == 0
    assert platform_compat.is_link_or_junction(
        home / MEMBERS_DIR_NAME
    ), "the link must be left as it was"
    assert (home / MEMBERS_DIR_NAME / "naruto" / BRIEFING_FILE_NAME).read_text() == "linked\n"


def test_a_replace_carrying_members_refuses_a_linked_root_and_changes_nothing(
    tmp_path, monkeypatch
):
    """Installing `members/` would write through the link, so that one run refuses up
    front and the live link and its folder are left as they were."""
    home = _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path)
    target = _link_members_outside(home, tmp_path)

    assert _restore(bundle, "replace", "members") != 0
    assert platform_compat.is_link_or_junction(home / MEMBERS_DIR_NAME)
    assert (target / "naruto" / BRIEFING_FILE_NAME).read_text() == "linked\n"
