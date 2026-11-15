"""Whose an ``artifacts/<P>`` here is, and what a record pinning it may then do.

The same real dashboard package and import home as ``test_portability_bot_stores``
(its fixtures and helpers are reused). An ``artifacts/<P>`` here is a Bot's own only
when its live binding (what ``scan_bound_dashboards`` and ``resolve_bound_slug`` read)
AND its pinned version both name the Bot; a copy with provenance owns its bytes like
any package, a live pointer never does. A page is served from a package here only when
no other package is bound to the Bot, and a kept record whose page or package could
have been brought back and was not is not whole.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_portability_bot_stores import (  # noqa: F401 - _catalog is an autouse fixture
    BOT,
    HELD,
    SLUG,
    _catalog,
    _crafted_v2,
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
from kiro_crew.portability_bot_types import BOT_ARTIFACTS_DIR, REASONS, ImportedBot, PartOutcome

OWNED = frozenset({SLUG})
OWN_OTHER = "another_package_of_this_bot"


@pytest.fixture
def world(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> tuple[_Source, _Import]:
    return _Source(tmp_path / "src-home", _floor_monkeypatch), _Import(
        tmp_path / "dst-home", _floor_monkeypatch
    )


def _record_installed(mc: Path) -> bool:
    return (mc / "members" / SLUG / "dashboard" / "instance.json").is_file()


def _package_here(snap: Path, mc: Path, package: str) -> ArtifactStore:
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / package, mc / "artifacts" / package)
    return ArtifactStore(mc / "artifacts")


def _rebind(store: ArtifactStore, package: str, bound_to: str) -> None:
    """Rebind *package* the way the store does: same layout, so no new version."""
    before = store.get(package, assign_token=True)
    store.update(
        package,
        content=dp.dump_package(_package(bound_to=bound_to)),
        expected_token=before.content_token,
    )
    assert store.get(package).version == before.version


def _set_meta(mc: Path, package: str, change: dict[str, Any]) -> None:
    meta = mc / "artifacts" / package / "meta.json"
    raw = json.loads(meta.read_text(encoding="utf-8"))
    raw.update(change)
    meta.write_text(json.dumps(raw), encoding="utf-8")


def _drop_pages() -> None:
    for page in _page_snapshots():
        (panel_dir() / page).unlink()


def _keep_record(snap: Path, mc: Path) -> None:
    shutil.copytree(snap / "members" / SLUG / "dashboard", mc / "members" / SLUG / "dashboard")


def test_a_package_here_rebound_to_another_bot_keeps_the_record_out(
    world: tuple[_Source, _Import],
) -> None:
    # Its pinned version still names this Bot, but its live binding is another Bot's:
    # the dashboard was handed over, so a record pinning it is not this Bot's to take.
    src, dst = world
    snap = dst.snap_from(src)
    store = _package_here(snap, dst.mc, src.package)
    _rebind(store, src.package, "crewmate:other")
    pinned = store.get(src.package, version=src.version)
    assert dp._decode_package(pinned.content or "")["bound_to"] == f"crewmate:{SLUG}"
    assert dp.resolve_bound_slug("crewmate:other", store=store) == src.package
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "not_restored", HELD)
    assert not os.path.lexists(dst.mc / "members" / SLUG / "dashboard")
    assert _page_snapshots() == []
    assert dp.resolve_bound_slug("crewmate:other", store=store) == src.package


def test_a_kept_record_never_rebuilds_its_page_from_a_rebound_package(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record(snap, dst.mc)
    store = _package_here(snap, dst.mc, src.package)
    _rebind(store, src.package, "crewmate:other")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "page_not_rebuilt", HELD)
    assert _page_snapshots() == []


def test_a_copy_only_package_here_is_this_bots_own(world: tuple[_Source, _Import]) -> None:
    # A copy with provenance owns its bytes: the scan reads it as bound to this Bot,
    # and so does the import.
    src, dst = world
    snap = dst.snap_from(src)
    store = _package_here(snap, dst.mc, src.package)
    _set_meta(
        dst.mc,
        src.package,
        {"source_path": str(dst.mc / "elsewhere.json"), "source_copy_only": True},
    )
    assert dp.scan_bound_dashboards(store).bound == {f"crewmate:{SLUG}": [src.package]}
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert _record_installed(dst.mc)
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]


def test_a_kept_record_rebuilds_its_page_from_a_copy_only_package(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record(snap, dst.mc)
    _package_here(snap, dst.mc, src.package)
    _set_meta(
        dst.mc,
        src.package,
        {"source_path": str(dst.mc / "elsewhere.json"), "source_copy_only": True},
    )
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]


def test_a_live_pointer_package_here_is_never_this_bots(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # Its metadata alone says so, before any read of its content (which would read the
    # file it points at, and could write its meta.json back): no Bot holds a live
    # pointer, so it is no other Bot's either.
    src, dst = world
    snap = dst.snap_from(src)
    _package_here(snap, dst.mc, src.package)
    live = dst.mc / "artifacts" / src.package / "current.html"
    _set_meta(dst.mc, src.package, {"source_path": str(live), "source_copy_only": False})
    meta = (dst.mc / "artifacts" / src.package / "meta.json").read_bytes()
    reads: list[str] = []
    real_get = ArtifactStore.get

    def recording(self: ArtifactStore, slug: str, *args: Any, **kwargs: Any) -> Any:
        reads.append(slug)
        return real_get(self, slug, *args, **kwargs)

    _floor_monkeypatch.setattr(ArtifactStore, "get", recording)
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "not_restored", "artifact_rejected"
    )
    assert not _record_installed(dst.mc)
    assert src.package not in reads
    assert (dst.mc / "artifacts" / src.package / "meta.json").read_bytes() == meta


@pytest.mark.parametrize("kept", [False, True])
def test_a_package_here_that_is_no_dashboard_is_rejected_not_another_bots(
    world: tuple[_Source, _Import], kept: bool
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    if kept:
        _keep_record(snap, dst.mc)
    _package_here(snap, dst.mc, src.package)
    _set_meta(dst.mc, src.package, {"kind": "html", "version_kinds": {"1": "html"}})
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    status = "page_not_rebuilt" if kept else "not_restored"
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", status, "artifact_rejected")
    assert _record_installed(dst.mc) is kept
    assert _page_snapshots() == []


def test_a_complete_scan_answers_whose_the_package_here_is(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scan read the package's live binding already: it is not read a second time.
    src, dst = world
    snap = dst.snap_from(src)
    _package_here(snap, dst.mc, src.package)
    live_reads: list[str] = []
    real_get = ArtifactStore.get

    def recording(self: ArtifactStore, slug: str, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("version") is None:
            live_reads.append(slug)
        return real_get(self, slug, *args, **kwargs)

    _floor_monkeypatch.setattr(ArtifactStore, "get", recording)
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert live_reads == [src.package]


def test_the_pinned_version_is_read_once_on_the_owned_path(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _package_here(snap, dst.mc, src.package)
    reads: list[Any] = []
    real_get = ArtifactStore.get

    def counting(self: ArtifactStore, slug: str, *args: Any, **kwargs: Any) -> Any:
        if slug == src.package and kwargs.get("version") is not None:
            reads.append(kwargs["version"])
        return real_get(self, slug, *args, **kwargs)

    _floor_monkeypatch.setattr(ArtifactStore, "get", counting)
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
    assert reads == [src.version]


def test_a_page_is_not_served_from_a_package_here_when_another_is_bound_to_the_bot(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _package_here(snap, dst.mc, src.package)
    _save_other_package(dst.mc, f"crewmate:{SLUG}")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "page_not_rebuilt", OWN_OTHER)
    assert _record_installed(dst.mc)
    assert _page_snapshots() == []


def test_a_kept_record_does_not_rebuild_its_page_when_another_package_is_bound_to_the_bot(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record(snap, dst.mc)
    _package_here(snap, dst.mc, src.package)
    _save_other_package(dst.mc, f"crewmate:{SLUG}")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "page_not_rebuilt", OWN_OTHER)
    assert not rows["dashboard"].whole
    assert _page_snapshots() == []


def test_another_package_of_this_bot_is_a_reason_of_its_own() -> None:
    assert OWN_OTHER in REASONS and HELD in REASONS
    assert stores._OWN_OTHER == OWN_OTHER


@pytest.mark.parametrize("lacks", ["package", "pinned-version"])
def test_a_package_the_archive_lacks_is_missing_before_any_library_scan(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch, lacks: str
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    carried = snap / BOT_ARTIFACTS_DIR / src.package
    if lacks == "package":
        shutil.rmtree(carried)
    else:
        (carried / "versions" / f"v{src.version}.html").unlink()
    scans: list[object] = []
    _floor_monkeypatch.setattr(dp, "scan_bound_dashboards", lambda store=None: scans.append(store))
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "artifact_missing"
    )
    assert scans == []
    assert _record_installed(dst.mc)


def test_a_kept_record_whose_published_package_has_no_page_is_not_whole(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    # The package was brought back but its page write failed: the package stays, and
    # the part says the page is missing.
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record(snap, dst.mc)
    _floor_monkeypatch.setattr(stores, "_write_page", lambda _slug, _content: "write_failed")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "page_not_rebuilt", "write_failed"
    )
    assert (dst.mc / "artifacts" / src.package / "meta.json").is_file()


def test_a_kept_record_with_nothing_in_the_archive_to_restore_from_stays_kept(
    world: tuple[_Source, _Import],
) -> None:
    # The page and the package were missing before the import and the archive holds no
    # package to bring back: the import changed nothing, so the part is kept.
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record(snap, dst.mc)
    shutil.rmtree(snap / BOT_ARTIFACTS_DIR / src.package)
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "kept")
    assert rows["dashboard"].whole
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)


def test_another_package_of_this_bot_is_answered_before_an_unread_record(
    _floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Two packages bound to the Bot already prove the refusal; the record the scan
    # could not read changes nothing about that.
    scan = dp.BoundScan(bound={f"crewmate:{SLUG}": ["first", "second"]}, listed=3, unread=1)
    _floor_monkeypatch.setattr(dp, "scan_bound_dashboards", lambda _store=None: scan)
    index = stores.BoundIndex(tmp_path)
    assert index.refusal("first", SLUG) == OWN_OTHER
    assert index.refusal("third", SLUG) == OWN_OTHER
    assert index.refusal("first", "bea") == "unreadable_record"


@pytest.mark.parametrize("bound_to", ["crewmate:other", f"crewmate:{SLUG}"])
def test_a_package_appearing_before_a_kept_records_publish_is_judged_as_one_here(
    world: tuple[_Source, _Import], _floor_monkeypatch: pytest.MonkeyPatch, bound_to: str
) -> None:
    # The kept record's package was missing, so the carried one was staged; another
    # appeared at artifacts/<P> before the publish. It is judged as any package here:
    # another Bot's gives no page, this Bot's own gives the page it pins.
    src, dst = world
    snap = dst.snap_from(src)
    _keep_record(snap, dst.mc)
    real = stores._check_staged_artifact

    def gate(*args: Any, **kwargs: Any) -> tuple[str, str]:
        result = real(*args, **kwargs)
        store = _package_here(snap, dst.mc, src.package)
        if bound_to != f"crewmate:{SLUG}":
            _rebind(store, src.package, bound_to)
            for page in (dst.mc / "artifacts" / src.package / "versions").iterdir():
                page.write_text(dp.dump_package(_package(bound_to=bound_to)), encoding="utf-8")
        return result

    _floor_monkeypatch.setattr(stores, "_check_staged_artifact", gate)
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    if bound_to == f"crewmate:{SLUG}":
        assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "restored")
        assert _page_snapshots() == [f"{SLUG}.dashboard-page.{src.digest}.json"]
    else:
        assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", "page_not_rebuilt", HELD)
        assert _page_snapshots() == []


BEA = ImportedBot(
    name="Bea",
    slug="bea",
    member_id="bea",
    memory_store="bea-store",
    generation="1",
    slot_key="dashboard:member:bea",
    kiro_agent="Bea",
)


@pytest.mark.parametrize("bea_first", [False, True])
def test_two_archive_bots_pinning_one_package_end_the_same_in_either_order(
    world: tuple[_Source, _Import], bea_first: bool
) -> None:
    # Bea's record pins the package the archive binds to this Bot: it is not Bea's,
    # whether this Bot's record published it first or not.
    src, dst = world
    snap = dst.snap_from(src)
    shutil.copytree(snap / "members" / SLUG / "dashboard", snap / "members" / "bea" / "dashboard")
    bots = [BEA, BOT] if bea_first else [BOT, BEA]
    rows = {
        (r.bot, r.part): r
        for r in stores.install_bot_stores(snap, dst.mc, bots, allow_unpinned=False)
    }
    assert rows[(BOT.name, "dashboard")] == PartOutcome(BOT.name, "dashboard", "restored")
    assert rows[("Bea", "dashboard")] == PartOutcome("Bea", "dashboard", "not_restored", HELD)
    assert not os.path.lexists(dst.mc / "members" / "bea" / "dashboard")
    assert _page_snapshots("bea") == []
    assert dp.resolve_bound_slug(f"crewmate:{SLUG}", store=ArtifactStore(dst.mc / "artifacts")) == (
        src.package
    )


@pytest.mark.parametrize("kept", [False, True])
def test_a_carried_package_the_archive_binds_to_another_bot_is_not_this_bots(
    world: tuple[_Source, _Import], kept: bool
) -> None:
    # Its pinned version names this Bot, but its live page names another: the archive
    # gave the package to that Bot, so this Bot's record does not take it.
    src, dst = world
    snap = dst.snap_from(src)
    if kept:
        _keep_record(snap, dst.mc)
    _crafted_v2(snap, src, "another-bot")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    status = "page_not_rebuilt" if kept else "not_restored"
    assert rows["dashboard"] == PartOutcome(BOT.name, "dashboard", status, HELD)
    assert _record_installed(dst.mc) is kept
    assert not os.path.lexists(dst.mc / "artifacts" / src.package)
    assert _page_snapshots() == []


def _here_with_kindless_live(snap: Path, mc: Path, package: str) -> None:
    """Put *package* here with its live page carrying no ``kind``, still bound to the Bot."""
    here = mc / "artifacts" / package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / package, here)
    raw = json.loads((here / "current.html").read_text(encoding="utf-8"))
    raw.pop("kind", None)
    (here / "current.html").write_text(json.dumps(raw), encoding="utf-8")


def test_the_live_binding_is_read_by_the_scans_rule_whether_or_not_the_scan_completes(
    world: tuple[_Source, _Import],
) -> None:
    """One reading of a live package's binding (``dashboard_package.live_binding_of``).

    A complete scan answers from the index, an incomplete one reads the package itself;
    both must judge a package the same way, so only the unreadable unrelated record
    changes the outcome (page_not_rebuilt / unreadable_record), never the ownership.
    """
    src, dst = world
    snap = dst.snap_from(src)
    _here_with_kindless_live(snap, dst.mc, src.package)
    assert _run(snap, dst.mc, owned_slugs=OWNED)["dashboard"] == PartOutcome(
        BOT.name, "dashboard", "restored"
    )


def test_an_incomplete_scan_does_not_turn_an_own_package_into_a_rejected_one(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    _here_with_kindless_live(snap, dst.mc, src.package)
    broken = _save_other_package(dst.mc, "crewmate:other")
    (dst.mc / "artifacts" / broken / "current.html").write_text("{not json", encoding="utf-8")
    row = _run(snap, dst.mc, owned_slugs=OWNED)["dashboard"]
    assert (row.status, row.reason) == ("page_not_rebuilt", "unreadable_record")
    assert _record_installed(dst.mc)


def test_a_meta_record_nested_past_the_decoder_is_unreadable_and_spares_the_briefing(
    world: tuple[_Source, _Import],
) -> None:
    src, dst = world
    snap = dst.snap_from(src)
    here = dst.mc / "artifacts" / src.package
    shutil.copytree(snap / BOT_ARTIFACTS_DIR / src.package, here)
    (here / "meta.json").write_text("[" * 50000, encoding="utf-8")
    rows = _run(snap, dst.mc, owned_slugs=OWNED)
    assert (rows["dashboard"].status, rows["dashboard"].reason) == (
        "not_restored",
        "unreadable_record",
    )
    assert rows["briefing"].status != "not_restored"


def test_live_binding_of_reads_bound_to_and_leaves_bad_text_to_the_caller() -> None:
    from kiro_crew.artifacts import ArtifactError

    assert dp.live_binding_of(json.dumps({"bound_to": "crewmate:atlas"})) == "crewmate:atlas"
    assert dp.live_binding_of(json.dumps({"bound_to": 7})) is None
    assert dp.live_binding_of(json.dumps(["crewmate:atlas"])) is None
    with pytest.raises(ArtifactError):
        dp.live_binding_of("{not json")
