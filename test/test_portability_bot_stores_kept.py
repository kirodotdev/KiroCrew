"""A Bot's dashboard record the destination keeps, and the member path it lives under.

The same real dashboard package and import home as ``test_portability_bot_stores``
(its fixtures and helpers are reused): a record this install already has keeps its
own pin, and the import brings back only what that pin is missing -- the page
snapshot, or the package -- or says why it could not.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from test_portability_bot_stores import (  # noqa: F401 - _catalog is an autouse fixture
    BOT,
    HELD,
    SLUG,
    _catalog,
    _Import,
    _package,
    _page_snapshots,
    _run,
    _save_other_package,
    _Source,
)

from kiro_crew import portability_bot_stores as stores
from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.portability_bot_types import BOT_ARTIFACTS_DIR, PartOutcome

OWNED = frozenset({SLUG})


@pytest.fixture
def world(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> tuple[_Source, _Import]:
    return _Source(tmp_path / "src-home", _floor_monkeypatch), _Import(
        tmp_path / "dst-home", _floor_monkeypatch
    )


def _keep_record_and_package(snap: Path, mc: Path, package: str) -> None:
    """Put this Bot's record and the package it pins in place, with no page snapshot."""
    shutil.copytree(snap / "members" / SLUG / "dashboard", mc / "members" / SLUG / "dashboard")
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / package, mc / "artifacts" / package)


def test_a_non_directory_at_the_member_path_is_a_failed_write(
    world: tuple[_Source, _Import],
) -> None:
    # A file at members/<slug> is no record: the dashboard was not written, and
    # the package published before it moves back.
    src, dst = world
    snap = dst.snap_from(src)
    (dst.mc / "members").mkdir(parents=True, exist_ok=True)
    (dst.mc / "members" / SLUG).write_text("not a directory", encoding="utf-8")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", "write_failed")
    assert (dst.mc / "members" / SLUG).is_file()
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_a_kept_record_rebuilds_its_missing_page_from_its_own_package(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record_and_package(snap, dst.mc, src.package)
    assert _page_snapshots() == []
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]
    # With the page in place nothing is missing: a second import keeps it.
    again = _run(dst.snap_from(src), dst.mc, owned_slugs=OWNED)
    assert again["dashboard"] == PartOutcome(BOT.name, "dashboard", "kept")


def test_a_kept_record_whose_page_cannot_be_written_names_the_page(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record_and_package(snap, dst.mc, src.package)
    _floor_monkeypatch.setattr(stores, "_write_page", lambda _slug, _content: "write_failed")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "write_failed"
    )
    assert (dst.mc / "members" / SLUG / "dashboard" / "instance.json").is_file()


def test_a_kept_record_never_rebuilds_a_page_from_a_package_bound_elsewhere(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record_and_package(snap, dst.mc, src.package)
    pinned = dst.mc / "artifacts" / src.package / "versions" / f"v{src.version}.html"
    pinned.write_text(dp.dump_package(_package(bound_to="crewmate:other")), encoding="utf-8")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "page_not_rebuilt", HELD)
    assert _page_snapshots() == []


def test_a_kept_record_whose_package_is_refused_by_the_index_is_not_whole(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # artifacts/<P> is missing and the scan of the bound dashboards could not finish:
    # the package is not brought back, so the part is not whole and says why.
    src, dst = world
    snap = dst.snap_from(src)
    shutil.copytree(snap / "members" / SLUG / "dashboard", dst.mc / "members" / SLUG / "dashboard")
    _floor_monkeypatch.setattr(
        dp,
        "scan_bound_dashboards",
        lambda _store=None: dp.BoundScan(bound={}, listed=1, unread=1),
    )
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "unreadable_record"
    )
    assert not rows["dashboard"].whole
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_the_bound_index_reads_the_scan_resolve_bound_slug_reads(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    dst.snap_from(src)
    here = _save_other_package(dst.mc, f"crewmate:{SLUG}")
    calls: list[object] = []
    real = dp.scan_bound_dashboards

    def counting(store: object = None) -> dp.BoundScan:
        calls.append(store)
        return real(store)  # type: ignore[arg-type]

    _floor_monkeypatch.setattr(dp, "scan_bound_dashboards", counting)
    index = stores.BoundIndex(dst.mc)
    assert index.refusal(here, SLUG) == ""
    assert index.refusal("another-package", SLUG) == "another_package_of_this_bot"
    assert index.refusal("another-package", "bea") == ""
    assert len(calls) == 1
    # The same scan, read one binding at a time, is resolve_bound_slug's answer.
    assert dp.resolve_bound_slug(f"crewmate:{SLUG}", store=calls[0]) == here  # type: ignore[arg-type]
    # Past the cap, or with a record unread, every answer is unproven.
    for scan in (
        dp.BoundScan(bound={}, listed=dp.MAX_BINDING_SCAN + 1, unread=0),
        dp.BoundScan(bound={}, listed=1, unread=1),
    ):
        _floor_monkeypatch.setattr(dp, "scan_bound_dashboards", lambda _store=None, s=scan: s)
        assert stores.BoundIndex(dst.mc).refusal(here, SLUG) == "unreadable_record"
