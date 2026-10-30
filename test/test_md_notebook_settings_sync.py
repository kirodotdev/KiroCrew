"""Notes (md-notebook) backend: change polling, sync stamps, settings and the auto-sync loop.

Covers ``/api/changes`` external-edit detection, how a pull made outside the app stamps
the last sync, the settings store, and the server-side auto-sync loop. Shared fixtures
live in ``md_notebook_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest
from aiohttp import web
from md_notebook_helpers import _seed_template  # noqa: F401 - fixture
from md_notebook_helpers import fixtures_fixture  # noqa: F401 - fixture
from md_notebook_helpers import _BARRIER_LOST_RUN_SEC, _clone, _git, _wait_for, signed_client

from kiro_crew.apps.builtins.md_notebook import git_ops

# ---------------------------------------------------------------------------
# External change detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_reports_external_edit(fixtures) -> None:
    _mod, _remote, seed = fixtures
    async with signed_client(_mod) as client:
        status, body = await client.post("/api/vaults/attach", {"path": str(seed)})
        vault_id = body["vault"]["id"]
        _, first = await client.get(f"/api/changes?vault={vault_id}&since=0")
        start_rev = first["rev"]

        (seed / "One.md").write_text("# One\n\ntouched externally\n", encoding="utf-8")

        _, poll = await client.get(f"/api/changes?vault={vault_id}&since={start_rev}")
        assert poll["rev"] != start_rev
        assert "One.md" in poll["changed"]


@pytest.mark.asyncio
async def test_own_save_is_not_reported_as_external(fixtures) -> None:
    """Saving through the API must not look like someone else's edit."""
    _mod, _remote, seed = fixtures
    async with signed_client(_mod) as client:
        _, body = await client.post("/api/vaults/attach", {"path": str(seed)})
        vault_id = body["vault"]["id"]
        _, first = await client.get(f"/api/changes?vault={vault_id}&since=0")
        start_rev = first["rev"]

        await client.put("/api/note", {"path": "One.md", "content": "# One\n\nvia api\n"})

        _, poll = await client.get(f"/api/changes?vault={vault_id}&since={start_rev}")
        assert "One.md" not in poll["changed"]


# ---------------------------------------------------------------------------
# A pull made outside the app counts as a sync
# ---------------------------------------------------------------------------


def _publish_from_seed(seed: Path, name: str) -> None:
    """Land a new note on the remote from the other checkout, as another writer would."""
    (seed / name).write_text(f"# {name}\n\nwritten elsewhere\n", encoding="utf-8")
    _git("add", name, cwd=seed)
    _git("commit", "-m", f"add {name}", cwd=seed)
    _git("push", "origin", "main", cwd=seed)


def _fetch_head_ms(vault: dict[str, Any]) -> int:
    return int(os.stat(Path(vault["gitDir"]) / "FETCH_HEAD").st_mtime * 1000)


@pytest.mark.asyncio
async def test_a_manual_pull_that_matches_the_remote_stamps_last_sync(fixtures) -> None:
    """A user who pulls the vault by hand (auto sync unavailable to them) sees
    "Synced N ago" move: the poll stamps the fetch time once HEAD sits at the
    fetched tip with nothing pending, and settings carry that stamp to the page."""
    server_mod, remote, seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        _, first = await client.get(f"/api/changes?vault={vault['id']}&since=0")
        # A fresh clone has never fetched: nothing to count.
        assert (await client.get("/api/settings"))[1]["settings"]["lastSync"] == {}

        _publish_from_seed(seed, "Three.md")
        _git("pull", "--ff-only", "origin", "main", cwd=Path(vault["localPath"]))

        _, poll = await client.get(f"/api/changes?vault={vault['id']}&since={first['rev']}")
        assert "Three.md" in poll["changed"]
        _, settings = await client.get("/api/settings")
        assert settings["settings"]["lastSync"][vault["id"]] == _fetch_head_ms(vault)
        # The poll's reply shape is unchanged; the stamp travels through settings.
        assert "lastSync" not in poll


