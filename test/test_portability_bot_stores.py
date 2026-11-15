"""An instance import installs each Bot's dashboard, artifact and briefing add-only.

The archive side is built the way an export produces it: a real dashboard package
saved to a real artifact store and put in force through ``instance.apply_package`` on
one data home, then its ``members/`` tree and its ``artifacts/`` tree (as the
archive's ``bot-artifacts/``) copied into an extraction directory under a second home's private import staging. The import side
is :func:`portability_bot_stores.install_bot_stores` against that second home.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
from pathlib import Path, PurePath
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew import pinned_fs
from kiro_crew import portability_bot_stores as stores
from kiro_crew.agent_panel import panel_dir
from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.portability_bot_types import BOT_ARTIFACTS_DIR, ImportedBot, PartOutcome

SLUG = "atlas"
BOT = ImportedBot(
    name="Atlas",
    slug=SLUG,
    member_id=SLUG,
    memory_store="atlas-store",
    generation="1",
    slot_key="dashboard:member:atlas",
    kiro_agent="Atlas",
)
PAGE = '<div><b data-dashboard-field="credits"></b></div>'
PACKAGE_HTML = '<p data-dashboard-field="goal"></p>'


@pytest.fixture(autouse=True)
def _catalog(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {"credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}}},
    }
    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (builtin / "template.html").write_text(PAGE, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)


def _package(bound_to: str = f"crewmate:{SLUG}", html: str = PACKAGE_HTML) -> dict[str, Any]:
    return dp.validate_package(
        {
            "kind": "dashboard",
            "bound_to": bound_to,
            "model": {
                "types": {
                    "goal": {
                        "type": "text",
                        "label": "Goal",
                        "source": {"fold": "work", "path": "conductor.goal"},
                    }
                }
            },
            "view": {"html": html},
            "theme": {"tokens": {}},
        }
    )


class _Source:
    """The exporting home, seeded once per test."""

    def __init__(self, home: Path, mp: pytest.MonkeyPatch, board: str = "atlas board") -> None:
        mp.setenv("KIROCREW_HOME", str(home))
        self.home = data_home()
        store = ArtifactStore(self.home / "artifacts")
        art = store.create(
            name=board,
            content=dp.dump_package(_package()),
            kind=dp.DASHBOARD_KIND,
            source="dashboard",
        )
        loaded = store.get(art.slug)
        content = dp.canonical_package_content(loaded.content or "")
        instance.adopt(SLUG, "fixture-board")
        record = instance.apply_package(
            SLUG, package_slug=art.slug, package_version=int(loaded.version), content=content
        )
        self.package = art.slug
        self.version = int(loaded.version)
        self.digest = record.package_digest
        member = self.home / "members" / SLUG
        (member / "briefing.md").write_text("# Atlas briefing\n", encoding="utf-8")
        (member / "welcome_claimed.json").write_text("{}", encoding="utf-8")
        (member / "activity.jsonl").write_text('{"forged": true}\n', encoding="utf-8")
        (member / "welcome_owed.json").write_text("{}", encoding="utf-8")


class _Import:
    """The importing home, with one extraction directory per import, as the real one."""

    def __init__(self, home: Path, mp: pytest.MonkeyPatch) -> None:
        self.mp = mp
        self.raw_home = home
        self.count = 0

    def snap_from(self, src: _Source) -> Path:
        self.count += 1
        self.mp.setenv("KIROCREW_HOME", str(self.raw_home))
        mc = data_home()
        snap = mc / "import-staging" / f"work{self.count}" / "snap"
        snap.mkdir(parents=True)
        shutil.copytree(src.home / "members", snap / "members")
        shutil.copytree(src.home / "artifacts", snap / BOT_ARTIFACTS_DIR)
        return snap

    @property
    def mc(self) -> Path:
        return data_home()


@pytest.fixture
def world(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> tuple[_Source, _Import]:
    src = _Source(tmp_path / "src-home", _floor_monkeypatch)
    dst = _Import(tmp_path / "dst-home", _floor_monkeypatch)
    return src, dst


def _run(snap: Path, mc: Path, owned_slugs: frozenset[str] | None = None) -> dict[str, PartOutcome]:
    rows = stores.install_bot_stores(snap, mc, [BOT], allow_unpinned=False, owned_slugs=owned_slugs)
    assert [r.part for r in rows] == ["dashboard", "briefing"]
    assert all(r.bot == BOT.name for r in rows)
    return {r.part: r for r in rows}


def _page_snapshots(slug: str = SLUG) -> list[str]:
    directory = panel_dir()
    if not directory.is_dir():
        return []
    return sorted(
        p.name for p in directory.iterdir() if p.name.startswith(f"{slug}.dashboard-page.")
    )


def _version_file(snap: Path, src: _Source) -> Path:
    return snap / BOT_ARTIFACTS_DIR / src.package / "versions" / f"v{src.version}.html"


def _stage_of(snap: Path) -> Path:
    """The one stage directory this import created beside the extraction."""
    [stage] = [p for p in snap.parent.iterdir() if p.name.startswith(f"{stores.STAGE_DIRNAME}-")]
    return stage


def _edit_meta(snap: Path, src: _Source, change: dict[str, Any]) -> None:
    meta = snap / BOT_ARTIFACTS_DIR / src.package / "meta.json"
    raw = json.loads(meta.read_text(encoding="utf-8"))
    raw.update(change)
    meta.write_text(json.dumps(raw), encoding="utf-8")


def test_dashboard_installed_and_page_snapshot_matches_pin(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "restored")
    record = instance.read(SLUG)
    assert record.state != instance.STATE_ERROR
    assert (record.package_slug, record.package_version, record.package_digest) == (
        src.package,
        src.version,
        src.digest,
    )
    pinned = instance.pinned_package(record)
    assert pinned is not None, "the reader serves the pinned page, not the template copy"
    assert instance._page_digest(pinned.content) == src.digest
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]
    assert (dst.mc / "artifacts" / src.package / "versions" / f"v{src.version}.html").is_file()
    member = dst.mc / "members" / SLUG
    assert (member / "briefing.md").read_text(encoding="utf-8") == "# Atlas briefing\n"
    assert (member / "welcome_claimed.json").is_file()


def test_archive_host_state_and_history_are_not_installed(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    (snap / "members" / SLUG / "dashboard" / "preview.json").write_text("{}", encoding="utf-8")
    _run(snap, dst.mc)
    member = dst.mc / "members" / SLUG
    assert not (member / "activity.jsonl").exists()
    assert not (member / "welcome_owed.json").exists()
    assert not (member / "dashboard" / "preview.json").exists()


def test_member_dir_is_created_owner_only(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _run(snap, dst.mc)
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(dst.mc / "members" / SLUG).st_mode)
        assert mode == 0o700


def test_edited_artifact_is_not_rebuilt_into_a_page(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    edited = dp.dump_package(_package(html="<p>an edited page</p>"))
    _version_file(snap, src).write_text(edited, encoding="utf-8")
    (snap / BOT_ARTIFACTS_DIR / src.package / "current.html").write_text(edited, encoding="utf-8")
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "digest_mismatch"
    )
    assert _page_snapshots() == []
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_artifact_bound_to_another_bot_is_not_rebuilt(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _version_file(snap, src).write_text(
        dp.dump_package(_package(bound_to="crewmate:other")), encoding="utf-8"
    )
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"
    )
    assert _page_snapshots() == []
    assert _page_snapshots("other") == []
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_existing_dashboard_is_kept_whole(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    live = dst.mc / "members" / SLUG / "dashboard"
    live.mkdir(parents=True)
    (live / "instance.json").write_bytes(b'{"live": true}')
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "kept")
    assert (live / "instance.json").read_bytes() == b'{"live": true}'
    assert sorted(p.name for p in live.iterdir()) == ["instance.json"]
    assert not (dst.mc / "artifacts" / src.package).exists()
    assert _page_snapshots() == []


def test_existing_artifact_is_not_overwritten(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    live = dst.mc / "artifacts" / src.package
    live.mkdir(parents=True)
    (live / "marker").write_bytes(b"live artifact")
    rows = _run(snap, dst.mc)
    assert sorted(p.name for p in live.iterdir()) == ["marker"]
    assert (live / "marker").read_bytes() == b"live artifact"
    # The artifact here cannot be read, so whose it is is unknown: the record pinning
    # it is not published either (A37), and the reason says it was unreadable (A44).
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "not_restored", "unreadable_record"
    )
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")


def test_existing_briefing_is_kept(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    member = dst.mc / "members" / SLUG
    member.mkdir(parents=True)
    (member / "briefing.md").write_bytes(b"live briefing")
    (member / "welcome_claimed.json").write_bytes(b"live claim")
    rows = _run(snap, dst.mc)
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "kept")
    assert (member / "briefing.md").read_bytes() == b"live briefing"
    assert (member / "welcome_claimed.json").read_bytes() == b"live claim"


def test_copy_failing_on_second_file_publishes_nothing_and_a_retry_installs(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    real_copy = stores._copy_file
    calls: list[Any] = []

    def failing_copy(*args: Any) -> None:
        calls.append(args)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real_copy(*args)

    _floor_monkeypatch.setattr(stores, "_copy_file", failing_copy)
    rows = _run(snap, dst.mc)
    assert len(calls) >= 2
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    assert not (dst.mc / "artifacts" / src.package).exists()
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")
    assert _page_snapshots() == []

    _floor_monkeypatch.setattr(stores, "_copy_file", real_copy)
    again = _run(dst.snap_from(src), dst.mc)
    assert again["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert (dst.mc / "artifacts" / src.package).is_dir()
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]


def test_artifact_copy_failure_publishes_neither_store(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    real_copy = stores._copy_file

    def failing_copy(src_root: Path, src_rel: Any, dst_root: Path, dst_rel: Any) -> None:
        if dst_rel.parts[0] == "artifacts":
            raise OSError(5, "Input/output error")
        real_copy(src_root, src_rel, dst_root, dst_rel)

    _floor_monkeypatch.setattr(stores, "_copy_file", failing_copy)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    assert not (dst.mc / "artifacts" / src.package).exists()
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")


def test_error_state_record_is_unpublished(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    (snap / "members" / SLUG / "dashboard" / "instance.json").write_text(
        json.dumps({"schema": 99}), encoding="utf-8"
    )
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "not_restored", "unreadable_record"
    )
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")
    assert (_stage_of(snap) / stores.ROLLED_BACK_RECORD).is_dir()
    assert not (dst.mc / "artifacts" / src.package).exists()


def test_package_slug_failing_the_store_validator_builds_no_artifact_path(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    record_path = snap / "members" / SLUG / "dashboard" / "instance.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    raw["package"]["slug"] = "../escape"
    record_path.write_text(json.dumps(raw), encoding="utf-8")
    (snap / "escape").mkdir()
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "not_restored", "artifact_rejected"
    )
    assert not (dst.mc / "escape").exists()
    assert not (dst.mc / "artifacts").exists()
    assert _page_snapshots() == []


def test_missing_pinned_version_file_is_artifact_missing(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _version_file(snap, src).unlink()
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_missing"
    )
    assert not (dst.mc / "artifacts" / src.package).exists()


def test_archive_without_bot_stores_reports_none(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    shutil.rmtree(snap / "members")
    rows = _run(snap, dst.mc)
    assert rows == {
        "dashboard": PartOutcome(BOT.name, "dashboard", "none"),
        "briefing": PartOutcome(BOT.name, "briefing", "none"),
    }
    assert not (dst.mc / "members").exists()


@requires_symlinks
def test_linked_archive_dashboard_is_not_followed(
    world: tuple[_Source, _Import], tmp_path: Path
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    outside = tmp_path / "outside-dashboard"
    shutil.move(str(snap / "members" / SLUG / "dashboard"), str(outside))
    os.symlink(outside, snap / "members" / SLUG / "dashboard", target_is_directory=True)
    (snap / "members" / SLUG / "briefing.md").unlink()
    (snap / "members" / SLUG / "welcome_claimed.json").unlink()
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "none")
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")


@requires_symlinks
def test_linked_file_inside_archive_dashboard_is_not_copied(
    world: tuple[_Source, _Import], tmp_path: Path
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    secret = tmp_path / "secret.json"
    secret.write_text('{"secret": true}', encoding="utf-8")
    history = snap / "members" / SLUG / "dashboard" / "history.json"
    history.unlink()
    os.symlink(secret, history)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"].status == "restored"
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard" / "history.json")


def test_unpinnable_platform_without_permission_installs_nothing(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _floor_monkeypatch.setattr(stores.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "not_restored", "write_failed")
    assert not (dst.mc / "members").exists()


HELD = "held_by_other_bot"
OWN_OTHER = "another_package_of_this_bot"
NOT_OWNED: frozenset[str] = frozenset({"someone-else"})

requires_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="the publish fallback runs only where dirs pin"
)


def test_existing_dashboard_of_this_bot_is_kept(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    live = dst.mc / "members" / SLUG / "dashboard"
    live.mkdir(parents=True)
    rows = _run(snap, dst.mc, owned_slugs=frozenset({SLUG}))
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "kept")


def test_existing_dashboard_of_another_bot_is_held_not_kept(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    live = dst.mc / "members" / SLUG / "dashboard"
    live.mkdir(parents=True)
    (live / "instance.json").write_bytes(b'{"other": true}')
    rows = _run(snap, dst.mc, owned_slugs=NOT_OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", HELD)
    assert sorted(p.name for p in live.iterdir()) == ["instance.json"]
    assert (live / "instance.json").read_bytes() == b'{"other": true}'
    assert not (dst.mc / "artifacts" / src.package).exists()
    assert _page_snapshots() == []


def test_existing_briefing_of_another_bot_is_held_and_nothing_is_added(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    member = dst.mc / "members" / SLUG
    member.mkdir(parents=True)
    (member / "briefing.md").write_bytes(b"other briefing")
    rows = _run(snap, dst.mc, owned_slugs=NOT_OWNED)
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "not_restored", HELD)
    assert (member / "briefing.md").read_bytes() == b"other briefing"
    assert not (member / "welcome_claimed.json").exists()


def test_new_stores_install_for_an_unowned_slug(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    rows = _run(snap, dst.mc, owned_slugs=NOT_OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "restored")


def test_existing_artifact_of_another_bot_is_not_rebuilt_into_a_page(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, dst.mc / "artifacts" / src.package)
    rows = _run(snap, dst.mc, owned_slugs=NOT_OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", HELD)
    assert _page_snapshots() == []
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")


def _save_other_package(mc: Path, bound_to: str) -> str:
    """A real dashboard artifact saved on the destination, bound to *bound_to*."""
    store = ArtifactStore(mc / "artifacts")
    return store.create(
        name="another board",
        content=dp.dump_package(_package(bound_to=bound_to, html="<p>here</p>")),
        kind=dp.DASHBOARD_KIND,
        source="dashboard",
    ).slug


def test_an_artifact_here_under_the_pinned_name_bound_elsewhere_holds_the_record(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    other = dst.mc / "artifacts" / src.package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, other)
    # Rebind every version here to another Bot: this Bot's own slug, but not its page.
    for page in [other / "current.html", *(other / "versions").iterdir()]:
        page.write_text(dp.dump_package(_package(bound_to="crewmate:other")), encoding="utf-8")
    before = _tree(other)
    rows = _run(snap, dst.mc, owned_slugs=frozenset({SLUG}))
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", HELD)
    assert _tree(other) == before
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")
    assert _page_snapshots() == []


def test_a_second_package_bound_to_the_bot_is_never_published(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    here = _save_other_package(dst.mc, f"crewmate:{SLUG}")
    assert here != src.package
    rows = _run(snap, dst.mc, owned_slugs=frozenset({SLUG}))
    # The record installs; only its package, which would be a second one bound to
    # the Bot, is not published.
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "page_not_rebuilt", OWN_OTHER)
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert (dst.mc / "members" / SLUG / "dashboard" / "instance.json").is_file()
    found = dp.resolve_bound_slug(f"crewmate:{SLUG}", store=ArtifactStore(dst.mc / "artifacts"))
    assert found == here


def test_a_package_bound_to_another_bot_here_does_not_hold_this_one(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _save_other_package(dst.mc, "crewmate:other")
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")


def test_a_kept_record_brings_back_no_second_bound_package(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    live = dst.mc / "members" / SLUG / "dashboard"
    shutil.copytree(snap / "members" / SLUG / "dashboard", live)
    here = _save_other_package(dst.mc, f"crewmate:{SLUG}")
    rows = _run(snap, dst.mc, owned_slugs=frozenset({SLUG}))
    # The kept record pins a package that is not here and cannot be brought back:
    # the part is not whole, with the refusal as its reason, never a plain "kept".
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "page_not_rebuilt", OWN_OTHER)
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert sorted(p.name for p in (dst.mc / "artifacts").iterdir() if p.is_dir()) == [here]


def test_a_failed_page_write_keeps_the_record_and_its_package(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record and the package it pins are in place and passed the gate; only the
    # page snapshot failed, so both stay and the part names only the page.
    src, dst = world
    snap = dst.snap_from(src)
    _floor_monkeypatch.setattr(stores, "_write_page", lambda _slug, _content: "write_failed")
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "write_failed"
    )
    assert (dst.mc / "members" / SLUG / "dashboard" / "instance.json").is_file()
    assert (dst.mc / "artifacts" / src.package / "meta.json").is_file()
    assert not os.path.lexists(_stage_of(snap) / stores.ROLLED_BACK_RECORD)


def test_a_record_that_does_not_read_back_rolls_back_with_its_package(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _floor_monkeypatch.setattr(stores, "_record_state_ok", lambda _slug: False)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "not_restored", "unreadable_record"
    )
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    stage = _stage_of(snap)
    assert (stage / stores.ROLLED_BACK_RECORD / "instance.json").is_file()
    assert (stage / stores.ROLLED_BACK_PACKAGE / "meta.json").is_file()


def test_a_record_that_stays_published_keeps_its_package_and_names_only_the_page(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # A43: the package moves back only once the record that pins it did. A record
    # rollback that fails leaves both installed, and the part says only the page failed.
    src, dst = world
    snap = dst.snap_from(src)
    real = stores._rename_noreplace

    def record_stays(src_root: Path, src_parts: Any, dst_root: Path, dst_parts: Any) -> None:
        if tuple(dst_parts)[-1] == stores.ROLLED_BACK_RECORD:
            raise OSError(16, "Device or resource busy")
        real(src_root, src_parts, dst_root, dst_parts)

    _floor_monkeypatch.setattr(stores, "_record_state_ok", lambda _slug: False)
    _floor_monkeypatch.setattr(stores, "_rename_noreplace", record_stays)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "unreadable_record"
    )
    assert (dst.mc / "members" / SLUG / "dashboard" / "instance.json").is_file()
    assert (dst.mc / "artifacts" / src.package / "meta.json").is_file()
    assert not os.path.lexists(_stage_of(snap) / stores.ROLLED_BACK_PACKAGE)


def test_a_package_slugged_dashboard_rolls_back_beside_its_record(
    tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # A43: the rollback names are fixed per item, never the package slug, so a
    # package the archive calls "dashboard" cannot collide with the record's.
    src = _Source(tmp_path / "src-home", _floor_monkeypatch, board="dashboard")
    assert src.package == "dashboard"
    dst = _Import(tmp_path / "dst-home", _floor_monkeypatch)
    snap = dst.snap_from(src)
    _floor_monkeypatch.setattr(stores, "_record_state_ok", lambda _slug: False)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "not_restored", "unreadable_record"
    )
    stage = _stage_of(snap)
    assert (stage / stores.ROLLED_BACK_RECORD / "instance.json").is_file()
    assert (stage / stores.ROLLED_BACK_PACKAGE / "meta.json").is_file()
    assert not os.path.lexists(dst.mc / "artifacts" / "dashboard")
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")


def test_an_artifact_here_is_judged_by_its_pinned_version(
    world: tuple[_Source, _Import],
) -> None:
    # A44: the record would serve the PINNED version, so that version's binding
    # decides; a later version bound to this Bot does not make an older one its own.
    src, dst = world
    snap = dst.snap_from(src)
    here = dst.mc / "artifacts" / src.package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, here)
    pinned = here / "versions" / f"v{src.version}.html"
    pinned.write_text(dp.dump_package(_package(bound_to="crewmate:other")), encoding="utf-8")
    store = ArtifactStore(dst.mc / "artifacts")
    token = store.get(src.package, assign_token=True).content_token
    store.update(
        src.package, content=dp.dump_package(_package(html="<p>v2</p>")), expected_token=token
    )
    assert store.get(src.package).version > src.version
    rows = _run(snap, dst.mc, owned_slugs=frozenset({SLUG}))
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", HELD)
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")


def test_the_bound_dashboards_are_listed_once_per_import(
    tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # A44: one scan of the destination's dashboard artifacts serves every Bot.
    src = _Source(tmp_path / "src-home", _floor_monkeypatch)
    dst = _Import(tmp_path / "dst-home", _floor_monkeypatch)
    snap = dst.snap_from(src)
    shutil.copytree(snap / "members" / SLUG, snap / "members" / "bea")
    _save_other_package(dst.mc, "crewmate:other")
    listed: list[str] = []
    real_list = ArtifactStore.list

    def counting(self: ArtifactStore, *args: Any, **kwargs: Any) -> Any:
        listed.append(str(kwargs.get("kind", "")))
        return real_list(self, *args, **kwargs)

    _floor_monkeypatch.setattr(ArtifactStore, "list", counting)
    bea = ImportedBot(
        name="Bea",
        slug="bea",
        member_id="bea",
        memory_store="bea-store",
        generation="1",
        slot_key="dashboard:member:bea",
        kiro_agent="Bea",
    )
    rows = stores.install_bot_stores(snap, dst.mc, [BOT, bea], allow_unpinned=False)
    assert [r.part for r in rows].count("dashboard") == 2
    assert listed == [dp.DASHBOARD_KIND]


def test_an_unreadable_bound_dashboard_leaves_the_binding_unproven(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _save_other_package(dst.mc, "crewmate:other")

    def unreadable(self: ArtifactStore, *args: Any, **kwargs: Any) -> Any:
        raise OSError(5, "Input/output error")

    _floor_monkeypatch.setattr(ArtifactStore, "get", unreadable)
    rows = _run(snap, dst.mc)
    # Only the package waits on the unproven binding: the record installs.
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "unreadable_record"
    )
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert (dst.mc / "members" / SLUG / "dashboard" / "instance.json").is_file()


def test_a_record_publish_that_fails_takes_its_package_back(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    real = stores._rename_noreplace

    def failing(src_root: Path, src_parts: Any, dst_root: Path, dst_parts: Any) -> None:
        if tuple(dst_parts)[-1] == "dashboard":
            raise OSError(28, "No space left on device")
        real(src_root, src_parts, dst_root, dst_parts)

    _floor_monkeypatch.setattr(stores, "_rename_noreplace", failing)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")


def test_existing_artifact_of_this_bot_is_rebuilt_into_a_page(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, dst.mc / "artifacts" / src.package)
    rows = _run(snap, dst.mc, owned_slugs=frozenset({SLUG}))
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]


def _no_atomic_rename(*_args: Any, **_kwargs: Any) -> None:
    raise NotImplementedError("atomic no-replace rename is unavailable")


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@requires_pinned_walk
def test_without_atomic_rename_stores_publish_by_exclusive_mkdir(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _floor_monkeypatch.setattr(stores.platform_compat, "rename_noreplace", _no_atomic_rename)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]
    published = _tree(dst.mc / "artifacts" / src.package)
    assert published == _tree(snap / BOT_ARTIFACTS_DIR / src.package)
    assert "instance.json" in _tree(dst.mc / "members" / SLUG / "dashboard")
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(dst.mc / "members" / SLUG / "dashboard").st_mode)
        assert mode == 0o700


@requires_pinned_walk
def test_without_atomic_rename_a_destination_that_appears_is_kept(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    live = dst.mc / "members" / SLUG / "dashboard"

    def racing(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        # Only the record's publish races; the package before it publishes as usual.
        if Path(dst).name != "dashboard":
            _no_atomic_rename()
        live.mkdir()
        (live / "instance.json").write_bytes(b'{"live": true}')
        raise NotImplementedError("atomic no-replace rename is unavailable")

    _floor_monkeypatch.setattr(stores.platform_compat, "rename_noreplace", racing)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "kept")
    assert sorted(p.name for p in live.iterdir()) == ["instance.json"]
    assert (live / "instance.json").read_bytes() == b'{"live": true}'


@requires_pinned_walk
def test_without_atomic_rename_a_failed_publish_publishes_nothing(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    member = dst.mc / "members" / SLUG
    real_rename = os.rename
    published: list[str] = []

    def failing_rename(a: Any, b: Any, **kwargs: Any) -> None:
        # The descriptor-relative rename parks the stage; the by-path one publishes.
        if "dst_dir_fd" not in kwargs:
            published.append(str(b))
            raise OSError(28, "No space left on device")
        real_rename(a, b, **kwargs)

    _floor_monkeypatch.setattr(stores.platform_compat, "rename_noreplace", _no_atomic_rename)
    _floor_monkeypatch.setattr(stores.os, "rename", failing_rename)
    rows = _run(snap, dst.mc)
    _floor_monkeypatch.setattr(stores.os, "rename", real_rename)
    # The package publishes first; the record is tried after it, and fails the same way.
    artifacts = dst.mc / "artifacts"
    assert published == [str(artifacts / src.package), str(member / "dashboard")]
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    # Neither the mkdir claim nor the parked sibling is left behind.
    assert not os.path.lexists(artifacts / src.package)
    assert not [
        p.name for p in artifacts.iterdir() if p.name.startswith(f".{src.package}.publish-")
    ]
    assert not os.path.lexists(member / "dashboard")


@requires_pinned_walk
def test_a_stage_that_cannot_be_parked_is_write_failed(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stage and the destination are on one filesystem in practice; a rename that
    # still crosses devices fails the part and publishes nothing.
    src, dst = world
    snap = dst.snap_from(src)
    real_rename = os.rename

    def cross_device(a: Any, b: Any, **kwargs: Any) -> None:
        if "dst_dir_fd" in kwargs:
            raise OSError(18, "Invalid cross-device link")
        real_rename(a, b, **kwargs)

    _floor_monkeypatch.setattr(stores.os, "rename", cross_device)
    rows = _run(snap, dst.mc)
    _floor_monkeypatch.setattr(stores.os, "rename", real_rename)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")
    assert (_stage_of(snap) / "dashboard" / "instance.json").is_file()


def test_an_entry_at_the_fixed_stage_name_is_never_installed(
    world: tuple[_Source, _Import],
) -> None:
    # The archive picks the extraction's name; a tree it plants where a stage named
    # after the extraction would be is never part of what gets published.
    src, dst = world
    snap = dst.snap_from(src)
    planted = snap.parent / stores.STAGE_DIRNAME / SLUG
    (planted / "dashboard").mkdir(parents=True)
    (planted / "dashboard" / "forged.json").write_bytes(b"planted")
    (planted / "briefing").mkdir()
    (planted / "briefing" / "welcome_owed.json").write_bytes(b"planted")
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "restored")
    member = dst.mc / "members" / SLUG
    assert not (member / "dashboard" / "forged.json").exists()
    assert not (member / "welcome_owed.json").exists()
    assert (member / "dashboard" / "instance.json").is_file()


def test_an_entry_already_at_the_stage_name_is_write_failed(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _floor_monkeypatch.setattr(stores.secrets, "token_hex", lambda _n: "fixed")
    stage = snap.parent / f"{stores.STAGE_DIRNAME}-fixed"
    (stage / "dashboard").mkdir(parents=True)
    (stage / "dashboard" / "instance.json").write_bytes(b"leftover")
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "not_restored", "write_failed")
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")
    assert not (dst.mc / "members" / SLUG / "briefing.md").exists()


def test_each_stage_is_created_owner_only_under_a_random_name(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _run(snap, dst.mc)
    stage = _stage_of(snap)
    assert len(stage.name) > len(stores.STAGE_DIRNAME) + 16
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(stage).st_mode) == 0o700


@pytest.mark.parametrize(
    "field, value",
    [
        ("source_path", "victim-notes.txt"),
        ("source_root", "/"),
        ("source_copy_only", True),
    ],
)
def test_a_live_pointer_in_meta_refuses_the_artifact_before_publish(
    world: tuple[_Source, _Import], field: str, value: Any
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    if field == "source_path":
        victim = dst.mc / "victim-notes.txt"
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_text("private", encoding="utf-8")
        value = str(victim)
    _edit_meta(snap, src, {field: value})
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"
    )
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert _page_snapshots() == []


def test_every_refused_and_stripped_field_is_an_artifact_field() -> None:
    from kiro_crew.artifact_store.model import Artifact

    refused, stripped = set(stores.REFUSED_META_FIELDS), set(stores.STRIPPED_META_FIELDS)
    assert not refused & stripped
    assert refused | stripped <= set(Artifact.__dataclass_fields__)


REMOTE_TIES: dict[str, Any] = {
    "publication": {"artifact_id": "remote-1", "view_url": "https://example.invalid/a"},
    "fork_metadata": {"upstream_artifact_id": "remote-1"},
    "webapp_metadata": {"target": "somewhere"},
}


@pytest.mark.parametrize("field", sorted(REMOTE_TIES))
def test_a_remote_tie_in_meta_is_stripped_and_the_package_restores(
    world: tuple[_Source, _Import], field: str
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _edit_meta(snap, src, {field: REMOTE_TIES[field]})
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    installed = dst.mc / "artifacts" / src.package
    raw = json.loads((installed / "meta.json").read_text(encoding="utf-8"))
    assert not set(stores.STRIPPED_META_FIELDS) & set(raw)
    assert not [p.name for p in installed.iterdir() if p.name.startswith(".meta.json.")]
    art = ArtifactStore(dst.mc / "artifacts").get(src.package, version=src.version)
    assert art.publication is None and art.fork_metadata is None
    assert art.webapp_metadata is None and art.kind == dp.DASHBOARD_KIND
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]


def test_a_published_package_restores_and_its_page_serves(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _edit_meta(snap, src, {"publication": REMOTE_TIES["publication"]})
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    pinned = instance.pinned_package(instance.read(SLUG))
    assert pinned is not None and pinned.slug == src.package
    assert ArtifactStore(dst.mc / "artifacts").get(src.package).publication is None


def test_a_remote_tie_next_to_a_source_path_is_still_refused(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _edit_meta(
        snap,
        src,
        {"publication": REMOTE_TIES["publication"], "source_path": str(dst.mc / "config.json")},
    )
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"
    )
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert _page_snapshots() == []


@pytest.mark.parametrize(
    "change",
    [
        {"slug": "another-package"},
        {"kind": "html"},
        {"version_kinds": {"1": "html"}},
        {"version": 0},
    ],
    ids=["slug", "kind", "version-kind", "version-range"],
)
def test_a_meta_that_is_not_this_dashboard_package_is_not_published(
    world: tuple[_Source, _Import], change: dict[str, Any]
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    if "version_kinds" in change:
        change = {"version_kinds": {str(src.version): "html"}}
    _edit_meta(snap, src, change)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"].status == "page_not_rebuilt"
    assert rows["dashboard"].reason in ("artifact_rejected", "artifact_missing")
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert _page_snapshots() == []


def test_an_unreadable_meta_is_not_published(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    (snap / BOT_ARTIFACTS_DIR / src.package / "meta.json").write_bytes(b"[1, 2]")
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"
    )
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_a_kept_record_brings_back_its_deleted_artifact(world: tuple[_Source, _Import]) -> None:
    src, dst = world
    first = _run(dst.snap_from(src), dst.mc)
    assert first["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    shutil.rmtree(dst.mc / "artifacts" / src.package)
    rows = _run(dst.snap_from(src), dst.mc, owned_slugs=frozenset({SLUG}))
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    art = ArtifactStore(dst.mc / "artifacts").get(src.package, version=src.version)
    assert art.kind == dp.DASHBOARD_KIND and not art.source_path
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]


def test_a_kept_record_with_its_artifact_present_stays_kept(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    _run(dst.snap_from(src), dst.mc)
    before = _tree(dst.mc / "artifacts" / src.package)
    rows = _run(dst.snap_from(src), dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "kept")
    assert _tree(dst.mc / "artifacts" / src.package) == before


def test_a_kept_record_never_brings_back_a_refused_artifact(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    _run(dst.snap_from(src), dst.mc)
    shutil.rmtree(dst.mc / "artifacts" / src.package)
    snap = dst.snap_from(src)
    _edit_meta(snap, src, {"source_path": str(dst.mc / "config.json")})
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"
    )
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_a_kept_record_of_another_bot_brings_back_nothing(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    _run(dst.snap_from(src), dst.mc)
    shutil.rmtree(dst.mc / "artifacts" / src.package)
    rows = _run(dst.snap_from(src), dst.mc, owned_slugs=NOT_OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", HELD)
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_a_failed_second_briefing_file_removes_the_first(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # briefing.md and welcome_claimed.json install all or nothing (A26).
    src, dst = world
    snap = dst.snap_from(src)
    real_publish = stores._publish_file

    def failing_publish(data: bytes, staged: Path, root: Path, parts: Any) -> tuple[int, int]:
        if parts[-1] == "welcome_claimed.json":
            raise OSError(28, "No space left on device")
        return real_publish(data, staged, root, parts)

    _floor_monkeypatch.setattr(stores, "_publish_file", failing_publish)
    rows = _run(snap, dst.mc)

    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "not_restored", "write_failed")
    member = dst.mc / "members" / SLUG
    assert not os.path.lexists(member / "briefing.md")
    assert not os.path.lexists(member / "welcome_claimed.json")


def test_a_briefing_file_that_fails_to_stage_publishes_neither(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    real_copy = stores._copy_file

    def failing_copy(src_root: Path, src_rel: Any, dst_root: Path, dst_rel: Any) -> None:
        if dst_rel == PurePath("briefing", "welcome_claimed.json"):
            raise OSError(5, "Input/output error")
        real_copy(src_root, src_rel, dst_root, dst_rel)

    _floor_monkeypatch.setattr(stores, "_copy_file", failing_copy)
    rows = _run(snap, dst.mc)

    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "not_restored", "write_failed")
    member = dst.mc / "members" / SLUG
    assert not os.path.lexists(member / "briefing.md")
    assert not os.path.lexists(member / "welcome_claimed.json")


def test_a_removal_never_takes_a_file_that_replaced_the_created_one(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    real_publish = stores._publish_file
    member = dst.mc / "members" / SLUG

    def swap_then_fail(data: bytes, staged: Path, root: Path, parts: Any) -> tuple[int, int]:
        if parts[-1] == "welcome_claimed.json":
            # Someone replaced the briefing this import created before the rollback.
            (member / "briefing.md").unlink()
            (member / "briefing.md").write_bytes(b"theirs")
            raise OSError(28, "No space left on device")
        return real_publish(data, staged, root, parts)

    _floor_monkeypatch.setattr(stores, "_publish_file", swap_then_fail)
    rows = _run(snap, dst.mc)

    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "not_restored", "write_failed")
    assert (member / "briefing.md").read_bytes() == b"theirs"


@pytest.mark.parametrize("hostile", ["hard_link", "too_large"])
def test_a_store_file_the_pinned_copy_refuses_installs_no_store(
    world: tuple[_Source, _Import],
    _floor_monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hostile: str,
) -> None:
    # The stage copy is pinned_fs.copy_file_pinned: a second hard link or a file over
    # the cap copies nothing, and the dashboard is not published.
    src, dst = world
    snap = dst.snap_from(src)
    history = snap / "members" / SLUG / "dashboard" / "history.json"
    assert history.is_file()
    if hostile == "hard_link":
        os.link(history, tmp_path / "second-name")
    else:
        _floor_monkeypatch.setattr(stores, "MAX_FILE_BYTES", history.stat().st_size - 1)

    rows = _run(snap, dst.mc)

    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")
    assert not (dst.mc / "artifacts" / src.package).exists()


def _art_dir(snap: Path, src: _Source) -> Path:
    return snap / BOT_ARTIFACTS_DIR / src.package


def _assert_refused_and_only_the_record_installed(
    rows: dict[str, PartOutcome], dst: _Import, src: _Source
) -> None:
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"
    )
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert _page_snapshots() == []
    assert (dst.mc / "members" / SLUG / "dashboard" / "instance.json").is_file()


FALSY_VALUES: list[Any] = [0, False, [], {}, None, "", 0.0]


@pytest.mark.parametrize(
    "field, value",
    [
        (field, value)
        for field in stores.REFUSED_META_FIELDS
        for value in FALSY_VALUES
        if not (type(value) is type(stores.REFUSED_META_EMPTY[field]))
    ],
)
def test_a_refused_field_holding_any_falsy_non_empty_value_refuses_the_artifact(
    world: tuple[_Source, _Import], field: str, value: Any
) -> None:
    # records.decode_meta stringifies any present value: "source_path": 0 reads as "0".
    src, dst = world
    snap = dst.snap_from(src)
    _edit_meta(snap, src, {field: value})
    _assert_refused_and_only_the_record_installed(_run(snap, dst.mc), dst, src)


@pytest.mark.parametrize("field", stores.REFUSED_META_FIELDS)
def test_a_refused_field_holding_the_store_empty_value_restores(
    world: tuple[_Source, _Import], field: str
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _edit_meta(snap, src, {field: stores.REFUSED_META_EMPTY[field]})
    assert _run(snap, dst.mc)["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")


def test_the_store_writes_exactly_the_empty_values_the_gate_admits(
    world: tuple[_Source, _Import],
) -> None:
    src, _dst = world
    raw = json.loads((src.home / "artifacts" / src.package / "meta.json").read_text("utf-8"))
    assert {name: raw[name] for name in stores.REFUSED_META_FIELDS} == stores.REFUSED_META_EMPTY
    assert all(
        type(raw[name]) is type(stores.REFUSED_META_EMPTY[name])
        for name in stores.REFUSED_META_FIELDS
    )


def _crafted_v2(snap: Path, src: _Source, variant: str) -> None:
    """Pin a valid v1 and add a version 2 the gate must read, as *variant* shapes it."""
    art = _art_dir(snap, src)
    own = dp.dump_package(_package(html="<p>a later page</p>"))
    pages = {
        "spec": "<script>arbitrary</script>",
        "html-kind": own,
        "not-a-package": "<p>no package</p>",
        "another-bot": dp.dump_package(_package(bound_to="crewmate:vega")),
        "current-differs": own,
        "no-v2-file": "",
    }
    change: dict[str, Any] = {"version": 2}
    if variant in ("spec", "html-kind"):
        change["version_kinds"] = {"2": "html"}
    _edit_meta(snap, src, change)
    if variant != "no-v2-file":
        (art / "versions" / "v2.html").write_text(pages[variant], encoding="utf-8")
    current = "<p>something else</p>" if variant == "current-differs" else pages[variant] or own
    (art / "current.html").write_text(current, encoding="utf-8")


#: A later version bound to another Bot (``"another-bot"``) gives the package to that Bot
#: in the archive itself: ``test_portability_bot_stores_owner`` pins what it does.
CRAFTED_V2 = ["spec", "html-kind", "not-a-package", "current-differs", "no-v2-file"]


@pytest.mark.parametrize("variant", CRAFTED_V2)
def test_a_package_pinning_a_valid_v1_with_a_crafted_later_version_is_refused(
    world: tuple[_Source, _Import], variant: str
) -> None:
    src, dst = world
    assert src.version == 1
    snap = dst.snap_from(src)
    _crafted_v2(snap, src, variant)
    _assert_refused_and_only_the_record_installed(_run(snap, dst.mc), dst, src)


@pytest.mark.parametrize("variant", CRAFTED_V2)
def test_a_kept_record_never_brings_back_a_crafted_later_version(
    world: tuple[_Source, _Import], variant: str
) -> None:
    src, dst = world
    _run(dst.snap_from(src), dst.mc)
    shutil.rmtree(dst.mc / "artifacts" / src.package)
    before = _page_snapshots()
    snap = dst.snap_from(src)
    _crafted_v2(snap, src, variant)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"
    )
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert _page_snapshots() == before


@pytest.mark.parametrize(
    "stray",
    [
        "notes.txt",
        "extra/page.html",
        "versions/v01.html",
        "versions/v2.html",
        "versions/v0.html",
        "versions/notes.txt",
        "current.html.old",
    ],
)
def test_a_stray_file_in_the_package_refuses_it(world: tuple[_Source, _Import], stray: str) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    target = _art_dir(snap, src).joinpath(*stray.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_version_file(snap, src).read_text("utf-8"), encoding="utf-8")
    _assert_refused_and_only_the_record_installed(_run(snap, dst.mc), dst, src)


def test_a_stray_file_refuses_the_kept_records_artifact_too(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    _run(dst.snap_from(src), dst.mc)
    shutil.rmtree(dst.mc / "artifacts" / src.package)
    snap = dst.snap_from(src)
    (_art_dir(snap, src) / "notes.txt").write_text("planted", encoding="utf-8")
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"
    )
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_the_comment_thread_is_not_installed_and_the_package_restores(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    (_art_dir(snap, src) / "comments.json").write_text("[]", encoding="utf-8")
    assert _run(snap, dst.mc)["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    installed = dst.mc / "artifacts" / src.package
    assert sorted(_tree(installed)) == ["current.html", "meta.json", "versions/v1.html"]


@pytest.mark.parametrize("pin", ["latest", "earlier"])
def test_a_multi_version_dashboard_of_this_bot_restores(
    world: tuple[_Source, _Import], pin: str
) -> None:
    src, dst = world
    store = ArtifactStore(src.home / "artifacts")
    updated = store.update(
        src.package,
        content=dp.dump_package(_package(html="<p>a later page</p>")),
        snapshot=True,
        expected_token=store.get(src.package).content_token,
    )
    assert int(updated.version) == 2
    if pin == "latest":
        loaded = store.get(src.package)
        record = instance.apply_package(
            SLUG,
            package_slug=src.package,
            package_version=2,
            content=dp.canonical_package_content(loaded.content or ""),
        )
        src.version, src.digest = 2, record.package_digest
    snap = dst.snap_from(src)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    installed = dst.mc / "artifacts" / src.package
    assert sorted(_tree(installed)) == [
        "current.html",
        "meta.json",
        "versions/v1.html",
        "versions/v2.html",
    ]
    art = ArtifactStore(dst.mc / "artifacts").get(src.package, version=src.version)
    assert art.kind == dp.DASHBOARD_KIND and not art.source_path
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]


def test_rewriting_a_staged_meta_goes_through_the_shared_pinned_writer(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _edit_meta(snap, src, {"publication": REMOTE_TIES["publication"]})
    calls: list[str] = []
    real = stores.atomic_write_at

    def recording(dir_fd: int, name: str, content: str, **kwargs: Any) -> None:
        calls.append(name)
        assert kwargs == {"fsync": True, "mode": 0o600}
        real(dir_fd, name, content, **kwargs)

    _floor_monkeypatch.setattr(stores, "atomic_write_at", recording)
    assert _run(snap, dst.mc)["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert calls == (["meta.json"] if pinned_fs.supports_pinned_walk() else [])


def test_a_briefing_publish_goes_through_the_shared_exclusive_create(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    created: list[str] = []
    real = pinned_fs.write_new_file_pinned

    def recording(dir_fd: int, name: str, data: bytes, **kwargs: Any) -> None:
        created.append(name)
        assert kwargs == {"mode": 0o600, "fsync": True}
        real(dir_fd, name, data, **kwargs)

    _floor_monkeypatch.setattr(pinned_fs, "write_new_file_pinned", recording)
    assert _run(snap, dst.mc)["briefing"] == PartOutcome(BOT.name, "briefing", "restored")
    if pinned_fs.supports_pinned_walk():
        assert sorted(created) == sorted(stores.BRIEFING_FILES)


@contextlib.contextmanager
def _unpinned(*_args: Any, **_kwargs: Any) -> Any:
    yield None


def test_an_unpinnable_publish_creates_by_the_shared_copy_and_never_overwrites(
    tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    _floor_monkeypatch.setattr(stores, "_pinned_dir", _unpinned)
    staged = tmp_path / "stage" / "briefing.md"
    staged.parent.mkdir()
    staged.write_bytes(b"# briefing\n")
    root = tmp_path / "home"
    (root / "members").mkdir(parents=True)
    ident = stores._publish_file(b"# briefing\n", staged, root, ("members", "briefing.md"))
    out = root / "members" / "briefing.md"
    st = os.stat(out, follow_symlinks=False)
    assert (st.st_dev, st.st_ino) == ident and out.read_bytes() == b"# briefing\n"
    if os.name == "posix":
        assert stat.S_IMODE(st.st_mode) == 0o600
    out.write_bytes(b"theirs")
    with pytest.raises(FileExistsError):
        stores._publish_file(b"# briefing\n", staged, root, ("members", "briefing.md"))
    assert out.read_bytes() == b"theirs"


def test_an_unpinnable_publish_that_fails_leaves_nothing(
    tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    _floor_monkeypatch.setattr(stores, "_pinned_dir", _unpinned)
    staged = tmp_path / "stage" / "briefing.md"
    staged.parent.mkdir()
    staged.write_bytes(b"x" * 16)
    _floor_monkeypatch.setattr(stores, "MAX_FILE_BYTES", 4)
    root = tmp_path / "home"
    (root / "members").mkdir(parents=True)
    with pytest.raises(ValueError):
        stores._publish_file(b"x" * 16, staged, root, ("members", "briefing.md"))
    assert not os.path.lexists(root / "members" / "briefing.md")
