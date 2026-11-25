"""After an instance import, a rollback to any retained dashboard version serves its page.

A Bot's dashboard record keeps its earlier versions, and a rollback to one serves the
gateway-owned page snapshot its pin names. Those snapshots are never carried: the
import rebuilds each from the carried package (``portability_bot_types.carried_pin``),
so a rollback on the importing install serves the package page it served on the
source, and never silently falls back to the template.

The stores step's notices are read twice: through a recording wrapper around
``install_bot_stores`` (the stores behaviour alone) and from the summary row (what the
person reads).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from portability_bot_helpers import (
    AppliedDashboard,
    Homes,
    MadeBot,
    apply_dashboard,
    create_bot,
    dashboard_package,
    export_archive,
    get_dashboard,
    install_fixture_catalog,
)

from kiro_crew import portability_bot_stores as stores
from kiro_crew.agent_panel import panel_dir
from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard_templates import instance
from kiro_crew.portability import apply_import_zip

FIRST_TEXT = "first-retained-page"
SECOND_TEXT = "second-current-page"


@pytest.fixture
def homes(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Homes:
    install_fixture_catalog(tmp_path / "catalog", _floor_monkeypatch)
    return Homes(tmp_path / "homes", _floor_monkeypatch)


@pytest.fixture
def store_notices(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Every notice the stores step raised, by Bot name, whatever the caller passed."""
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


def _row(summary: dict[str, Any], name: str) -> dict[str, Any]:
    [row] = [r for r in summary.get("bots", []) if r["bot"] == name]
    return dict(row)


def _next_package_version(bot: MadeBot, first: AppliedDashboard, text: str) -> int:
    """Save version 2 of *first*'s package and put it in force; the record's version."""
    store = ArtifactStore(data_home() / "artifacts")
    token = store.get(first.package, assign_token=True).content_token
    store.update(
        first.package,
        content=dp.dump_package(
            dashboard_package(bot.slug, f'<p data-dashboard-field="goal">{text}</p>')
        ),
        expected_token=token,
    )
    loaded = store.get(first.package)
    assert int(loaded.version) == 2
    record = instance.apply_package(
        bot.slug,
        package_slug=first.package,
        package_version=2,
        content=dp.canonical_package_content(loaded.content or ""),
    )
    return record.instance_version


def _two_package_versions(homes: Homes) -> MadeBot:
    """E3's source: package v1, then v2, back to v1 and forward to v2 (five versions)."""
    homes.use("a")
    atlas = create_bot("Atlas")
    first = apply_dashboard(atlas, f'<p data-dashboard-field="goal">{FIRST_TEXT}</p>')
    applied = _next_package_version(atlas, first, SECOND_TEXT)
    assert applied == 3
    instance.rollback(atlas.slug, 2)
    instance.rollback(atlas.slug, 3)
    assert instance.read(atlas.slug).instance_version == 5
    return atlas


async def _serves(bot: MadeBot, to_version: int, text: str) -> None:
    back = instance.rollback(bot.slug, to_version)
    assert instance.pinned_package(back) is not None, to_version
    status, body = await get_dashboard(bot)
    assert status == 200, body
    assert text in str(body), (to_version, body)


def _drop_pages(slug: str) -> None:
    for page in panel_dir().glob(f"{slug}.dashboard-page.*.json"):
        page.unlink()


@pytest.mark.asyncio
async def test_every_retained_dashboard_version_serves_its_page_after_a_replace(
    homes: Homes, tmp_path: Path, store_notices: dict[str, list[str]]
) -> None:
    atlas = _two_package_versions(homes)
    archive = export_archive(tmp_path / "a.zip")
    homes.use("b")
    summary = apply_import_zip(archive, mode="replace")
    row = _row(summary, "Atlas")
    assert row["dashboard"] == "restored"
    assert store_notices == {}
    # Versions 4 and 2 pin package version 1; version 3 pins version 2.
    for to_version, text in ((4, FIRST_TEXT), (3, SECOND_TEXT), (2, FIRST_TEXT)):
        await _serves(atlas, to_version, text)
    assert row["notices"] == []