@pytest.mark.asyncio
async def test_a_manual_pull_leaving_local_commits_unpushed_is_not_a_sync(fixtures) -> None:
    """HEAD ahead of the fetched tip means notes exist only on this machine;
    the label must not claim they are backed up."""
    server_mod, remote, seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        local = Path(vault["localPath"])
        (local / "Mine.md").write_text("# Mine\n\nnot pushed\n", encoding="utf-8")
        _git("add", "Mine.md", cwd=local)
        _git("commit", "-m", "local only", cwd=local)

        _publish_from_seed(seed, "Theirs.md")
        _git("pull", "--no-rebase", "--no-edit", "origin", "main", cwd=local)

        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        _, settings = await client.get("/api/settings")
        assert settings["settings"]["lastSync"] == {}


@pytest.mark.asyncio
async def test_a_manual_pull_over_uncommitted_edits_is_not_a_sync(fixtures) -> None:
    """An edit still pending in the vault's scope never reached the remote."""
    server_mod, remote, seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        local = Path(vault["localPath"])
        _publish_from_seed(seed, "Three.md")
        _git("pull", "--ff-only", "origin", "main", cwd=local)
        (local / "One.md").write_text("# One\n\nstill pending\n", encoding="utf-8")

        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        _, settings = await client.get("/api/settings")
        assert settings["settings"]["lastSync"] == {}


@pytest.mark.asyncio
async def test_the_apps_own_fetch_is_not_stamped_twice(fixtures) -> None:
    """The app's sync fetches too; its stamp already covers that fetch, so the
    poll leaves it alone rather than moving it back to the fetch time."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body
        stamped = body["lastSync"]

        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        _, settings = await client.get("/api/settings")
        assert settings["settings"]["lastSync"][vault["id"]] == stamped


@pytest.mark.asyncio
async def test_a_repointed_origin_is_not_a_sync(fixtures, tmp_path: Path) -> None:
    """The same origin pin sync enforces: a `.git/config` whose origin differs
    from the trusted URL earns nothing, however level HEAD is with its ref."""
    server_mod, remote, seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        local = Path(vault["localPath"])
        _publish_from_seed(seed, "Three.md")
        _git("pull", "--ff-only", "origin", "main", cwd=local)
        _git("remote", "set-url", "origin", str(tmp_path / "elsewhere.git"), cwd=local)

        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        _, settings = await client.get("/api/settings")
        assert settings["settings"]["lastSync"] == {}


@pytest.mark.asyncio
async def test_a_poll_between_fetch_and_merge_does_not_lose_the_pull(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git pull` writes FETCH_HEAD, then merges. A poll landing in between sees
    HEAD behind the tip; that fetch must still be stamped once the merge lands."""
    server_mod, remote, seed = fixtures
    monkeypatch.setattr(server_mod, "FETCH_SETTLE_MS", 10**9)
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        local = Path(vault["localPath"])
        _publish_from_seed(seed, "Three.md")
        _git("fetch", "origin", "main", cwd=local)

        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        assert (await client.get("/api/settings"))[1]["settings"]["lastSync"] == {}

        _git("merge", "--ff-only", "FETCH_HEAD", cwd=local)
        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        _, settings = await client.get("/api/settings")
        assert settings["settings"]["lastSync"][vault["id"]] == _fetch_head_ms(vault)


