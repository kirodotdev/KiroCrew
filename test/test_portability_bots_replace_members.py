"""An instance Replace never installs an archive's ``members/`` tree.

``portability_bots.INSTANCE_REPLACE_COMPONENTS`` leaves the ``members`` component out of
``_do_replace``, as it leaves out ``artifacts`` and ``uploads``: every Bot folder here
stays byte-for-byte, and a Bot's folder files arrive only from ``bot-members/<slug>/``
through the checked Bot stores. No export writes a ``members/`` tree, so the archive
here is hand-built: a real export with ``members/`` members added to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from portability_bot_helpers import (
    Homes,
    apply_dashboard,
    archive_members,
    create_bot,
    export_archive,
    install_fixture_catalog,
    rewrite_archive,
    tree_bytes,
)

from kiro_crew.config.paths import data_home
from kiro_crew.portability import apply_import_zip

ATLAS_PAGE = '<p data-dashboard-field="goal">atlas</p>'


@pytest.fixture
def homes(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Homes:
    install_fixture_catalog(tmp_path / "catalog", _floor_monkeypatch)
    return Homes(tmp_path / "homes", _floor_monkeypatch)


def test_a_replace_never_installs_an_archives_members_tree(homes: Homes, tmp_path: Path) -> None:
    homes.use("a")
    atlas = create_bot("Atlas")
    apply_dashboard(atlas, ATLAS_PAGE)
    archive = export_archive(tmp_path / "a.zip")
    assert not any(name.startswith("members/") for name in archive_members(archive))

    homes.use("b")
    zed = create_bot("Zed")
    mc = data_home()
    zed_folder = mc / "members" / zed.slug
    zed_folder.mkdir(parents=True, exist_ok=True)
    (zed_folder / "briefing.md").write_text("Zed's own\n", encoding="utf-8")
    # Live files of a Bot the archive names, left by an earlier Bot of that slug.
    atlas_folder = mc / "members" / atlas.slug
    atlas_folder.mkdir(parents=True, exist_ok=True)
    (atlas_folder / "activity.jsonl").write_text('{"kind": "here"}\n', encoding="utf-8")
    (atlas_folder / "welcome_owed.json").write_text('{"owed": true}\n', encoding="utf-8")
    before = tree_bytes(mc / "members")

    crafted = rewrite_archive(
        archive,
        tmp_path / "crafted.zip",
        add={
            f"members/{atlas.slug}/briefing.md": b"from a members tree\n",
            f"members/{atlas.slug}/activity.jsonl": b'{"kind": "forged"}\n',
            f"members/{atlas.slug}/welcome_owed.json": b'{"owed": false}\n',
            f"members/{zed.slug}/briefing.md": b"forged for Zed\n",
        },
    )
    summary = apply_import_zip(crafted, mode="replace")

    after = tree_bytes(mc / "members")
    zed_before = {k: v for k, v in before.items() if k.startswith(f"{zed.slug}/")}
    assert {k: v for k, v in after.items() if k.startswith(f"{zed.slug}/")} == zed_before
    assert (zed_folder / "briefing.md").read_text(encoding="utf-8") == "Zed's own\n"
    for live in ("activity.jsonl", "welcome_owed.json"):
        assert after[f"{atlas.slug}/{live}"] == before[f"{atlas.slug}/{live}"], live
    assert not (atlas_folder / "briefing.md").exists()
    # Atlas's dashboard came from bot-members/, through the Bot stores.
    assert (atlas_folder / "dashboard" / "instance.json").is_file()
    [row] = [r for r in summary["bots"] if r["bot"] == "Atlas"]
    assert row["dashboard"] == "restored", row
