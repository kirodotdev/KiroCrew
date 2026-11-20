"""The store listing answers within its budget while slow ``app.json`` fetches run.

An app page waits for ``GET /api/apps/registry``, and a cold manifest cache costs one
``git clone`` per external row. A listing that waited for every clone took minutes on a
host where the clones fail slowly. These tests pin that the listing answers at the
budget, that the fetches still running fill the cache in the background without a
second listing cloning the same source again, and that a row past the budget shows
its last cached copy however old.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture

from kiro_crew.apps import registry
from kiro_crew.apps.registry_pipeline import catalog
from kiro_crew.testing.wait import await_until


def _external_rows(count: int) -> list[dict]:
    return [
        {
            "name": f"ext-{i}",
            "gitUrl": f"https://example.invalid/acme/ext-{i}.git",
            "_registry": "labs",
        }
        for i in range(count)
    ]


def _stub_listing_sources(monkeypatch, rows: list[dict], resolve) -> None:
    async def _fake_external():
        return [dict(r) for r in rows]

    monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
    monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
    monkeypatch.setattr(registry, "_resolve_manifest", resolve)
    monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
    monkeypatch.setattr(registry.official_catalog, "fetch_inventory_entries", lambda: [])
    # ``raising=False`` so a build without the budget fails on the listing's wait,
    # the behaviour under test, rather than on this patch.
    monkeypatch.setattr(registry, "_LISTING_MANIFEST_BUDGET", 0.05, raising=False)


async def _listing(listing):
    return await await_until(
        asyncio.ensure_future(listing), what="the listing answering within its budget"
    )


async def _drain_background_fetches() -> None:
    await await_until(
        asyncio.gather(*list(catalog._MANIFEST_BATCHES)), what="the background fetches finishing"
    )


async def _signalled(event: asyncio.Event, what: str) -> None:
    await await_until(event, what=what)


def _signal_on_resolve_once(monkeypatch, nth: int) -> asyncio.Event:
    """An event set when ``_resolve_manifest_once`` is entered for the *nth* time.

    The call joins or starts its fetch before its first await, so once the event is
    set the caller has looked up the running fetch.
    """
    real = catalog._resolve_manifest_once
    reached = asyncio.Event()
    calls = 0

    async def _counting(entry):
        nonlocal calls
        calls += 1
        if calls == nth:
            reached.set()
        return await real(entry)

    monkeypatch.setattr(registry, "_resolve_manifest_once", _counting)
    assert catalog._resolve_manifest_once is _counting, "the patch must reach catalog"
    return reached


def _first_party_seed() -> dict:
    (seed,) = _external_rows(1)
    del seed["_registry"]
    seed["author"] = "Kiro Crew"
    return seed


@pytest.mark.asyncio
async def test_listing_answers_at_the_budget_and_the_fetches_keep_running(monkeypatch):
    rows = _external_rows(12)
    release = asyncio.Event()
    finished: list[str] = []

    async def _slow_resolve(entry):
        await release.wait()
        finished.append(entry["name"])
        return {**entry, "displayName": "Fetched"}

    _stub_listing_sources(monkeypatch, rows, _slow_resolve)

    listed = await _listing(registry.list_registry())

    by_name = {r["name"]: r for r in listed}
    assert set(by_name) >= {r["name"] for r in rows}
    # No fetch finished, so every row answers bare, as a failed fetch does.
    assert all("displayName" not in by_name[r["name"]] for r in rows)
    assert catalog._MANIFEST_BATCHES, "the fetches past the budget must keep running"

    release.set()
    await _drain_background_fetches()
    # Every row was fetched, not only the first gate's worth.
    assert sorted(finished) == sorted(r["name"] for r in rows)


@pytest.mark.asyncio
async def test_a_seed_row_does_not_hold_the_offline_listing(monkeypatch):
    (seed,) = _external_rows(1)
    del seed["_registry"]
    release = asyncio.Event()

    async def _slow_resolve(entry):
        await release.wait()
        return entry

    _stub_listing_sources(monkeypatch, [], _slow_resolve)
    monkeypatch.setattr(registry, "_load_registry_file", lambda: [dict(seed)])

    listed = await _listing(registry.list_registry())
    release.set()
    await _drain_background_fetches()

    assert seed["name"] in {r["name"] for r in listed}


@pytest.mark.asyncio
async def test_an_edition_row_does_not_hold_the_catalog_listing(monkeypatch):
    (edition,) = _external_rows(1)
    del edition["_registry"]
    release = asyncio.Event()

    async def _slow_resolve(entry):
        await release.wait()
        return entry

    _stub_listing_sources(monkeypatch, [], _slow_resolve)
    monkeypatch.setattr(
        registry.official_catalog,
        "list_catalog_rows",
        lambda: [{"name": "shipped-app", "source": {"type": "builtin"}}],
    )
    monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})
    monkeypatch.setattr(registry, "_edition_registry_rows", lambda: [dict(edition)])

    listed = await _listing(registry.list_catalog_apps())
    release.set()
    await _drain_background_fetches()

    assert edition["name"] in {r["name"] for r in listed}


@pytest.mark.asyncio
async def test_a_second_listing_joins_the_fetches_still_running(monkeypatch):
    rows = _external_rows(3)
    release = asyncio.Event()
    calls: list[str] = []

    async def _slow_resolve(entry):
        calls.append(entry["name"])
        await release.wait()
        return entry

    _stub_listing_sources(monkeypatch, rows, _slow_resolve)

    await _listing(registry.list_registry())
    await _listing(registry.list_registry())
    release.set()
    await _drain_background_fetches()

    assert sorted(calls) == sorted(r["name"] for r in rows)


@pytest.mark.asyncio
async def test_a_row_past_the_budget_shows_its_last_cached_copy(monkeypatch):
    (row,) = _external_rows(1)
    registry._write_manifest_cache(row, {"displayName": "Cached a week ago"})
    week_ago = time.time() - 7 * 86400
    os.utime(registry._manifest_cache_path(row), (week_ago, week_ago))
    assert registry._read_manifest_cache(row) is None, "the copy must be past its TTL"
    release = asyncio.Event()

    async def _slow_resolve(entry):
        await release.wait()
        return entry

    _stub_listing_sources(monkeypatch, [row], _slow_resolve)

    listed = await _listing(registry.list_registry())
    release.set()
    await _drain_background_fetches()

    (shown,) = [r for r in listed if r["name"] == row["name"]]
    assert shown["displayName"] == "Cached a week ago"


@pytest.mark.asyncio
async def test_overlapping_listings_each_get_their_own_row(monkeypatch):
    seed = _first_party_seed()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _slow_resolve(entry):
        calls.append(entry["name"])
        started.set()
        await release.wait()
        return {**entry, "displayName": "Fetched"}

    _stub_listing_sources(monkeypatch, [], _slow_resolve)
    monkeypatch.setattr(registry, "_load_registry_file", lambda: [dict(seed)])
    # Far past the bounded waits below, so both listings take the fetched row
    # rather than a stale one however slow the host.
    monkeypatch.setattr(registry, "_LISTING_MANIFEST_BUDGET", 60.0)
    joined = _signal_on_resolve_once(monkeypatch, 2)

    first = asyncio.ensure_future(registry.list_registry())
    await _signalled(started, "the first listing's fetch start")
    second = asyncio.ensure_future(registry.list_registry())
    await _signalled(joined, "the second listing reach the running fetch")
    release.set()
    listings = await _listing(asyncio.gather(first, second))
    await _drain_background_fetches()

    assert calls == [seed["name"]], "the second listing must join the running fetch"
    rows = [next(r for r in listed if r["name"] == seed["name"]) for listed in listings]
    assert rows[0] is not rows[1]
    assert [r["displayName"] for r in rows] == ["Fetched", "Fetched"]
    assert [r["verified"] for r in rows] == [True, True]


@pytest.mark.asyncio
async def test_entries_that_differ_beyond_their_coordinates_do_not_share_a_fetch(monkeypatch):
    (external,) = _external_rows(1)
    seed = {k: v for k, v in external.items() if k != "_registry"}
    release = asyncio.Event()
    both_started = asyncio.Event()
    calls: list[object] = []

    async def _slow_resolve(entry):
        calls.append(entry.get("_registry"))
        if len(calls) == 2:
            both_started.set()
        await release.wait()
        return {**entry, "displayName": "Fetched"}

    _stub_listing_sources(monkeypatch, [], _slow_resolve)

    pending = asyncio.gather(
        catalog._resolve_manifest_once(dict(seed)),
        catalog._resolve_manifest_once(dict(external)),
    )
    await _signalled(both_started, "a fetch of its own for each entry")
    release.set()
    seed_row, external_row = await await_until(pending, what="both fetches finishing")

    assert sorted(calls, key=repr) == sorted([None, "labs"], key=repr)
    assert "_registry" not in seed_row
    assert external_row["_registry"] == "labs"
    stamped = registry._apply_trust_fields([seed_row, external_row])
    assert [r["provenance"] for r in stamped] == ["official", "external"]
    await _drain_background_fetches()


@pytest.mark.asyncio
async def test_a_listing_that_joins_after_the_budget_keeps_the_trust_stamp(monkeypatch):
    # The first listing answers bare at the budget and trust-stamps its row, which
    # pops ``_index_author``. The fetch still running must not see that, or the
    # listing that joins it gets a first-party row marked unverified.
    seed = _first_party_seed()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _slow_resolve(entry):
        calls.append(entry["name"])
        started.set()
        await release.wait()
        return {**entry, "displayName": "Fetched"}

    _stub_listing_sources(monkeypatch, [], _slow_resolve)
    monkeypatch.setattr(registry, "_load_registry_file", lambda: [dict(seed)])

    (bare,) = [r for r in await _listing(registry.list_registry()) if r["name"] == seed["name"]]
    assert "displayName" not in bare, "the first listing must answer at the budget"
    await _signalled(started, "the first listing's fetch start")

    monkeypatch.setattr(registry, "_LISTING_MANIFEST_BUDGET", 60.0)
    joined = _signal_on_resolve_once(monkeypatch, 1)
    second = asyncio.ensure_future(registry.list_registry())
    await _signalled(joined, "the second listing reach the running fetch")
    release.set()
    listed = await _listing(second)
    await _drain_background_fetches()

    assert calls == [seed["name"]], "the second listing must join the running fetch"
    (row,) = [r for r in listed if r["name"] == seed["name"]]
    assert row["displayName"] == "Fetched"
    assert row["verified"] is True


@pytest.mark.asyncio
async def test_a_mutated_listing_row_reaches_no_other_row(monkeypatch):
    # A listing owns every row it returns: changing one, nested values included,
    # reaches neither the shared fetch nor another listing on the same fetch.
    seed = _first_party_seed()
    started = asyncio.Event()
    release = asyncio.Event()
    fetch_inputs: list[dict] = []
    fetched: list[dict] = []

    async def _slow_resolve(entry):
        fetch_inputs.append(entry)
        started.set()
        await release.wait()
        row = {**entry, "displayName": "Fetched", "extra": {"tags": ["fetched"]}}
        fetched.append(row)
        return row

    _stub_listing_sources(monkeypatch, [], _slow_resolve)
    monkeypatch.setattr(registry, "_load_registry_file", lambda: [dict(seed)])

    # The bare row a listing answers with at the budget, mutated while the fetch runs.
    (bare,) = [r for r in await _listing(registry.list_registry()) if r["name"] == seed["name"]]
    await _signalled(started, "the first listing's fetch start")
    bare.pop("_index_author", None)
    bare["author"] = "Mallory"
    bare["extra"] = {"tags": ["mutated"]}

    monkeypatch.setattr(registry, "_LISTING_MANIFEST_BUDGET", 60.0)
    joined = _signal_on_resolve_once(monkeypatch, 2)
    listings = [asyncio.ensure_future(registry.list_registry()) for _ in range(2)]
    await _signalled(joined, "both later listings reach the running fetch")
    release.set()
    first, second = [
        next(r for r in listed if r["name"] == seed["name"])
        for listed in await _listing(asyncio.gather(*listings))
    ]
    await _drain_background_fetches()

    assert len(fetched) == 1, "every listing must share the one fetch"
    first.pop("_index_author", None)
    first["author"] = "Mallory"
    first["extra"]["tags"].append("mutated")

    assert fetch_inputs[0]["author"] == "Kiro Crew"
    assert fetch_inputs[0].get("_index_author") == "Kiro Crew"
    assert fetched[0]["extra"] == {"tags": ["fetched"]}
    assert second["author"] == "Kiro Crew"
    assert second["extra"] == {"tags": ["fetched"]}
    assert second["verified"] is True


@pytest.mark.asyncio
async def test_a_catalog_row_behind_slow_clones_keeps_the_catalog_copy(monkeypatch):
    # A catalog row needs no fetch, so it must not queue behind the clones and
    # answer from the agent-writable manifest cache when the deadline passes.
    seeds = [{k: v for k, v in r.items() if k != "_registry"} for r in _external_rows(8)]
    catalog_row = {
        "name": "shelf-app",
        "gitUrl": "https://example.invalid/acme/shelf-app.git",
        "_catalog": True,
        "displayName": "From the catalog",
        "version": "1.0.0",
    }
    registry._write_manifest_cache(
        catalog_row, {"displayName": "Planted", "version": "9.9.9", "permissions": ["all"]}
    )
    release = asyncio.Event()

    async def _slow_resolve(entry):
        await release.wait()
        return entry

    _stub_listing_sources(monkeypatch, [], _slow_resolve)
    monkeypatch.setattr(
        registry, "_load_registry_file", lambda: [dict(r) for r in [*seeds, catalog_row]]
    )

    listed = await _listing(registry.list_registry())
    release.set()
    await _drain_background_fetches()

    (shown,) = [r for r in listed if r["name"] == catalog_row["name"]]
    assert shown.get("displayName") == "From the catalog"
    assert shown.get("version") == "1.0.0"
    assert "permissions" not in shown


@pytest.mark.asyncio
async def test_an_installed_app_past_the_budget_shows_its_installed_manifest(monkeypatch):
    # ``_resolve_manifest`` reads the installed checkout's cache when the source has
    # none, so a row past the deadline reads it too, whatever its age.
    (row,) = _external_rows(1)
    registry._write_installed_manifest_cache(
        row, {"displayName": "Installed copy", "version": "0.1.11"}
    )
    week_ago = time.time() - 7 * 86400
    os.utime(registry._installed_manifest_cache_path(row), (week_ago, week_ago))
    assert not registry._manifest_cache_path(row).exists(), "no source cache"
    release = asyncio.Event()

    async def _slow_resolve(entry):
        await release.wait()
        return entry

    _stub_listing_sources(monkeypatch, [row], _slow_resolve)

    listed = await _listing(registry.list_registry())
    release.set()
    await _drain_background_fetches()

    (shown,) = [r for r in listed if r["name"] == row["name"]]
    assert shown.get("displayName") == "Installed copy"
    assert shown.get("version") == "0.1.11"


@pytest.mark.parametrize("listing", ["list_registry", "list_catalog_apps"])
@pytest.mark.asyncio
async def test_both_manifest_batches_share_one_listing_deadline(monkeypatch, listing):
    # The seed (or edition) batch and the external batch wait for one deadline
    # together, so a cold listing waits the budget once, not once per batch.
    (first,) = [{k: v for k, v in r.items() if k != "_registry"} for r in _external_rows(1)]
    first["name"] = "first-batch"
    (external,) = _external_rows(1)
    release = asyncio.Event()

    async def _slow_resolve(entry):
        await release.wait()
        return entry

    _stub_listing_sources(monkeypatch, [external], _slow_resolve)
    monkeypatch.setattr(registry, "_LISTING_MANIFEST_BUDGET", 0.3)
    if listing == "list_registry":
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [dict(first)])
    else:
        monkeypatch.setattr(
            registry.official_catalog,
            "list_catalog_rows",
            lambda: [{"name": "shipped-app", "source": {"type": "builtin"}}],
        )
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})
        monkeypatch.setattr(registry, "_edition_registry_rows", lambda: [dict(first)])

    real = catalog._gather_listing_manifests
    batches: list[tuple[list[str], float | None, float]] = []

    async def _spy(entries, resolve, deadline=None):
        now = asyncio.get_running_loop().time()
        batches.append(([e["name"] for e in entries], deadline, now))
        return await real(entries, resolve, deadline)

    monkeypatch.setattr(registry, "_gather_listing_manifests", _spy)
    assert catalog._gather_listing_manifests is _spy, "the patch must reach catalog"

    listed = await _listing(getattr(registry, listing)())
    release.set()
    await _drain_background_fetches()

    assert {first["name"], external["name"]} <= {r["name"] for r in listed}
    assert [names for names, _, _ in batches] == [[first["name"]], [external["name"]]]
    (_, first_deadline, _), (_, second_deadline, second_start) = batches
    assert first_deadline is not None, "the listing must set one deadline"
    assert second_deadline == first_deadline, "both batches must share the deadline"
    # The first batch waited for its gated fetch, so less than the budget was left.
    assert second_deadline - second_start < 0.3