@pytest.mark.asyncio
async def test_an_old_head_mismatch_is_final(fixtures, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the settle window, HEAD behind or ahead of the tip is a vault that
    simply differs from its remote; that fetch is not re-examined every tick."""
    server_mod, remote, seed = fixtures
    monkeypatch.setattr(server_mod, "FETCH_SETTLE_MS", 0)
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        local = Path(vault["localPath"])
        _publish_from_seed(seed, "Three.md")
        _git("fetch", "origin", "main", cwd=local)

        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        _git("merge", "--ff-only", "FETCH_HEAD", cwd=local)
        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        assert (await client.get("/api/settings"))[1]["settings"]["lastSync"] == {}


@pytest.mark.asyncio
async def test_a_failed_stamp_write_does_not_consume_the_fetch(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch is marked seen only after its stamp is on disk: a write that
    fails leaves it for the next poll instead of losing the pull."""
    server_mod, remote, seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        _publish_from_seed(seed, "Three.md")
        _git("pull", "--ff-only", "origin", "main", cwd=Path(vault["localPath"]))

        # A flag rather than monkeypatch.undo(): undo would also drop the
        # fixtures' own patches (the backend's test home among them).
        real_write, disk_full = server_mod._write_settings_sync, True

        def _write(settings: dict[str, Any]) -> None:
            if disk_full:
                raise OSError(28, "No space left on device")
            real_write(settings)

        monkeypatch.setattr(server_mod, "_write_settings_sync", _write)
        status, _ = await client.get(f"/api/changes?vault={vault['id']}&since=0")
        assert status == 200
        assert (await client.get("/api/settings"))[1]["settings"]["lastSync"] == {}

        disk_full = False
        await client.get(f"/api/changes?vault={vault['id']}&since=0")
        _, settings = await client.get("/api/settings")
        assert settings["settings"]["lastSync"][vault["id"]] == _fetch_head_ms(vault)


@pytest.mark.asyncio
async def test_a_redirected_gitdir_fails_the_pin(fixtures, tmp_path: Path) -> None:
    """The same pin sync enforces: a `.git` pointer aimed elsewhere is refused
    before any git command runs in the checkout."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        git_ops_mod = server_mod.git_ops
        assert await git_ops_mod.gitdir_is_trusted(vault["localPath"], vault["gitDir"])
        assert not await git_ops_mod.gitdir_is_trusted(
            vault["localPath"], str(tmp_path / "elsewhere")
        )


@pytest.mark.asyncio
async def test_record_last_sync_never_moves_backwards(fixtures) -> None:
    """A fetch time that predates a sync the app completed meanwhile must not
    roll the stamp back."""
    server_mod, _remote, _seed = fixtures
    assert await server_mod.record_last_sync("v", at=2_000) == 2_000
    assert await server_mod.record_last_sync("v", at=1_000) == 2_000
    assert (await server_mod.read_settings())["lastSync"]["v"] == 2_000


@pytest.mark.asyncio
async def test_a_future_fetch_time_is_clamped_to_now(fixtures) -> None:
    """The stamp comes from an mtime the agent can set; a future one would read
    "just now" forever and never move when syncs stop."""
    server_mod, _remote, _seed = fixtures
    far_future = int(time.time() * 1000) + 10**10
    stamped = await server_mod.record_last_sync("v", at=far_future)
    assert stamped <= int(time.time() * 1000)
    assert (await server_mod.read_settings())["lastSync"]["v"] == stamped


# ---------------------------------------------------------------------------
# Settings store and the server-side auto-sync loop
# ---------------------------------------------------------------------------


def _write_raw_settings(server_mod, content: str) -> None:
    """Drop a literal ``settings.json`` in place, as a hand or agent edit would."""
    path = server_mod._settings_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _default_settings_body(server_mod) -> dict[str, Any]:
    return {
        "autoSync": False,
        "autoSyncMins": server_mod.DEFAULT_AUTO_SYNC_MINS,
        "lastSync": {},
    }


async def _noop_rebuild(vault: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _no_auth() -> Optional[str]:
    """Stand in for ``resolve_auth`` so no test mints a token from the real gh."""
    return None


def _clean_result() -> dict[str, Any]:
    return {"pushed": True, "pulled": True, "committed": [], "conflicts": []}


@pytest.fixture
def loop_syncer(monkeypatch: pytest.MonkeyPatch):
    """The syncer module with a fast tick and a compressed minute.

    Only the two SCALES are patched, never the schedule arithmetic under test:
    ``TICK_SEC`` is how often settings are re-read, ``SECONDS_PER_MINUTE`` is the
    unit the stored interval is expressed in. Compressing both keeps the real
    ratio between the shortest selectable interval (1 minute) and the longest
    (1440) intact, so the interval assertions below hold with a wide margin
    instead of depending on the scheduler's cooperation.
    """
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    monkeypatch.setattr(syncer_mod, "TICK_SEC", 0.005)
    monkeypatch.setattr(syncer_mod, "SECONDS_PER_MINUTE", 0.005)
    return syncer_mod


@asynccontextmanager
async def running_syncer(syncer_mod) -> AsyncIterator[None]:
    """Arm the loop and guarantee it is cancelled, even when the test fails.

    A loop left running leaks a task into the next test's event loop, which
    surfaces as some unrelated test failing rather than this one.
    """
    await syncer_mod.start_syncer(web.Application())
    try:
        yield
    finally:
        await syncer_mod.stop_syncer(web.Application())


@pytest.mark.asyncio
async def test_settings_default_when_the_file_is_absent(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"] == _default_settings_body(server_mod)
        # autoSync defaults OFF, and that default is the authorization boundary:
        # enabling it is what lets the backend push to a remote unattended.
        assert body["settings"]["autoSync"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent,expected",
    [(0, 1), (-30, 1), (1, 1), (10, 10), (1440, 1440), (99999, 1440)],
)
async def test_settings_put_clamps_the_interval(fixtures, sent: int, expected: int) -> None:
    """Out-of-range minutes are clamped, and 0 is the one that matters: an
    unclamped 0 would make the scheduler a busy loop pushing continuously."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/settings", {"autoSyncMins": sent})
        assert status == 200, body
        assert body["settings"]["autoSyncMins"] == expected
        # The clamped value is what was PERSISTED, not just what was echoed.
        status, body = await client.get("/api/settings")
        assert body["settings"]["autoSyncMins"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["soon", "10", None, True, 10.5, [], {}])
