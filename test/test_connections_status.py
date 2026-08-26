"""Tests for the Connections authorization-status feed and cancel endpoint.

The status module is the AUTHORIZATION axis (grant presence + a persisted
first-connect time); it never probes reachability and never mints. The cancel
endpoint disposes an in-flight mint through the mint engine's ownership API
without touching MCP config. Both are additive to main's mint contract.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_grant
from kiro_crew.connections import mint, status
from kiro_crew.dashboard.handlers import connections

_NOTION = {"slug": "notion", "mcp_url": "https://mcp.notion.com/mcp"}
_LINEAR = {"slug": "linear", "mcp_url": "https://mcp.linear.app/mcp"}


#: The real function, captured before any test patches it, for tests that need
#: genuine path resolution against a monkeypatched cache directory.
_REAL_GRANT_ARTIFACT_PATHS = mcp_grant.grant_artifact_paths

#: A real regular file's stat result, served by the fake artifacts below.
_REGULAR_FILE_STAT = Path(__file__).stat()


class _FakeArtifact:
    """A grant artifact whose stat answers from the mutable ``_grants`` set."""

    def __init__(self, url: str) -> None:
        self._url = url

    def stat(self):
        if self._url in _grants:
            return _REGULAR_FILE_STAT
        raise FileNotFoundError(self._url)


def _install_grants_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the status probe's artifact stats through the ``_grants`` set."""
    monkeypatch.setattr(
        mcp_grant,
        "grant_artifact_paths",
        lambda url, **kw: (_FakeArtifact(url), _FakeArtifact(url)),
    )


