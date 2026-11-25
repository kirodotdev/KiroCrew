"""An import brings back the page of every retained dashboard version it can prove.

A Bot's dashboard keeps up to ``instance.MAX_RETAINED_VERSIONS`` earlier versions, and
a rollback to one serves the page snapshot its pin names. The snapshots are never
carried, so the import rebuilds each missing one from the carried package (the carried
pin's, ``portability_bot_types.carried_pin``), through the same ownership check and gate
as the current page, and names the Bot (``older_pages_not_restored``) when some could
not be rebuilt. A record that runs its template carries the package its newest retained
versions pin; the import publishes it add-only after the record is in place, and a
refusal there never changes the record's own outcome.

The stores-level tests drive ``install_bot_stores`` with the fixtures of
``test_portability_bot_stores``; the round-trip tests export and import real Bots, and
read the stores step's notices through a recording wrapper as well as the summary row.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from portability_bot_helpers import (
    Homes,
    MadeBot,
    apply_dashboard,
    create_bot,
    dashboard_package,
    export_archive,
    install_fixture_catalog,
    rewrite_archive,
    tree_bytes,
)
from test_portability_bot_stores import (  # noqa: F401 - _catalog is an autouse fixture
    BOT,
    HELD,
    SLUG,
    UNPINNED_OK,
    _catalog,
    _Import,
    _package,
    _page_snapshots,
    _Source,
)

from kiro_crew import portability_bot_stores as stores
from kiro_crew import portability_bot_types
from kiro_crew.agent_panel import panel_dir
from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard_templates import instance
from kiro_crew.portability import apply_import_zip
from kiro_crew.portability_bot_types import BOT_ARTIFACTS_DIR, PartOutcome

OLDER = "older_pages_not_restored"


@pytest.fixture
def world(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> tuple[_Source, _Import]:
    return _Source(tmp_path / "src-home", _floor_monkeypatch), _Import(
        tmp_path / "dst-home", _floor_monkeypatch
    )


@pytest.fixture
def homes(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Homes:
    install_fixture_catalog(tmp_path / "catalog", _floor_monkeypatch)
    return Homes(tmp_path / "homes", _floor_monkeypatch)


@pytest.fixture
def store_notices(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Every notice the stores step raised during an import, by Bot name.

    The stores step is called with a notices dict of its own whatever the caller
    passes, and what it fills is handed on to the caller's, so this proves the stores
    behaviour without depending on how the import merges it into a row.
    """
    seen: dict[str, list[str]] = {}
    real = stores.install_bot_stores

    def spy(*args: Any, notices: dict[str, list[str]] | None = None, **kwargs: Any) -> Any:
        mine: dict[str, list[str]] = {}
        rows = real(*args, notices=mine, **kwargs)
        for name, raised in mine.items():
            seen.setdefault(name, []).extend(raised)
            if notices is not None:
                notices.setdefault(name, []).extend(raised)
        return rows

    monkeypatch.setattr(stores, "install_bot_stores", spy)
    return seen


def _install(
    snap: Path, mc: Path, owned: frozenset[str] | None = None
) -> tuple[dict[str, PartOutcome], dict[str, list[str]]]:
    notices: dict[str, list[str]] = {}
    rows = stores.install_bot_stores(
        snap, mc, [BOT], allow_unpinned=UNPINNED_OK, owned_slugs=owned, notices=notices
    )
    return {r.part: r for r in rows}, notices


def _versions_dir(snap: Path) -> Path:
    return snap / "bot-members" / SLUG / "dashboard" / "versions"


def _edit_version(snap: Path, n: int, change: Callable[[dict[str, Any]], None]) -> None:
    path = _versions_dir(snap) / f"{n}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _second_version(src: _Source, html: str) -> tuple[int, str]:
    """Save a second version of the source's package and put it in force: (version, digest)."""
    store = ArtifactStore(src.home / "artifacts")
    token = store.get(src.package, assign_token=True).content_token
    store.update(src.package, content=dp.dump_package(_package(html=html)), expected_token=token)
    loaded = store.get(src.package)
    record = instance.apply_package(
        SLUG,
        package_slug=src.package,
        package_version=int(loaded.version),
        content=dp.canonical_package_content(loaded.content or ""),
    )
    return int(loaded.version), str(record.package_digest)