@pytest.mark.asyncio
async def test_a_bot_rolled_back_to_its_template_gets_its_package_pages_back_after_a_replace(
    homes: Homes, tmp_path: Path, store_notices: dict[str, list[str]]
) -> None:
    homes.use("a")
    atlas = create_bot("Atlas")
    applied = apply_dashboard(atlas, f'<p data-dashboard-field="goal">{FIRST_TEXT}</p>')
    back = instance.rollback(atlas.slug, 1)
    assert not back.package_slug
    archive = export_archive(tmp_path / "a.zip")
    homes.use("b")
    summary = apply_import_zip(archive, mode="replace")
    row = _row(summary, "Atlas")
    assert row["dashboard"] == "restored"
    assert not row["part_reasons"].get("dashboard")
    assert store_notices == {}
    assert (data_home() / "artifacts" / applied.package / "meta.json").is_file()
    await _serves(atlas, 2, FIRST_TEXT)
    assert row["notices"] == []


@pytest.mark.asyncio
async def test_a_self_restore_after_losing_every_page_brings_back_the_older_ones_too(
    homes: Homes, tmp_path: Path, store_notices: dict[str, list[str]]
) -> None:
    atlas = _two_package_versions(homes)
    archive = export_archive(tmp_path / "a.zip")
    _drop_pages(atlas.slug)
    summary = apply_import_zip(archive, mode="merge")
    row = _row(summary, "Atlas")
    assert row["dashboard"] == "restored"
    assert store_notices == {}
    assert len(list(panel_dir().glob(f"{atlas.slug}.dashboard-page.*.json"))) == 2
    for to_version, text in ((4, FIRST_TEXT), (3, SECOND_TEXT), (2, FIRST_TEXT)):
        await _serves(atlas, to_version, text)
    assert row["notices"] == []


def _apply_same_page_from_another_package(bot: MadeBot, html: str) -> str:
    """Save a second package showing exactly *html* for *bot* and put it in force."""
    store = ArtifactStore(data_home() / "artifacts")
    art = store.create(
        name=f"{bot.slug} copy",
        content=dp.dump_package(dashboard_package(bot.slug, html)),
        kind=dp.DASHBOARD_KIND,
        source="dashboard",
    )
    loaded = store.get(art.slug)
    instance.apply_package(
        bot.slug,
        package_slug=art.slug,
        package_version=int(loaded.version),
        content=dp.canonical_package_content(loaded.content or ""),
    )
    return art.slug


@pytest.mark.asyncio
async def test_another_packages_identical_page_does_not_mask_the_carried_packages_rebuild(
    homes: Homes, tmp_path: Path, store_notices: dict[str, list[str]]
) -> None:
    # The page digest covers the canonical page only, never the package slug, so a
    # newer version pinning another package with the same page shares the digest of
    # an older version pinning the carried one: the carried package's pin rebuilds it.
    homes.use("a")
    atlas = create_bot("Atlas")
    html = f'<p data-dashboard-field="goal">{FIRST_TEXT}</p>'
    first = apply_dashboard(atlas, html)
    other = _apply_same_page_from_another_package(atlas, html)
    assert other != first.package
    assert _next_package_version(atlas, first, SECOND_TEXT) == 4
    pins = {n: instance._package_field(instance._read_version(atlas.slug, n)) for n in (2, 3)}
    assert pins[2][0] == first.package and pins[3][0] == other
    assert pins[2][2] == pins[3][2] == first.digest
    archive = export_archive(tmp_path / "a.zip")
    homes.use("b")
    summary = apply_import_zip(archive, mode="replace")
    row = _row(summary, "Atlas")
    assert row["dashboard"] == "restored"
    assert store_notices == {}
    assert row["notices"] == []
    await _serves(atlas, 2, FIRST_TEXT)
