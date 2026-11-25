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
import logging
import os
import shutil
from collections.abc import Callable, Iterator
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

from conftest import make_dir_link
from kiro_crew import portability_bot_stores as stores
from kiro_crew import portability_bot_types
from kiro_crew.agent_panel import panel_dir
from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifact_store.model import ArtifactError
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


def test_the_export_and_the_import_choose_the_same_newest_version_names() -> None:
    from kiro_crew import portability_bot_export

    canonical = [f"{n}.json" for n in range(1, instance.MAX_RETAINED_VERSIONS + 2)]
    newest = set(canonical[-instance.MAX_RETAINED_VERSIONS :])
    names = canonical + ["07.json", "00.json", f"0{instance.MAX_RETAINED_VERSIONS + 1}.json"]
    assert portability_bot_export._newest_version_names(names) == newest
    keys = [("versions", name) for name in names] + [("instance.json",)]
    assert stores._newest_versions(keys) == {("versions", name) for name in newest}


@pytest.mark.parametrize(("name", "is_version"), [("7.json", True), ("07.json", False)])
def test_a_version_file_with_a_leading_zero_is_neither_installed_nor_read_for_the_pin(
    world: tuple[_Source, _Import], name: str, is_version: bool
) -> None:
    # Version 7 is read from "7.json" only, so "07.json" is no version on the import's
    # side either: not installed, not read for the carried pin, and not counted unread,
    # just as the export neither archives it nor reads it.
    src, dst = world
    template, pinned = _record_doc(src, 1), _record_doc(src, 2)
    snap = dst.snap_from(src)
    dash = snap / "bot-members" / SLUG / "dashboard"
    shutil.rmtree(dash)
    (dash / "versions").mkdir(parents=True)
    (dash / "instance.json").write_text(json.dumps(template), encoding="utf-8")
    (dash / "versions" / "1.json").write_text(json.dumps(template), encoding="utf-8")
    (dash / "versions" / name).write_text(json.dumps(pinned), encoding="utf-8")
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    installed = dst.mc / "members" / SLUG / "dashboard" / "versions"
    expected = ["1.json", name] if is_version else ["1.json"]
    assert sorted(p.name for p in installed.iterdir()) == expected
    assert (dst.mc / "artifacts" / src.package).is_dir() is is_version
    assert notices == {}
    # A kept record's versions folder holding the same name reads the same way.
    (installed / name).write_text(json.dumps(pinned), encoding="utf-8")
    docs, unread = stores._retained_version_docs(SLUG)
    assert (sorted(docs), unread) == ([1, 7] if is_version else [1], 0)


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


def _template_bot_with_briefing(homes: Homes) -> tuple[MadeBot, str]:
    """Atlas on home a, rolled back to its template with a briefing; its package slug."""
    homes.use("a")
    atlas = create_bot("Atlas")
    applied = apply_dashboard(atlas, '<p data-dashboard-field="goal">board</p>')
    instance.rollback(atlas.slug, 1)
    (data_home() / "members" / atlas.slug / "briefing.md").write_text("brief\n", encoding="utf-8")
    return atlas, applied.package


def _refuse_artifact_root() -> list[str]:
    """Move the data home's ``artifacts/`` under its sensitive ``diag/`` and link it back.

    ``ArtifactStore(<home>/artifacts)`` then refuses its root (``ArtifactError``), while
    every name under ``artifacts/`` still resolves. Returns what ``artifacts/`` lists.
    """
    mc = data_home()
    (mc / "diag").mkdir(exist_ok=True)
    os.replace(mc / "artifacts", mc / "diag" / "arts")
    make_dir_link(mc / "artifacts", mc / "diag" / "arts")
    with pytest.raises(ArtifactError):
        ArtifactStore(mc / "artifacts")
    return sorted(p.name for p in (mc / "artifacts").iterdir())


def _store_refusals(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        rec
        for rec in caplog.records
        if rec.name == stores.__name__
        and rec.levelno == logging.WARNING
        and rec.exc_info
        and isinstance(rec.exc_info[1], ArtifactError)
    ]