def _page_name(digest: str, slug: str = SLUG) -> str:
    return f"{slug}.dashboard-page.{digest}.json"


def test_a_retained_pin_of_another_package_is_named_not_rebuilt(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    other = {"slug": "beta-board", "version": 1, "digest": "f" * 32}
    _edit_version(snap, 1, lambda doc: doc.update(package=other))
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert notices == {BOT.name: [OLDER]}
    assert _page_snapshots() == [_page_name(src.digest)]


def test_a_retained_pin_whose_page_fails_the_gate_writes_no_snapshot(
    world: tuple[_Source, _Import],
) -> None:
    # The current pin passes, so the retained pages are judged; the retained one names
    # the carried package's own version with a digest its page does not hash to.
    src, dst = world
    snap = dst.snap_from(src)
    forged = {"slug": src.package, "version": src.version, "digest": "0" * 32}
    _edit_version(snap, 1, lambda doc: doc.update(package=forged))
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert notices == {BOT.name: [OLDER]}
    assert _page_snapshots() == [_page_name(src.digest)]


def _on_template(src: _Source) -> None:
    """Roll the source's record back to its template version (E8): the record pins nothing."""
    back = instance.rollback(SLUG, 1)
    assert not back.package_slug


def test_a_template_bot_whose_carried_package_is_not_in_the_archive_is_named(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    _on_template(src)
    snap = dst.snap_from(src)
    shutil.rmtree(snap / BOT_ARTIFACTS_DIR)
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert notices == {BOT.name: [OLDER]}
    assert _page_snapshots() == []
    assert not (dst.mc / "artifacts" / src.package).exists()


def test_a_template_bot_gets_its_carried_package_and_its_pages_back(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    _on_template(src)
    snap = dst.snap_from(src)
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert notices == {}
    assert (dst.mc / "artifacts" / src.package / "versions" / "v1.html").is_file()
    assert _page_snapshots() == [_page_name(src.digest)]
    back = instance.rollback(SLUG, 2)
    assert instance.pinned_package(back) is not None


def test_a_template_bot_new_here_never_takes_a_package_already_here(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    _on_template(src)
    snap = dst.snap_from(src)
    here = dst.mc / "artifacts" / src.package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, here)
    before = tree_bytes(here)
    rows, notices = _install(snap, dst.mc, owned=frozenset())
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert notices == {BOT.name: [OLDER]}
    assert tree_bytes(here) == before
    assert _page_snapshots() == []


def test_a_pinned_record_new_here_still_refuses_a_package_already_here(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, dst.mc / "artifacts" / src.package)
    rows, notices = _install(snap, dst.mc, owned=frozenset())
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", HELD)
    assert notices == {}


def _record_doc(src: _Source, n: int) -> dict[str, Any]:
    path = src.home / "members" / SLUG / "dashboard" / "versions" / f"{n}.json"
    return dict(json.loads(path.read_text(encoding="utf-8")))


def test_the_retained_pages_read_only_the_newest_versions_and_skip_an_unreadable_one(
    world: tuple[_Source, _Import], monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    second, second_digest = _second_version(src, '<p data-dashboard-field="goal">two</p>')
    template, first, latest = _record_doc(src, 1), _record_doc(src, 2), _record_doc(src, 3)
    snap = dst.snap_from(src)
    dash = snap / "bot-members" / SLUG / "dashboard"
    shutil.rmtree(dash)
    (dash / "versions").mkdir(parents=True)
    (dash / "instance.json").write_text(json.dumps(template), encoding="utf-8")
    for n in range(1, 199):
        (dash / "versions" / f"{n}.json").write_text(json.dumps(first), encoding="utf-8")
    (dash / "versions" / "199.json").write_text(json.dumps(latest), encoding="utf-8")
    (dash / "versions" / "200.json").write_text("{not json", encoding="utf-8")
    reads: list[int] = []
    pins: list[Any] = []
    real_read, real_pin = instance._read_version, portability_bot_types.carried_pin

    def read(slug: str, version: int) -> dict[str, Any]:
        reads.append(version)
        return real_read(slug, version)

    def pin(record: Any, versions: Any) -> Any:
        pins.append(real_pin(record, versions))
        return pins[-1]

    monkeypatch.setattr(instance, "_read_version", read)
    monkeypatch.setattr(portability_bot_types, "carried_pin", pin)
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    installed = sorted(
        int(p.name[: -len(".json")])
        for p in (dst.mc / "members" / SLUG / "dashboard" / "versions").iterdir()
    )
    assert installed == list(range(181, 200))
    assert sorted(reads) == list(range(181, 200))
    assert pins == [(src.package, second, second_digest)]
    assert _page_snapshots() == sorted([_page_name(src.digest), _page_name(second_digest)])
    assert notices == {}


def test_a_kept_record_with_more_than_twenty_versions_reads_only_the_newest(
    world: tuple[_Source, _Import], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A previous release's import could install more version files than the source
    # keeps; the kept path reads only the newest MAX_RETAINED_VERSIONS of them.
    src, dst = world
    snap = dst.snap_from(src)
    kept = dst.mc / "members" / SLUG / "dashboard"
    shutil.copytree(snap / "bot-members" / SLUG / "dashboard", kept)
    pinned = (kept / "versions" / "2.json").read_bytes()
    for n in range(3, 26):
        (kept / "versions" / f"{n}.json").write_bytes(pinned)
    reads: list[int] = []
    real_read = instance._read_version

    def read(slug: str, version: int) -> dict[str, Any]:
        reads.append(version)
        return real_read(slug, version)

    monkeypatch.setattr(instance, "_read_version", read)
    rows, notices = _install(snap, dst.mc, owned=frozenset({SLUG}))
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert sorted(reads) == list(range(6, 26))
    assert notices == {}


def test_a_template_record_with_no_versions_folder_is_restored(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    template = _record_doc(src, 1)
    snap = dst.snap_from(src)
    member = snap / "bot-members" / SLUG
    shutil.rmtree(member)
    (member / "dashboard").mkdir(parents=True)
    (member / "dashboard" / "instance.json").write_text(json.dumps(template), encoding="utf-8")
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "none")
    assert notices == {}


def test_the_retained_version_docs_never_raise_on_a_listing_that_fails(
    world: tuple[_Source, _Import], monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _dst = world

    def refused(_slug: str) -> list[str]:
        raise instance.InstanceError("the versions folder is a link")

    assert stores._retained_version_docs("nobody-here") == ({}, 0)
    monkeypatch.setattr(instance, "_list_versions", refused)
    assert stores._retained_version_docs(SLUG) == ({}, 1)


# Round trips: real Bots exported and imported.


def _row(summary: dict[str, Any], name: str) -> dict[str, Any]:
    [row] = [r for r in summary.get("bots", []) if r["bot"] == name]
    return dict(row)


def _drop_pages(slug: str) -> None:
    for page in panel_dir().glob(f"{slug}.dashboard-page.*.json"):
        page.unlink()


def _rebind(package: str, bound_to: str) -> None:
    """Rebind *package* the ordinary way: same layout, so no new version is written."""
    store = ArtifactStore(data_home() / "artifacts")
    before = store.get(package, assign_token=True)
    raw = dp._decode_package(before.content or "")
    store.update(
        package,
        content=dp.dump_package(dashboard_package(bound_to.split(":", 1)[1], raw["view"]["html"])),
        expected_token=before.content_token,
    )
    assert store.get(package).version == before.version


def _apply_existing(bot: MadeBot, package: str) -> str:
    """Put the live version of *package* in force for *bot*; the digest it pins."""
    store = ArtifactStore(data_home() / "artifacts")
    loaded = store.get(package)
    if instance.read(bot.slug).instance_version == 0:
        instance.adopt(bot.slug, "fixture-board")
    record = instance.apply_package(
        bot.slug,
        package_slug=package,
        package_version=int(loaded.version),
        content=dp.canonical_package_content(loaded.content or ""),
    )
    return str(record.package_digest)


def _without(archive: Path, dst: Path, name: str, slug: str) -> Path:
    """*archive* as if it had been exported without Bot *name*."""

    def drop_bot(config: dict[str, Any]) -> dict[str, Any]:
        config["agents"].pop(name)
        return config

    return rewrite_archive(
        archive,
        dst,
        drop=lambda rel: rel.startswith(f"bot-members/{slug}/"),
        edit={"config.json": drop_bot},
    )


@pytest.mark.parametrize("mode", ["replace", "self_restore"])
def test_a_template_bot_whose_carried_package_was_rebound_imports_restored_with_the_notice(
    homes: Homes, tmp_path: Path, store_notices: dict[str, list[str]], mode: str
) -> None:
    homes.use("a")
    atlas = create_bot("Atlas")
    applied = apply_dashboard(atlas, '<p data-dashboard-field="goal">atlas page</p>')
    instance.rollback(atlas.slug, 1)
    other = create_bot("Other")
    _rebind(applied.package, f"crewmate:{other.slug}")
    _apply_existing(other, applied.package)
    archive = export_archive(tmp_path / "a.zip")
    alone = _without(archive, tmp_path / "alone.zip", "Atlas", atlas.slug)
    if mode == "replace":
        homes.use("b")
        summary = apply_import_zip(archive, mode="replace")
        homes.use("c")
        baseline = apply_import_zip(alone, mode="replace")
        homes.use("b")
    else:
        _drop_pages(atlas.slug)
        summary = apply_import_zip(archive, mode="merge")
        baseline = apply_import_zip(alone, mode="merge")
    row = _row(summary, "Atlas")
    assert row["dashboard"] == ("restored" if mode == "replace" else "kept")
    assert not row["part_reasons"].get("dashboard")
    assert store_notices.get("Atlas") == [OLDER]
    assert not list(panel_dir().glob(f"{atlas.slug}.dashboard-page.{applied.digest}.json"))
    assert _row(summary, "Other") == _row(baseline, "Other")
    assert row["notices"] == [OLDER]


def test_a_retained_pin_of_a_rebound_package_here_is_named_not_rebuilt(
    homes: Homes, tmp_path: Path, store_notices: dict[str, list[str]]
) -> None:
    homes.use("a")
    atlas = create_bot("Atlas")
    first = apply_dashboard(atlas, '<p data-dashboard-field="goal">first board</p>')
    _rebind(first.package, "crewmate:other")
    store = ArtifactStore(data_home() / "artifacts")
    second = store.create(
        name="atlas second board",
        content=dp.dump_package(dashboard_package(atlas.slug, "<p>second board</p>")),
        kind=dp.DASHBOARD_KIND,
        source="dashboard",
    )
    _apply_existing(atlas, second.slug)
    archive = export_archive(tmp_path / "a.zip")
    _drop_pages(atlas.slug)
    summary = apply_import_zip(archive, mode="merge")
    row = _row(summary, "Atlas")
    assert row["dashboard"] == "restored"
    assert store_notices.get("Atlas") == [OLDER]
    assert not list(panel_dir().glob(f"{atlas.slug}.dashboard-page.{first.digest}.json"))
    assert row["notices"] == [OLDER]


def test_a_kept_template_record_whose_package_came_back_is_restored(
    homes: Homes, tmp_path: Path, store_notices: dict[str, list[str]]
) -> None:
    homes.use("a")
    atlas = create_bot("Atlas")
    applied = apply_dashboard(atlas, '<p data-dashboard-field="goal">board</p>')
    instance.rollback(atlas.slug, 1)
    archive = export_archive(tmp_path / "a.zip")
    shutil.rmtree(data_home() / "artifacts" / applied.package)
    summary = apply_import_zip(archive, mode="merge")
    row = _row(summary, "Atlas")
    assert row["dashboard"] == "restored"
    assert (data_home() / "artifacts" / applied.package / "meta.json").is_file()
    assert store_notices == {}
    assert row["notices"] == []
