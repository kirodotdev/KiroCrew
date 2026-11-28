"""A carried dashboard record installs unless the package it pins here is another Bot's.

The same real dashboard package and import home as ``test_portability_bot_stores``
(its fixtures and helpers are reused). What keeps a package from being published --
the archive lacks it, the gate refuses it, or the scan of the bound dashboards here
cannot prove no other package is bound to the Bot -- never keeps the record out: the
record installs and the part is ``page_not_rebuilt`` with that reason. Only an
``artifacts/<P>`` here that is not this Bot's own keeps it out.
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
from kiro_crew.agent_panel import panel_dir
from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.portability_bot_types import BOT_ARTIFACTS_DIR, PartOutcome

OWNED = frozenset({SLUG})


@pytest.fixture
def world(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> tuple[_Source, _Import]:
    return _Source(tmp_path / "src-home", _floor_monkeypatch), _Import(
        tmp_path / "dst-home", _floor_monkeypatch
    )


def _record_installed(mc: Path) -> bool:
    return (mc / "members" / SLUG / "dashboard" / "instance.json").is_file()


def test_a_package_the_archive_lacks_still_installs_the_record(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    shutil.rmtree(snap / BOT_ARTIFACTS_DIR / src.package)
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_missing"
    )
    assert _record_installed(dst.mc)
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert _page_snapshots() == []


def test_an_unrelated_unreadable_dashboard_blocks_only_the_package(
    world: tuple[_Source, _Import],
) -> None:
    # Another dashboard here that cannot be read leaves the binding unproven: the
    # package is never published next to it, but the record still installs.
    src, dst = world
    snap = dst.snap_from(src)
    broken = _save_other_package(dst.mc, "crewmate:other")
    (dst.mc / "artifacts" / broken / "current.html").write_text("{not json", encoding="utf-8")
    assert not dp.scan_bound_dashboards(ArtifactStore(dst.mc / "artifacts")).complete
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "unreadable_record"
    )
    assert _record_installed(dst.mc)
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_a_scan_past_its_cap_blocks_only_the_package(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _floor_monkeypatch.setattr(
        dp,
        "scan_bound_dashboards",
        lambda _store=None: dp.BoundScan(bound={}, listed=dp.MAX_BINDING_SCAN + 1, unread=0),
    )
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "unreadable_record"
    )
    assert _record_installed(dst.mc)
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_an_own_package_here_missing_the_pinned_version_is_artifact_missing(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    here = dst.mc / "artifacts" / src.package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, here)
    (here / "versions" / f"v{src.version}.html").unlink()
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_missing"
    )
    assert _record_installed(dst.mc)
    assert _page_snapshots() == []


def test_a_package_here_missing_the_pinned_version_and_bound_elsewhere_is_held(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    here = dst.mc / "artifacts" / src.package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, here)
    (here / "versions" / f"v{src.version}.html").unlink()
    (here / "current.html").write_text(
        dp.dump_package(_package(bound_to="crewmate:other")), encoding="utf-8"
    )
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", HELD)
    assert not _record_installed(dst.mc)


def test_a_package_here_that_cannot_be_read_keeps_the_record_out(
    world: tuple[_Source, _Import],
) -> None:
    # Whose package it is cannot be told, so no record pinning it is installed.
    src, dst = world
    snap = dst.snap_from(src)
    here = dst.mc / "artifacts" / src.package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, here)
    (here / "meta.json").write_text("[1, 2]", encoding="utf-8")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "not_restored", "unreadable_record"
    )
    assert not _record_installed(dst.mc)


def test_an_own_package_here_whose_page_fails_the_gate_keeps_the_record(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    here = dst.mc / "artifacts" / src.package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, here)
    edited = dp.dump_package(_package(html="<p>edited here</p>"))
    for page in (here / "current.html", here / "versions" / f"v{src.version}.html"):
        page.write_text(edited, encoding="utf-8")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "digest_mismatch"
    )
    assert _record_installed(dst.mc)
    assert _page_snapshots() == []


def test_a_package_publish_that_fails_still_installs_the_record(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _floor_monkeypatch.setattr(stores, "_publish_artifact", lambda *_a: "write_failed")
    rows = _run(snap, dst.mc)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "write_failed"
    )
    assert _record_installed(dst.mc)


def test_a_self_merge_whose_kept_record_cannot_rebuild_its_page_is_not_whole(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record and its package are here, only the page snapshot is missing and its
    # rebuild fails: the page could have been brought back and was not, so the part
    # is not whole, with why.
    src, dst = world
    snap = dst.snap_from(src)
    assert _run(snap, dst.mc)["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    for page in _page_snapshots():
        (panel_dir() / page).unlink()
    _floor_monkeypatch.setattr(stores, "_write_page", lambda _slug, _content: "write_failed")
    rows = _run(dst.snap_from(src), dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "write_failed"
    )
    assert not rows["dashboard"].whole