@pytest.fixture()
def isolated_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A scratch sidecar, a fixed visible-provider set, and controllable facts.

    ``grant_artifact_paths`` is reached through its module and
    ``pending_mint_for`` is imported per call, so each defining module stays
    patchable.
    """
    monkeypatch.setattr(status, "_CONNECTION_STATE_PATH", tmp_path / "connected-since.json")
    monkeypatch.setattr(status, "_UNPERSISTED_RECORD", None)
    monkeypatch.setattr(status, "get_visible_providers", lambda: [dict(_NOTION), dict(_LINEAR)])
    _install_grants_fake(monkeypatch)
    monkeypatch.setattr(mint, "pending_mint_for", lambda slug: _mint_rows.get(slug))
    # A scratch artifact directory for tests that restore the REAL path
    # resolution; with the grants fake installed it is not consulted.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(mcp_grant, "kiro_oauth_cache_dir", lambda **kw: cache_dir)
    _set_facts()  # start each test from empty, controllable facts
    return tmp_path


# Mutable facts the fixture reads; reset per test by reassigning in the test body.
_grants: set[str] = set()
_mint_rows: dict[str, dict] = {}


def _set_facts(granted: set[str] | None = None, rows: dict[str, dict] | None = None) -> None:
    _grants.clear()
    _grants.update(granted or set())
    _mint_rows.clear()
    _mint_rows.update(rows or {})


def _by_slug(statuses: list[status.ConnectionStatus]) -> dict[str, status.ConnectionStatus]:
    return {entry["slug"]: entry for entry in statuses}


# ── classification (pure) ──


def test_classify_covers_the_three_authorization_outcomes():
    assert status._classify(True, "") == (status.STATUS_CONNECTED, "grant_present")
    assert status._classify(False, "waiting") == (status.STATUS_AWAITING_CONSENT, "mint_in_flight")
    assert status._classify(False, "minting") == (status.STATUS_AWAITING_CONSENT, "mint_in_flight")
    assert status._classify(False, "") == (status.STATUS_NOT_CONNECTED, "no_grant")
    # A terminal mint state without a grant is not "awaiting" anything.
    assert status._classify(False, "failed") == (status.STATUS_NOT_CONNECTED, "no_grant")


# ── collect_connection_statuses ──


@pytest.mark.asyncio
async def test_a_grant_stamps_connected_and_records_connected_since(isolated_status):
    _set_facts(granted={_NOTION["mcp_url"]})

    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["notion"]["status"] == status.STATUS_CONNECTED
    assert statuses["notion"]["grantPresent"] is True
    assert statuses["notion"].get("connectedSince")  # stamped
    assert statuses["linear"]["status"] == status.STATUS_NOT_CONNECTED
    assert statuses["linear"]["grantPresent"] is False
    assert "connectedSince" not in statuses["linear"]
    # Persisted, so the timestamp survives the next read.
    saved = json.loads((isolated_status / "connected-since.json").read_text(encoding="utf-8"))
    assert "notion" in saved["providers"]


@pytest.mark.asyncio
async def test_awaiting_consent_when_a_mint_is_in_flight_without_a_grant(isolated_status):
    _set_facts(rows={"notion": {"state": "waiting", "token": "t"}})

    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["notion"]["status"] == status.STATUS_AWAITING_CONSENT
    assert statuses["notion"]["grantPresent"] is False


@pytest.mark.asyncio
async def test_connected_since_is_stable_across_reads(isolated_status):
    _set_facts(granted={_NOTION["mcp_url"]})

    first = _by_slug(await status.collect_connection_statuses())["notion"]["connectedSince"]
    second = _by_slug(await status.collect_connection_statuses())["notion"]["connectedSince"]

    assert first == second  # stamped once, not re-stamped each read


@pytest.mark.asyncio
async def test_connected_since_is_pruned_when_the_grant_disappears(isolated_status):
    _set_facts(granted={_NOTION["mcp_url"]})
    await status.collect_connection_statuses()  # stamps notion
    _set_facts(granted=set())  # grant revoked

    statuses = _by_slug(await status.collect_connection_statuses())

    assert "connectedSince" not in statuses["notion"]
    saved = json.loads((isolated_status / "connected-since.json").read_text(encoding="utf-8"))
    assert saved["providers"] == {}  # self-healed


@pytest.mark.asyncio
async def test_no_visible_providers_prunes_stale_records(isolated_status, monkeypatch):
    (isolated_status / "connected-since.json").write_text(
        json.dumps({"schema_version": 1, "providers": {"gone": {"connected_since": "x"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(status, "get_visible_providers", lambda: [])

    assert await status.collect_connection_statuses() == []
    saved = json.loads((isolated_status / "connected-since.json").read_text(encoding="utf-8"))
    assert saved["providers"] == {}


def test_a_damaged_sidecar_reads_as_empty(isolated_status):
    (isolated_status / "connected-since.json").write_text("{not json", encoding="utf-8")
    assert status._load_connected_since() == {}


def test_an_unreadable_sidecar_reads_as_unknowable_not_empty(isolated_status):
    """A directory at the record's path raises a non-ENOENT ``OSError`` on read
    (IsADirectoryError on POSIX, PermissionError on Windows) -- the same class
    as EACCES/EIO on a real file. That is "could not look", never "empty"."""
    (isolated_status / "connected-since.json").mkdir()
    assert status._load_connected_since() is None


def test_an_unknowable_baseline_aborts_the_reconcile_without_output_or_write(
    isolated_status, monkeypatch
):
    """An unreadable record can sit in a perfectly writable directory, so a
    reconcile that assumed it empty would rebuild from a blank baseline and the
    atomic replace would OVERWRITE every persisted timestamp. With no baseline
    there is no output and no write."""
    monkeypatch.setattr(status, "_load_connected_since", lambda: None)

    def _never(recorded):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("reconcile must not write over a record it could not read")

    monkeypatch.setattr(status, "_save_connected_since", _never)
    entry: status.ConnectionStatus = {
        "slug": "notion",
        "status": status.STATUS_CONNECTED,
        "reason": "grant_present",
        "grantPresent": True,
    }
    assert status.reconcile_connected_since([entry], "2026-08-21T00:00:00+00:00") == {}


# ── indeterminate grant lookup ──


def _unreadable_cache_dir(monkeypatch: pytest.MonkeyPatch, errno_cls=PermissionError) -> None:
    """Make every grant-artifact stat fail the way EACCES/EIO does.

    ``Path.is_file()`` already swallows OSError and answers False, so a broken
    mount reaches the status module as "absent" -- this reproduces exactly that,
    plus the artifact stat failing, which is what makes it distinguishable.
    """

    class _UnreadableArtifact:
        def stat(self, *a, **kw):
            raise errno_cls("stat failed")

        def is_file(self) -> bool:
            return False  # what Path.is_file() answers when its stat raises

    monkeypatch.setattr(
        mcp_grant,
        "grant_artifact_paths",
        lambda url, **kw: (_UnreadableArtifact(), _UnreadableArtifact()),
    )


@pytest.mark.asyncio
async def test_a_transient_stat_failure_keeps_the_recorded_timestamp(isolated_status, monkeypatch):
    _set_facts(granted={_NOTION["mcp_url"]})
    stamped = _by_slug(await status.collect_connection_statuses())["notion"]["connectedSince"]

    # The grant is still there; the artifact directory just cannot be read.
    _set_facts(granted=set())
    _unreadable_cache_dir(monkeypatch)
    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["notion"]["grantIndeterminate"] is True
    assert statuses["notion"]["grantPresent"] is False  # never claims authorization
    assert statuses["notion"]["reason"] == "grant_unreadable"
    # The record survives: "could not look" is not evidence of absence.
    assert statuses["notion"]["connectedSince"] == stamped
    saved = json.loads((isolated_status / "connected-since.json").read_text(encoding="utf-8"))
    assert saved["providers"]["notion"]["connected_since"] == stamped


@pytest.mark.asyncio
async def test_the_original_timestamp_survives_the_outage_and_recovers(
    isolated_status, monkeypatch
):
    _set_facts(granted={_NOTION["mcp_url"]})
    stamped = _by_slug(await status.collect_connection_statuses())["notion"]["connectedSince"]

    _unreadable_cache_dir(monkeypatch)
    _set_facts(granted=set())
    await status.collect_connection_statuses()

    # The mount comes back with the grant intact: the ORIGINAL clock continues
    # rather than restarting at the recovery moment.
    _install_grants_fake(monkeypatch)
    _set_facts(granted={_NOTION["mcp_url"]})
    recovered = _by_slug(await status.collect_connection_statuses())["notion"]

    assert recovered["status"] == status.STATUS_CONNECTED
    assert recovered["connectedSince"] == stamped


@pytest.mark.asyncio
async def test_an_indeterminate_lookup_never_stamps_a_new_timestamp(isolated_status, monkeypatch):
    # Nothing recorded and nothing observable: a non-observation must not invent a
    # connected-since for a provider that may never have been authorized.
    _unreadable_cache_dir(monkeypatch)
    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["notion"]["grantIndeterminate"] is True
    assert "connectedSince" not in statuses["notion"]


@pytest.mark.asyncio
async def test_a_true_absence_still_prunes(isolated_status):
    _set_facts(granted={_NOTION["mcp_url"]})
    await status.collect_connection_statuses()

    # Readable directory, artifacts gone: a definitive answer, so the record goes.
    _set_facts(granted=set())
    statuses = _by_slug(await status.collect_connection_statuses())

    assert "grantIndeterminate" not in statuses["notion"]
    assert statuses["notion"]["reason"] == "no_grant"
    assert "connectedSince" not in statuses["notion"]


def test_a_missing_artifact_directory_is_a_definitive_absence(isolated_status, monkeypatch):
    # ENOENT means no grant was ever written — an answer, not an error. Real
    # path resolution restored so this exercises the genuine ENOENT branch.
    monkeypatch.setattr(mcp_grant, "grant_artifact_paths", _REAL_GRANT_ARTIFACT_PATHS)
    monkeypatch.setattr(
        mcp_grant,
        "kiro_oauth_cache_dir",
        lambda **kw: isolated_status / "absent",
    )
    assert status._grant_presence_map([dict(_NOTION)]) == {"notion": False}


def test_pair_semantics_from_one_stat_pass(isolated_status, monkeypatch):
    """Both artifacts are stat-ed exactly once and the pair combines: a
    definitive absence of EITHER decides the pair (both must exist), while an
    unreadable stat with no definitive absence is unknowable."""

    class _Boom:
        def stat(self):
            raise PermissionError("EACCES")

    class _Absent:
        def stat(self):
            raise FileNotFoundError()

    class _Present:
        def stat(self):
            return _REGULAR_FILE_STAT

    def probe(pair):
        monkeypatch.setattr(mcp_grant, "grant_artifact_paths", lambda url, **kw: pair)
        return status._provider_grant_presence("https://mcp.notion.com/mcp")

    assert probe((_Boom(), _Absent())) is False  # absence decides the pair
    assert probe((_Boom(), _Present())) is None  # could still be True: unknowable
    assert probe((_Present(), _Present())) is True


@pytest.mark.asyncio
async def test_a_stamp_that_cannot_persist_is_not_published(isolated_status, monkeypatch):
    """A read-only home must not fabricate connected-since: an in-memory stamp
    the next read cannot reproduce would re-date the connection every poll."""
    from kiro_crew import agent, hooks

    def _read_only(*a, **kw):
        raise OSError("read-only home")

    monkeypatch.setattr(agent, "_atomic_json_write", _read_only)
    audits: list[str] = []
    monkeypatch.setattr(
        hooks,
        "emit_internal_read_audit",
        lambda read_id, outcome: (audits.append(read_id), True)[1],
    )

    _set_facts(granted={_NOTION["mcp_url"]})
    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["notion"]["status"] == status.STATUS_CONNECTED
    assert "connectedSince" not in statuses["notion"]
    # Nothing was stamped, so there is no acted-on observation to audit.
    assert audits == []


# ── the acted-on observation is SEL-audited ──


def test_the_status_read_id_is_registered_with_the_audit_gate():
    """``emit_internal_read_audit`` fail-closes on an unregistered read_id, and
    the audit tests above monkeypatch the hook, so only this un-mocked check can
    catch a registration gap that silently disables the audit."""
    from kiro_crew import hooks

    assert status._GRANT_PRESENCE_READ_ID in hooks._AUDIT_ONLY_READ_IDS


@pytest.mark.asyncio
async def test_a_first_observed_grant_is_sel_audited_once(isolated_status, monkeypatch):
    """Stamping a first-connect time is the credential-store observation this
    module acts on; it owes exactly one trail entry, not one per poll sweep."""
    from kiro_crew import hooks

    calls: list[str] = []
    monkeypatch.setattr(
        hooks,
        "emit_internal_read_audit",
        lambda read_id, outcome: (calls.append(read_id), True)[1],
    )

    _set_facts(granted={_NOTION["mcp_url"]})
    await status.collect_connection_statuses()
    assert calls == ["connections_status.oauth_grant_presence"]

    # The next sweep observes the SAME grant: no new stamp, no audit flood.
    await status.collect_connection_statuses()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_unrecordable_audit_never_fails_the_status_read(
    isolated_status, monkeypatch, caplog
):
    """Best-effort like the mint engine's convention: nothing sensitive crosses
    this boundary (stats only), so an SEL outage warns instead of denying."""
    import logging

    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "emit_internal_read_audit", lambda *a, **kw: False)
    _set_facts(granted={_NOTION["mcp_url"]})
    with caplog.at_level(logging.WARNING):
        statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["notion"]["status"] == status.STATUS_CONNECTED
    assert "unaudited" in caplog.text


def test_overlapping_reconciles_serialize_and_the_second_carries_the_first_stamp(
    isolated_status, monkeypatch
):
    """Two concurrent polls (multiple dashboard tabs) must not interleave the
    load-modify-write: unserialized, both load the empty record and each returns
    its OWN clock -- the loser's write re-dates the winner's first-connect stamp.
    The barrier below forces exactly that interleaving whenever both threads can
    sit inside the critical section together, so this test fails without the
    module lock and passes deterministically with it."""
    import threading as _threading

    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "emit_internal_read_audit", lambda *a, **kw: True)

    real_load = status._load_connected_since
    barrier = _threading.Barrier(2)

    def racing_load() -> dict[str, str]:
        # Passes only if BOTH threads are inside the critical section at once;
        # under the lock the second thread is still queued, the wait times out,
        # and the barrier breaks -- which is the serialized (correct) outcome.
        try:
            barrier.wait(timeout=0.4)
        except _threading.BrokenBarrierError:
            pass
        return real_load()

    monkeypatch.setattr(status, "_load_connected_since", racing_load)

    entry: status.ConnectionStatus = {
        "slug": "notion",
        "status": status.STATUS_CONNECTED,
        "reason": "grant_present",
        "grantPresent": True,
    }
    results: dict[str, dict[str, str]] = {}
    first = _threading.Thread(
        target=lambda: results.__setitem__(
            "a", status.reconcile_connected_since([entry], "2026-08-20T00:00:00+00:00")
        )
    )
    second = _threading.Thread(
        target=lambda: results.__setitem__(
            "b", status.reconcile_connected_since([entry], "2026-08-20T00:00:01+00:00")
        )
    )
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    # The reconcile that ran second must OBSERVE the first's stamp and carry it,
    # not stamp its own clock over it: both report the identical timestamp, and
    # that timestamp is what a later read serves.
    assert results["a"] == results["b"]
    assert real_load() == results["a"]
    assert results["a"]["notion"] in ("2026-08-20T00:00:00+00:00", "2026-08-20T00:00:01+00:00")


def _entry(slug: str, *, present: bool | None) -> "status.ConnectionStatus":
    """A reconcile input: present, confirmed absent, or indeterminate."""
    made: status.ConnectionStatus = {
        "slug": slug,
        "status": status.STATUS_CONNECTED if present else status.STATUS_NOT_CONNECTED,
        "reason": "grant_present" if present else "no_grant",
        "grantPresent": present is True,
    }
    if present is None:
        made["grantIndeterminate"] = True
    return made


def test_a_failed_prune_never_resurrects_the_revoked_timestamp(isolated_status, monkeypatch):
    """The full failed-write class, all branches at once: a confirmed absence
    whose prune write fails must invalidate the stored timestamp EVERYWHERE --
    a re-authorization starts a fresh clock (never re-serves the pre-revocation
    date from the unpruned file), an indeterminate lookup carries nothing, and
    the first successful save persists the honest state and retires the memory
    baseline."""
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "emit_internal_read_audit", lambda *a, **kw: True)

    save_ok = {"value": True}
    real_save = status._save_connected_since
    monkeypatch.setattr(
        status, "_save_connected_since", lambda rec: save_ok["value"] and real_save(rec)
    )

    # Seed persisted truth: notion first connected at T0.
    t0 = "2026-08-13T00:00:00+00:00"
    assert status.reconcile_connected_since([_entry("notion", present=True)], t0) == {"notion": t0}

    # The grant is revoked, but the sidecar has become unwritable: the prune
    # cannot land, so the FILE still carries T0.
    save_ok["value"] = False
    assert status.reconcile_connected_since([_entry("notion", present=False)], "x") == {}
    assert status._load_connected_since() == {"notion": t0}

    # An indeterminate lookup must not carry the invalidated value back.
    assert status.reconcile_connected_since([_entry("notion", present=None)], "x") == {}

    # Re-authorization while still unwritable: the fresh stamp cannot persist,
    # so nothing is reported -- and CRUCIALLY the answer is not T0.
    t2 = "2026-08-20T00:00:00+00:00"
    assert status.reconcile_connected_since([_entry("notion", present=True)], t2) == {}

    # The write path recovers: the next observation stamps a fresh clock, the
    # honest record reaches disk, and the stale T0 is gone for good.
    save_ok["value"] = True
    t3 = "2026-08-20T00:05:00+00:00"
    assert status.reconcile_connected_since([_entry("notion", present=True)], t3) == {"notion": t3}
    assert status._load_connected_since() == {"notion": t3}
    assert status._UNPERSISTED_RECORD is None


# ── HTTP surface ──


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_get("/api/connections/status", connections.api_connections_status)
    app.router.add_post("/api/connections/cancel", connections.api_connections_cancel)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_status_endpoint_serves_the_verdicts(isolated_status):
    _set_facts(granted={_NOTION["mcp_url"]})
    client = await _client()
    try:
        resp = await client.get("/api/connections/status")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["schema_version"] == status._STATUS_SCHEMA_VERSION
    verdicts = {entry["slug"]: entry for entry in body["connections"]}
    assert verdicts["notion"]["status"] == "connected"
    assert verdicts["notion"]["grantPresent"] is True


@pytest.mark.asyncio
async def test_cancel_endpoint_rejects_an_unknown_provider():
    client = await _client()
    try:
        resp = await client.post("/api/connections/cancel", json={"slug": "not-a-provider"})
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()
    assert body["code"] == "unknown_provider"


@pytest.mark.asyncio
async def test_cancel_endpoint_rejects_a_present_but_malformed_token():
    """A token that is present but empty or non-string is a malformed request,
    never the no-token privilege: coercing it to None would let a caller that
    failed to echo its row token dispose another tab's current mint."""
    client = await _client()
    try:
        for bad_token in ("", 123, {"nested": "x"}):
            resp = await client.post(
                "/api/connections/cancel", json={"slug": "notion", "token": bad_token}
            )
            assert resp.status == 400, f"token={bad_token!r} must be rejected"
            body = await resp.json()
            assert body["code"] == "invalid_token"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_endpoint_reports_not_dropped_without_a_live_mint(monkeypatch):
    # A real provider slug, but no row in the table -> idempotent no-op.
    monkeypatch.setattr(mint, "_mints", {})
    monkeypatch.setattr(mint, "_mints_lock", asyncio.Lock())
    client = await _client()
    try:
        resp = await client.post("/api/connections/cancel", json={"slug": "notion"})
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()
    assert body == {"ok": True, "slug": "notion", "dropped": False}