async def test_settings_put_rejects_a_non_integer_interval(fixtures, sent: Any) -> None:
    """A value that is not a whole number of minutes carries no interval to
    clamp, so it is refused rather than silently replaced with a default."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/settings", {"autoSyncMins": sent})
        assert status == 400, body
        assert body["code"] == "invalid_auto_sync_mins"
        status, body = await client.get("/api/settings")
        assert body["settings"]["autoSyncMins"] == server_mod.DEFAULT_AUTO_SYNC_MINS


@pytest.mark.asyncio
async def test_settings_put_rejects_a_non_boolean_auto_sync(fixtures) -> None:
    """A truthy string must not enable unattended pushing."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/settings", {"autoSync": "yes"})
        assert status == 400, body
        assert body["code"] == "invalid_auto_sync"
        status, body = await client.get("/api/settings")
        assert body["settings"]["autoSync"] is False


@pytest.mark.asyncio
async def test_settings_put_drops_unknown_keys(fixtures) -> None:
    """A stale or hostile client must not be able to grow the file."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put(
            "/api/settings", {"autoSync": True, "pushAnywhere": True, "vaults": []}
        )
        assert status == 200, body
        assert body["settings"] == {
            "autoSync": True,
            "autoSyncMins": server_mod.DEFAULT_AUTO_SYNC_MINS,
            "lastSync": {},
        }
        stored = json.loads(server_mod._settings_json().read_text(encoding="utf-8"))
        assert set(stored) == {"autoSync", "autoSyncMins", "lastSync"}


@pytest.mark.asyncio
async def test_settings_writes_apply_in_arrival_order(fixtures) -> None:
    """Ordering is the server's job, done by arrival order under the settings
    lock — the client sends no ordering token. The last write the server accepts
    wins, and a disable always applies, so auto-sync can never stay authorized
    against a user (or another tab) that turned it off."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/settings", {"autoSync": True})
        assert status == 200, body
        assert body["settings"]["autoSync"] is True
        # A disable is never dropped — revocation always applies.
        status, body = await client.put("/api/settings", {"autoSync": False})
        assert status == 200, body
        assert body["settings"]["autoSync"] is False
        _, get_body = await client.get("/api/settings")
        assert get_body["settings"]["autoSync"] is False
        # The last write the server accepts wins.
        status, body = await client.put("/api/settings", {"autoSync": True})
        assert status == 200, body
        assert body["settings"]["autoSync"] is True