def test_a_template_record_installed_where_the_store_refuses_its_root_is_restored(
    homes: Homes,
    tmp_path: Path,
    store_notices: dict[str, list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    atlas, package = _template_bot_with_briefing(homes)
    archive = export_archive(tmp_path / "a.zip")
    homes.use("b")
    first = _row(apply_import_zip(archive, mode="replace"), "Atlas")
    assert (first["dashboard"], first["briefing"]) == ("restored", "restored")
    mc = data_home()
    assert (mc / "artifacts" / package / "meta.json").is_file()
    shutil.rmtree(mc / "members" / atlas.slug)
    _drop_pages(atlas.slug)
    listed = _refuse_artifact_root()
    store_notices.clear()
    caplog.set_level(logging.WARNING, logger=stores.__name__)
    row = _row(apply_import_zip(archive, mode="replace"), "Atlas")
    assert (row["dashboard"], row["briefing"]) == ("restored", "restored")
    assert not row["part_reasons"].get("dashboard") and not row["part_reasons"].get("briefing")
    member = mc / "members" / atlas.slug
    assert (member / "dashboard" / "instance.json").is_file()
    assert (member / "briefing.md").read_text(encoding="utf-8") == "brief\n"
    assert store_notices.get("Atlas") == [OLDER]
    assert row["notices"] == [OLDER]
    assert sorted(p.name for p in (mc / "artifacts").iterdir()) == listed
    assert not list(panel_dir().glob(f"{atlas.slug}.dashboard-page.*.json"))
    assert _store_refusals(caplog)


def test_a_kept_template_record_where_the_store_refuses_its_root_stays_kept(
    homes: Homes,
    tmp_path: Path,
    store_notices: dict[str, list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    atlas, _package = _template_bot_with_briefing(homes)
    archive = export_archive(tmp_path / "a.zip")
    mc = data_home()
    (mc / "members" / atlas.slug / "briefing.md").unlink()
    _drop_pages(atlas.slug)
    listed = _refuse_artifact_root()
    caplog.set_level(logging.WARNING, logger=stores.__name__)
    row = _row(apply_import_zip(archive, mode="merge"), "Atlas")
    assert (row["dashboard"], row["briefing"]) == ("kept", "restored")
    assert not row["part_reasons"].get("dashboard") and not row["part_reasons"].get("briefing")
    assert (mc / "members" / atlas.slug / "briefing.md").read_text(encoding="utf-8") == "brief\n"
    assert store_notices.get("Atlas") == [OLDER]
    assert row["notices"] == [OLDER]
    assert sorted(p.name for p in (mc / "artifacts").iterdir()) == listed
    assert not list(panel_dir().glob(f"{atlas.slug}.dashboard-page.*.json"))
    assert _store_refusals(caplog)


# Archive and destination bytes that would make a reader raise instead of refusing.


class _Tracebacks(logging.Handler):
    """Every record the stores step logged with a traceback."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.seen: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info:
            self.seen.append(record)


@pytest.fixture
def tracebacks() -> Iterator[list[logging.LogRecord]]:
    handler = _Tracebacks()
    stores_logger = logging.getLogger(stores.__name__)
    stores_logger.addHandler(handler)
    try:
        yield handler.seen
    finally:
        stores_logger.removeHandler(handler)


#: A JSON document nested past the decoder's recursion limit.
DEEP = "[" * 200_000 + "]" * 200_000


@pytest.mark.parametrize("odd", [None, "².json"])
def test_a_kept_record_with_a_non_ascii_digit_version_name_imports_as_without_it(
    world: tuple[_Source, _Import], odd: str | None
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    kept = dst.mc / "members" / SLUG / "dashboard"
    shutil.copytree(snap / "bot-members" / SLUG / "dashboard", kept)
    if odd:
        (kept / "versions" / odd).write_bytes((kept / "versions" / "1.json").read_bytes())
    rows, notices = _install(snap, dst.mc, owned=frozenset({SLUG}))
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "restored")
    assert (dst.mc / "members" / SLUG / "briefing.md").is_file()
    assert notices == {}
    assert sorted(stores._retained_version_docs(SLUG)[0]) == [1, 2]


@pytest.mark.parametrize(
    ("on_template", "dashboard", "notice"),
    [
        (True, PartOutcome(BOT.name, "dashboard", "restored"), {BOT.name: [OLDER]}),
        (
            False,
            PartOutcome(BOT.name, "dashboard", "page_not_rebuilt", "artifact_rejected"),
            {},
        ),
    ],
)
def test_a_carried_package_whose_meta_nests_too_deep_is_refused_by_the_gate(
    world: tuple[_Source, _Import],
    tracebacks: list[logging.LogRecord],
    on_template: bool,
    dashboard: PartOutcome,
    notice: dict[str, list[str]],
) -> None:
    src, dst = world
    if on_template:
        _on_template(src)
    snap = dst.snap_from(src)
    (snap / BOT_ARTIFACTS_DIR / src.package / "meta.json").write_text(DEEP, encoding="utf-8")
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == dashboard
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "restored")
    assert (dst.mc / "members" / SLUG / "dashboard" / "instance.json").is_file()
    assert (dst.mc / "members" / SLUG / "briefing.md").is_file()
    assert not (dst.mc / "artifacts" / src.package).exists()
    assert notices == notice
    assert tracebacks == []


def test_a_record_that_nests_too_deep_is_an_unreadable_record(
    world: tuple[_Source, _Import], tracebacks: list[logging.LogRecord]
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    (snap / "bot-members" / SLUG / "dashboard" / "instance.json").write_text(DEEP, encoding="utf-8")
    rows, notices = _install(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "not_restored", "unreadable_record"
    )
    assert rows["briefing"] == PartOutcome(BOT.name, "briefing", "restored")
    assert not (dst.mc / "members" / SLUG / "dashboard").exists()
    assert notices == {}
    assert tracebacks == []