@pytest.mark.asyncio
async def test_auto_sync_permission_flip_is_audited(fixtures) -> None:
    """Enabling autoSync authorizes unattended `git push`; the permission flip
    MUST leave a SEL record. A no-op re-write (True->True) writes nothing."""
    from kiro_crew.sel import sel

    server_mod, _remote, _seed = fixtures
    fingerprint = "autoSyncMins=1439"  # unique to this test, so it isolates its events

    def flip_outcomes() -> list[str]:
        return [
            e.get("outcome")
            for e in sel().recent(limit=1000)
            if e.get("operation") == "md_notebook.settings.autoSync"
            and fingerprint in (e.get("resources") or "")
        ]

    async with signed_client(server_mod) as client:
        await client.put("/api/settings", {"autoSync": True, "autoSyncMins": 1439})
        await client.put("/api/settings", {"autoSync": True})  # no change -> no event
        await client.put("/api/settings", {"autoSync": False})
    outcomes = flip_outcomes()
    assert outcomes.count("enabled") == 1, outcomes
    assert outcomes.count("disabled") == 1, outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '"autoSync"',
        "null",
        "42",
        "{ not json at all",
        '{"autoSync": "yes", "autoSyncMins": "ten", "lastSync": 7}',
    ],
)
async def test_a_corrupt_settings_file_degrades_to_defaults(fixtures, raw: str) -> None:
    """The file sits in an agent-writable directory, so its shape AND its fields
    are untrusted — a broken document must degrade rather than 500 the endpoint,
    and a truthy-but-not-boolean autoSync must fail toward NOT pushing."""
    server_mod, _remote, _seed = fixtures
    _write_raw_settings(server_mod, raw)
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"] == _default_settings_body(server_mod)


# A hand edit saved in a legacy code page: valid JSON shape, but 0xE9 is not UTF-8.
_NON_UTF8_STATE = '{"autoSync": true, "note": "café"}'.encode("latin-1")


@pytest.mark.asyncio
async def test_a_non_utf8_settings_file_degrades_to_defaults(fixtures) -> None:
    """Undecodable bytes are as corrupt as malformed JSON: the read degrades to
    defaults (autoSync OFF) instead of raising out of the settings endpoint."""
    server_mod, _remote, _seed = fixtures
    path = server_mod._settings_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_NON_UTF8_STATE)
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"] == _default_settings_body(server_mod)


@pytest.mark.asyncio
async def test_a_non_utf8_vault_registry_reads_as_empty(fixtures) -> None:
    """vaults.json shares the settings file's read contract: undecodable bytes read
    as no vaults, the same as malformed JSON, so the app still opens."""
    server_mod, _remote, _seed = fixtures
    path = server_mod._vaults_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_NON_UTF8_STATE)
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/vaults")
        assert status == 200, body
        assert body["vaults"] == []


@pytest.mark.asyncio
async def test_a_mangled_last_sync_entry_does_not_cost_the_others(fixtures) -> None:
    """Per-entry filtering: one bad timestamp must not erase every vault's."""
    server_mod, _remote, _seed = fixtures
    _write_raw_settings(
        server_mod,
        json.dumps(
            {
                "lastSync": {
                    "good": 1700000000000,
                    "stringy": "nope",
                    "negative": -1,
                    "flag": True,
                }
            }
        ),
    )
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"]["lastSync"] == {"good": 1700000000000}


@pytest.mark.asyncio
async def test_settings_put_refuses_a_client_supplied_last_sync(fixtures) -> None:
    """lastSync is server-owned: a client sending one is claiming a sync
    happened, and accepting it would show a backup time nothing earned."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put(
            "/api/settings", {"autoSync": True, "lastSync": {"v1": 1700000000000}}
        )
        assert status == 400, body
        assert body["code"] == "last_sync_read_only"
        status, body = await client.get("/api/settings")
        assert body["settings"]["lastSync"] == {}
        # Refused outright, so the autoSync riding along in the same request
        # did not land either.
        assert body["settings"]["autoSync"] is False


@pytest.mark.asyncio
async def test_a_rejected_settings_request_changes_nothing(fixtures) -> None:
    """Validation is two-phase. This pair is why it matters: a request that
    enabled autoSync and then 400'd on the interval would leave unattended
    pushing on, from a request the user was told had failed."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put(
            "/api/settings", {"autoSync": True, "autoSyncMins": "whenever"}
        )
        assert status == 400, body
        assert body["code"] == "invalid_auto_sync_mins"
        status, body = await client.get("/api/settings")
        assert body["settings"]["autoSync"] is False


@pytest.mark.asyncio
async def test_create_app_wires_the_auto_sync_loop(fixtures) -> None:
    """The loop is armed by the backend app, not by a page: wired to a page's
    lifecycle it would stop the moment the Notes tab closed, which is the whole
    defect this replaces."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    app = server_mod.create_app()
    assert syncer_mod.start_syncer in app.on_startup
    assert syncer_mod.stop_syncer in app.on_cleanup


@pytest.mark.asyncio
async def test_the_loop_starts_and_stops_with_the_app(fixtures, loop_syncer) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod):
        task = loop_syncer._sync_task
        assert task is not None and not task.done()
    assert loop_syncer._sync_task is None
    assert task.done()


@pytest.mark.asyncio
async def test_starting_the_loop_twice_is_a_no_op(fixtures, loop_syncer) -> None:
    """A second arm must not create a second loop: two loops committing in one
    working tree race on index.lock and one of them loses its notes."""
    server_mod, _remote, _seed = fixtures
    async with running_syncer(loop_syncer):
        first = loop_syncer._sync_task
        await loop_syncer.start_syncer(web.Application())
        assert loop_syncer._sync_task is first


#: Loop time a "nothing synced" window spans at minimum, on top of its counted ticks:
#: 40 of the compressed one-minute intervals ``loop_syncer`` configures.
_QUIET_WINDOW_LOOP_SEC = 0.2


def _counted_ticks(server_mod, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the loop clock at each tick of the running sync loop.

    ``_sync_loop`` re-reads ``server.read_settings()`` exactly once per tick, and it
    makes that tick's decision in the same task step the read returns in. So when the
    test observes N entries, N decisions are complete: "nothing ran" is asserted over
    ticks that really happened, not over a stretch of wall time the loop may have spent
    starved. The real reader still runs, so the per-tick re-read stays exercised.
    """
    real = server_mod.read_settings
    ticks: list[float] = []

    async def counting() -> dict[str, Any]:
        settings = await real()
        ticks.append(asyncio.get_running_loop().time())
        return settings

    monkeypatch.setattr(server_mod, "read_settings", counting)
    return ticks


@pytest.mark.asyncio
async def test_the_loop_does_nothing_while_auto_sync_is_off(
    fixtures, loop_syncer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in gate. The second half is the control — without it, a loop that
    never ran at all would pass the first half just as well."""
    server_mod, _remote, _seed = fixtures
    cycles: list[int] = []

    async def _count(*_a: Any) -> None:
        cycles.append(1)

    monkeypatch.setattr(loop_syncer, "_sync_once", _count)
    ticks = _counted_ticks(server_mod, monkeypatch)
    _write_raw_settings(server_mod, json.dumps({"autoSync": False, "autoSyncMins": 1}))
    started = asyncio.get_running_loop().time()
    async with running_syncer(loop_syncer):
        # Many ticks spanning many (compressed) one-minute intervals: without the gate
        # each of those intervals would have produced a push.
        # flake-ok: pre-existing real loop-clock window the schedule itself reads, moved verbatim; tracked for a de-flake pass
        await _wait_for(
            lambda: len(ticks) >= 10 and ticks[-1] - started >= _QUIET_WINDOW_LOOP_SEC,
            "the loop never ticked through the quiet window with autoSync off",
            _BARRIER_LOST_RUN_SEC,
        )
        assert cycles == []

        _write_raw_settings(server_mod, json.dumps({"autoSync": True, "autoSyncMins": 1}))
        await _wait_for(lambda: cycles, "enabling autoSync never produced a sync cycle")


@pytest.mark.asyncio
async def test_the_loop_picks_up_a_changed_interval_without_a_restart(
    fixtures, loop_syncer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings.json is re-read every cycle, so shortening the interval takes
    effect on the running loop instead of at the next gateway start."""
    server_mod, _remote, _seed = fixtures
    cycles: list[float] = []

    async def _count(*_a: Any) -> None:
        cycles.append(asyncio.get_running_loop().time())

    monkeypatch.setattr(loop_syncer, "_sync_once", _count)
    ticks = _counted_ticks(server_mod, monkeypatch)
    # The longest selectable interval: nothing is due for a compressed day.
    _write_raw_settings(server_mod, json.dumps({"autoSync": True, "autoSyncMins": 1440}))
    interval = 1440 * loop_syncer.SECONDS_PER_MINUTE
    started = asyncio.get_running_loop().time()
    async with running_syncer(loop_syncer):
        # flake-ok: pre-existing real loop-clock window the schedule itself reads, moved verbatim; tracked for a de-flake pass
        await _wait_for(
            lambda: len(ticks) >= 10 and ticks[-1] - started >= _QUIET_WINDOW_LOOP_SEC,
            "the loop never ticked through the quiet window",
            _BARRIER_LOST_RUN_SEC,
        )
        # Judged on the loop clock the schedule itself reads: on a starved runner the
        # counted ticks can outlast the compressed interval, and a sync after it is due.
        # flake-ok: pre-existing real loop-clock window the schedule itself reads, moved verbatim; tracked for a de-flake pass
        early = [round(t - started, 3) for t in cycles if t - started < interval]
        assert early == [], f"synced {early}s into a {interval:.1f}s interval ({len(ticks)} ticks)"

        # Shortened while the loop runs — no restart of the loop or the gateway.
        before = len(cycles)
        _write_raw_settings(server_mod, json.dumps({"autoSync": True, "autoSyncMins": 1}))
        await _wait_for(lambda: len(cycles) > before, "a shortened interval never took effect")


@pytest.mark.asyncio
async def test_a_raising_vault_does_not_stop_the_others(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-vault containment: an unreachable remote costs that vault one cycle,
    not auto-sync for every other vault until the gateway restarts."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults(
        [
            {"id": "boom", "localPath": "/vaults/boom"},
            {"id": "fine", "localPath": "/vaults/fine"},
        ]
    )
    synced: list[str] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        if local_path == "/vaults/boom":
            raise git_ops.GitError("remote unreachable")
        synced.append(local_path)
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    assert synced == ["/vaults/fine"]
    settings = await server_mod.read_settings()
    assert list(settings["lastSync"]) == ["fine"]


@pytest.mark.asyncio
async def test_the_loop_skips_read_only_vaults(fixtures, monkeypatch: pytest.MonkeyPatch) -> None:
    """sync commits, merges and pushes — all writes — so a read-only vault must
    not reach it, exactly as on the manual path."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults(
        [
            {"id": "ro", "localPath": "/vaults/ro", "readOnly": True},
            {"id": "rw", "localPath": "/vaults/rw"},
        ]
    )
    synced: list[str] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        synced.append(local_path)
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    assert synced == ["/vaults/rw"]
    assert list((await server_mod.read_settings())["lastSync"]) == ["rw"]


@pytest.mark.asyncio
async def test_a_vault_forgotten_mid_cycle_is_skipped(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cycle snapshots the vault list at the top; a vault the user FORGETS
    mid-cycle has had its authorization revoked and must not be pushed from the
    stale snapshot. It is skipped while the still-connected vaults sync."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    full = [
        {"id": "a", "localPath": "/vaults/a"},
        {"id": "b", "localPath": "/vaults/b"},
        {"id": "c", "localPath": "/vaults/c"},
    ]
    reduced = [full[0], full[2]]  # 'b' forgotten after the cycle's opening snapshot
    calls = {"n": 0}

    async def _read_vaults() -> list[dict[str, Any]]:
        calls["n"] += 1
        # First call is the cycle's opening snapshot (sees all three); every
        # per-vault live re-read afterwards sees 'b' gone.
        return full if calls["n"] == 1 else reduced

    synced: list[str] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        synced.append(local_path)
        return _clean_result()

    monkeypatch.setattr(server_mod, "read_vaults", _read_vaults)
    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    # 'b' is skipped; 'a' and 'c' — still in the live registry — are pushed.
    assert synced == ["/vaults/a", "/vaults/c"]


@pytest.mark.asyncio
async def test_a_mid_cycle_revocation_stops_the_remaining_vaults(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cycle pushes every vault in turn; disabling auto-sync mid-cycle must stop
    the run rather than finish pushing the rest against a revoked authorization."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults(
        [
            {"id": "a", "localPath": "/vaults/a"},
            {"id": "b", "localPath": "/vaults/b"},
            {"id": "c", "localPath": "/vaults/c"},
        ]
    )
    synced: list[str] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        synced.append(local_path)
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    # Authorized for the first vault, revoked before the second — the gate the
    # loop passes in. The run must push 'a' and then stop, never reaching 'b'/'c'.
    calls = {"n": 0}

    async def _still_enabled() -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    await syncer_mod._sync_once(_still_enabled)

    assert synced == ["/vaults/a"]


@pytest.mark.asyncio
async def test_the_loop_passes_the_anti_tamper_pins(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unattended path must carry the same remote/gitdir pins as the manual
    one. Nobody is watching this run, so a repointed remote that was not refused
    would send the user's notes somewhere new with no step to intervene at."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    vault = {
        "id": "v1",
        "localPath": "/vaults/v1",
        "branch": "main",
        "subfolder": "notes",
        "remoteUrl": "https://example.invalid/repo.git",
        "gitDir": "/vaults/v1/.git",
        "localOnly": False,
    }
    await server_mod.write_vaults([vault])
    seen: list[dict[str, Any]] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        seen.append({"localPath": local_path, **kwargs})
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    assert seen == [
        {
            "localPath": "/vaults/v1",
            "branch": "main",
            "pat": None,
            "subfolder": "notes",
            "trusted_remote": "https://example.invalid/repo.git",
            "trusted_gitdir": "/vaults/v1/.git",
            "local_only": False,
            # A timer chose this run, so it stages notes ONLY even though it
            # pushes — a stray non-note file must not reach the remote unasked.
            "notes_only": True,
        }
    ]


@pytest.mark.asyncio
async def test_conflicts_do_not_record_a_sync_time(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflicted merge was aborted and pushed nothing, so stamping it would
    tell the user their notes are backed up when they are still only local."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults([{"id": "v1", "localPath": "/vaults/v1"}])

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "pushed": False,
            "pulled": False,
            "committed": [],
            "conflicts": [{"path": "One.md", "local": "a", "remote": "b"}],
        }

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    assert (await server_mod.read_settings())["lastSync"] == {}


@pytest.mark.asyncio
async def test_a_cycle_never_overlaps_the_previous_one(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent commits in one repository race on index.lock, and the
    loser drops that cycle's notes from history."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults([{"id": "v1", "localPath": "/vaults/v1"}])
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def _slow_sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        started.set()
        # flake-ok: pre-existing unbounded Event wait inside the fake; the test sets the event itself, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _slow_sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    first = asyncio.create_task(syncer_mod._sync_once())
    # flake-ok: pre-existing unbounded Event wait; the cycle task this test started sets it, moved verbatim; tracked for a de-flake pass
    await started.wait()
    # A second entrant while the first cycle is mid-flight returns without
    # touching the vault.
    await syncer_mod._sync_once()
    assert calls == [1]
    release.set()
    # flake-ok: pre-existing unbounded await of the cycle task released above, moved verbatim; tracked for a de-flake pass
    await first
    assert calls == [1]


@pytest.mark.asyncio
async def test_manual_sync_records_and_returns_the_sync_time(fixtures) -> None:
    """Deliverable of the single-writer rule: the handler stamps lastSync and
    hands it back, so the UI updates without a second round-trip."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body
        stamped = body["lastSync"]
        assert isinstance(stamped, int) and stamped > 0

        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"]["lastSync"][vault["id"]] == stamped


@pytest.mark.asyncio
async def test_a_conflicted_manual_sync_reports_no_sync_time(fixtures) -> None:
    """Same rule on the manual path: a conflict earns no timestamp."""
    server_mod, remote, seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        (seed / "One.md").write_text("# One\n\nREMOTE side\n", encoding="utf-8")
        _git("add", "-A", cwd=seed)
        _git("commit", "-m", "remote", cwd=seed)
        _git("push", "origin", "main", cwd=seed)
        await client.put("/api/note", {"path": "One.md", "content": "# One\n\nLOCAL side\n"})

        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body
        assert body["result"]["conflicts"], "expected a conflicted sync to test"
        assert body["lastSync"] is None

        status, body = await client.get("/api/settings")
        assert body["settings"]["lastSync"] == {}
