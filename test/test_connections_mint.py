"""Tests for on-demand minting of a Connections provider's approval URL."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import hooks
from kiro_crew.connections import mint
from kiro_crew.dashboard.handlers import connections

_URL = "https://mcp.example.com/mcp"
_AUTHORIZE = (
    "https://auth.example.com/authorize?client_id=abc"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A43123%2Foauth%2Fcallback&state=opaque"
)


class _FakeClient:
    """Stand-in for AcpClient: records lifecycle, yields one OAuth challenge."""

    instances: list["_FakeClient"] = []
    next_pid = 424242

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.ready = False
        self.shutdowns = 0
        self.alive = True
        _FakeClient.next_pid += 1
        self._pid = _FakeClient.next_pid
        self.requests: list[dict[str, str]] = [
            {"serverName": "notion", "oauthUrl": _AUTHORIZE},
        ]
        _FakeClient.instances.append(self)

    async def ensure_ready(self) -> None:
        self.ready = True

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        out = list(self.requests)
        self.requests.clear()
        return out

    def is_process_alive(self) -> bool:
        return self.alive

    async def shutdown(self) -> None:
        self.shutdowns += 1


def _state_only(view: dict | None) -> dict:
    """The card-facing view minus the row token.

    The token is a correlation id, not part of the state contract, so assertions
    about what a state MEANS compare without it. Its presence has its own tests.
    """
    return {k: v for k, v in (view or {}).items() if k != "token"}


def _grant_reads_recorded(seen: list[int]) -> Any:
    """The real grant predicate, plus the id of the thread each read ran on."""
    real = mint.grant_present

    def recorded(url: str, **kw: Any) -> bool:
        seen.append(threading.get_ident())
        return real(url, **kw)

    return recorded


def _write_paired_grant_artifacts(mcp_url: str) -> None:
    """Land a grant kiro-cli would recognize in the scratch cache dir."""
    cache_dir = mint.kiro_oauth_cache_dir()
    key = mint.grant_key(mcp_url)
    (cache_dir / f"{key}.token.json").write_text("{}", encoding="utf-8")
    (cache_dir / f"{key}.registration.json").write_text("{}", encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_mint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Empty mint table, a scratch agents dir and grant cache, no real spawn."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": _URL}}}), encoding="utf-8"
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: agents_dir)
    monkeypatch.setattr(mint, "kiro_oauth_cache_dir", lambda **kw: cache_dir)
    # The manifest is real gateway state; tests must never write the live one.
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: tmp_path / "mint-specs.json")
    monkeypatch.setattr(mint, "_mints", {})
    monkeypatch.setattr(mint, "_mints_lock", asyncio.Lock())
    _FakeClient.instances.clear()
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _FakeClient)
    return agents_dir


@pytest.fixture()
def protected_pids(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """The sweep-protection set as this module drives it."""
    live: set[int] = set()
    monkeypatch.setattr(mint, "register_protected_pid", live.add)
    monkeypatch.setattr(mint, "unregister_protected_pid", live.discard)
    return live


# ── the mint session's shape ──


@pytest.mark.asyncio
async def test_a_successful_mint_holds_the_process_and_serves_the_url():
    await mint.start_oauth_mint("notion", _URL)

    view = mint.pending_mint_for("notion")
    assert _state_only(view) == {"state": "waiting", "oauth_url": _AUTHORIZE}
    client = _FakeClient.instances[-1]
    # Held, not disposed: the PKCE verifier and the loopback listener live here.
    assert client.shutdowns == 0
    await mint._dispose_mint(mint._mints["notion"])


def test_the_session_is_promptless_model_free_and_mounts_one_server():
    body = mint._mint_spec_body("kirocrew-mint-notion", {"notion": {"url": _URL}}, "desc")

    assert body["prompt"] == ""
    assert body["model"] == "auto"
    assert body["allowedTools"] == []
    assert body["includeMcpJson"] is False
    assert list(body["mcpServers"]) == ["notion"]
    assert body["tools"] == ["@notion"]


@pytest.mark.asyncio
async def test_the_mint_runs_on_a_dedicated_single_server_spec():
    await mint.start_oauth_mint("notion", _URL)

    agent = _FakeClient.instances[-1].kwargs["agent"]
    assert agent.startswith(f"kirocrew-mint-notion-{os.getpid()}-")
    await mint._dispose_mint(mint._mints["notion"])


# ── result matrix ──


@pytest.mark.asyncio
async def test_an_existing_grant_short_circuits_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(mint, "grant_present", lambda url, **kw: True)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    assert _FakeClient.instances == []


@pytest.mark.asyncio
async def test_a_server_that_never_challenges_is_granted_and_disposed(
    monkeypatch: pytest.MonkeyPatch,
):
    class _NoChallenge(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = []

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _NoChallenge)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    # Nothing to hold, so the process goes immediately.
    assert _FakeClient.instances[-1].shutdowns == 1


@pytest.mark.asyncio
async def test_a_challenge_for_another_server_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch,
):
    class _WrongServer(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = [{"serverName": "linear", "oauthUrl": _AUTHORIZE}]

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _WrongServer)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}


@pytest.mark.asyncio
async def test_a_failed_mint_records_a_coarse_reason_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Boom(_FakeClient):
        async def ensure_ready(self) -> None:
            raise TimeoutError("provider unreachable at 10.0.0.5")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _Boom)

    await mint.start_oauth_mint("notion", _URL)

    view = mint.pending_mint_for("notion")
    assert view is not None
    assert view["state"] == "failed"
    # Coarse code only: no provider- or exception-supplied text.
    assert view["reason"] == "mint_timeouterror"
    assert "10.0.0.5" not in json.dumps(view)
    assert _FakeClient.instances[-1].shutdowns == 1


@pytest.mark.asyncio
async def test_a_dead_holder_reports_expired_rather_than_serving_a_dead_url():
    await mint.start_oauth_mint("notion", _URL)
    _FakeClient.instances[-1].alive = False

    # The GET path commits the verdict first, so the view reports a state the
    # stored row actually has -- a URL whose verifier and listener are gone can
    # never be redeemed, and the card must fall back to idle Connect.
    await mint.expire_dead_holder("notion")
    view = mint.pending_mint_for("notion")

    assert _state_only(view) == {"state": "expired", "reason": "mint_process_gone"}
    assert view.get("oauth_url") is None


@pytest.mark.asyncio
async def test_the_view_never_reports_a_state_the_stored_row_does_not_have():
    # The holder is alive when expire_dead_holder probes, and dies before the view
    # is read. Probing a second time here would report expired while the row still
    # said waiting -- and the abandon fence, which only acts on a stored 'expired',
    # would then refuse every attempt to clean the entry up.
    await mint.start_oauth_mint("notion", _URL)
    holder = _FakeClient.instances[-1]

    await mint.expire_dead_holder("notion")  # alive: nothing to commit
    holder.alive = False

    view = mint.pending_mint_for("notion")
    assert view is not None
    assert view["state"] == mint._mints["notion"]["state"] == "waiting"

    # The next poll commits it, and only then does the fence accept the claim.
    await mint.expire_dead_holder("notion")
    assert mint.pending_mint_for("notion")["state"] == "expired"
    # The row is committed, not merely reported -- nothing deletes the entry.
    assert mint._mints["notion"]["state"] == "expired"


def test_no_mint_reads_as_absent():
    assert mint.pending_mint_for("notion") is None


@pytest.mark.asyncio
async def test_a_second_mint_supersedes_and_disposes_the_first():
    await mint.start_oauth_mint("notion", _URL)
    first = _FakeClient.instances[-1]

    await mint.start_oauth_mint("notion", _URL)
    second = _FakeClient.instances[-1]

    assert first is not second
    assert first.shutdowns == 1
    assert second.shutdowns == 0
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_dispose_releases_the_process_the_watcher_and_the_spec(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    removed: list[str] = []
    monkeypatch.setattr(mint, "_remove_mint_agent_spec", removed.append)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    watcher = entry["watcher"]
    spec_path = entry["spec_path"]
    assert protected_pids == {_FakeClient.instances[-1]._pid}

    await mint._dispose_mint(entry)

    assert _FakeClient.instances[-1].shutdowns == 1
    assert watcher.cancelled() or watcher.cancelling() > 0
    assert removed == [spec_path]
    assert protected_pids == set()
    assert "client" not in entry and "agent" not in entry


# ── the watcher disposing its own row ──


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["grant", "expiry"])
async def test_the_watcher_completes_teardown_when_it_disposes_its_own_row(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int], terminal: str
):
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    if terminal == "expiry":
        # Expiry is the one terminal this test does not trigger itself, so order it
        # structurally rather than by margin. The watcher always awaits a full poll
        # interval BEFORE it re-checks its deadline, so a poll longer than the TTL
        # parks it in that sleep for the whole of the synchronous row read below and
        # lets it take the expiry branch on its first wake. Reading ``spec_path`` off
        # a row that a concurrent teardown is free to strip is what made this flaky.
        monkeypatch.setattr(mint, "_MINT_TTL_SECONDS", 0.05)
        monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.25)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    client = _FakeClient.instances[-1]
    spec_path = Path(entry["spec_path"])
    assert spec_path.is_file()

    if terminal == "grant":
        monkeypatch.setattr(mint, "grant_present", lambda url, **kw: True)
    await asyncio.wait_for(entry["watcher"], timeout=5)

    # Cancelling the calling task would land the cancellation inside the client
    # teardown and abandon it, leaking the process tree and the listener.
    assert client.shutdowns == 1
    assert not spec_path.exists()
    assert protected_pids == set()
    assert mint._mints["notion"]["state"] == ("granted" if terminal == "grant" else "expired")
    assert "client" not in mint._mints["notion"]


@pytest.mark.asyncio
async def test_a_teardown_cancelled_from_outside_still_releases_the_spec_and_pid(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    class _HangingShutdown(_FakeClient):
        async def shutdown(self) -> None:
            self.shutdowns += 1
            await asyncio.Event().wait()

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _HangingShutdown)
    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    spec_path = Path(entry["spec_path"])
    assert protected_pids == {_FakeClient.instances[-1]._pid}

    task = asyncio.create_task(mint._dispose_mint(entry))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # A wedged teardown cancelled from outside must not leave the spec on disk
    # or the PID shielded from the sweep for the rest of the process's life.
    assert not spec_path.exists()
    assert protected_pids == set()


@pytest.mark.asyncio
async def test_a_watcher_never_writes_to_a_row_it_does_not_own(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    monkeypatch.setattr(mint, "grant_present", lambda url, **kw: True)
    live: mint.MintState = {
        "state": "waiting",
        "started": 2.0,
        "oauth_url": _AUTHORIZE,
        "token": "b" * 32,
    }
    mint._mints["notion"] = live

    # A watcher left over from a superseded flow: its token no longer names the
    # row the slug now points at, so it must leave that row alone.
    stale_token = "f" * 32
    assert live["token"] != stale_token
    await asyncio.wait_for(mint._mint_watcher("notion", _URL, stale_token), timeout=5)

    assert mint._mints["notion"] is live
    assert live["state"] == "waiting"


# ── the sweep-protected PID ──


@pytest.mark.asyncio
async def test_the_held_process_is_shielded_from_the_orphan_sweep(protected_pids: set[int]):
    await mint.start_oauth_mint("notion", _URL)

    # Nothing claims a mint as a session, so without this the periodic sweep
    # reaps it once it ages past the spawn grace -- inside the mint's own TTL.
    assert protected_pids == {_FakeClient.instances[-1]._pid}
    await mint._dispose_mint(mint._mints["notion"])
    assert protected_pids == set()


@pytest.mark.asyncio
async def test_a_failed_mint_releases_its_pid_protection(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    class _FailsAfterSpawn(_FakeClient):
        def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
            raise RuntimeError("drain exploded")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _FailsAfterSpawn)

    await mint.start_oauth_mint("notion", _URL)

    assert protected_pids == set()
    assert _FakeClient.instances[-1].shutdowns == 1


# ── concurrent mints for one slug ──


@pytest.mark.asyncio
async def test_row_identity_survives_a_coarse_clock(monkeypatch: pytest.MonkeyPatch):
    # Windows' time.monotonic() has ~15.6ms granularity, so two Connects inside one
    # tick read the same clock value. Identity must not be a clock reading, or
    # every token guard fails open and the late-failure clobber returns.
    monkeypatch.setattr(mint.time, "monotonic", lambda: 1234.5)
    gate = asyncio.Event()

    class _SlowFailure(_FakeClient):
        async def ensure_ready(self) -> None:
            await gate.wait()
            raise RuntimeError("late boom")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _SlowFailure)
    first = asyncio.create_task(mint.start_oauth_mint("notion", _URL))
    await asyncio.sleep(0)

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _FakeClient)
    await mint.start_oauth_mint("notion", _URL)
    live = mint._mints["notion"]

    gate.set()
    await asyncio.wait_for(first, timeout=5)

    assert mint._mints["notion"] is live
    assert live["state"] == "waiting"
    await mint._dispose_mint(live)


@pytest.mark.asyncio
async def test_a_late_failure_does_not_clobber_the_live_row(monkeypatch: pytest.MonkeyPatch):
    gate = asyncio.Event()

    class _SlowFailure(_FakeClient):
        async def ensure_ready(self) -> None:
            await gate.wait()
            raise RuntimeError("late boom")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _SlowFailure)
    first = asyncio.create_task(mint.start_oauth_mint("notion", _URL))
    await asyncio.sleep(0)

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _FakeClient)
    await mint.start_oauth_mint("notion", _URL)
    live = mint._mints["notion"]
    assert live["state"] == "waiting"

    gate.set()
    await asyncio.wait_for(first, timeout=5)

    # The superseded flow's failure must not replace the row that holds a live
    # client, watcher and spec -- that would strand all three.
    assert mint._mints["notion"] is live
    assert live["state"] == "waiting"
    assert live["oauth_url"] == _AUTHORIZE
    await mint._dispose_mint(live)


# ── the URL predicate ──


@pytest.mark.asyncio
async def test_a_credential_bearing_url_is_refused_and_never_recorded(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int], caplog
):
    tainted = "https://auth.example.com/authorize?client_id=abc&access_token=AKIAIOSFODNN7EXAMPLE"

    class _Tainted(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = [{"serverName": "notion", "oauthUrl": tainted}]

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _Tainted)
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )

    with caplog.at_level("WARNING"):
        await mint.start_oauth_mint("notion", _URL)

    # Same predicate the chat consent path applies. The card gets a coarse
    # failure, and the value appears in no log line and no audit event.
    view = mint.pending_mint_for("notion")
    assert _state_only(view) == {"state": "failed", "reason": "mint_url_rejected"}
    assert logged == ["error reason=mint_url_rejected"]
    assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(logged)
    assert protected_pids == set()
    assert _FakeClient.instances[-1].shutdowns == 1


@pytest.mark.asyncio
async def test_a_failure_records_an_audit_event_with_no_exception_text(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Boom(_FakeClient):
        async def ensure_ready(self) -> None:
            raise TimeoutError("provider unreachable at 10.0.0.5")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _Boom)
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )

    await mint.start_oauth_mint("notion", _URL)

    assert logged == ["error reason=mint_timeouterror"]
    assert "10.0.0.5" not in json.dumps(logged)


# ── ephemeral spec lifecycle ──
#
# The invariant under test: cleanup only ever deletes a path this gateway
# RECORDED CREATING. Name shape authorizes nothing.


def test_cleanup_never_touches_a_file_it_did_not_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    manifest = tmp_path / "mint-specs.json"
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: manifest)
    old = time.time() - mint._MINT_SPEC_ORPHAN_SECONDS - 60
    # Aged files that LOOK exactly like ours, including the pid-token shape a
    # previous round matched on. None of them is in the manifest.
    lookalikes = [
        tmp_path / f"kirocrew-mint-notion-{os.getpid()}-abcdef12.json",
        tmp_path / "kirocrew-mint-notion-1234-deadbeef.json",
        tmp_path / "kirocrew-mint-notion.json",
    ]
    for path in lookalikes:
        path.write_text("USER FILE", encoding="utf-8")
        os.utime(path, (old, old))

    mint._sweep_mint_specs()

    for path in lookalikes:
        assert path.read_text(encoding="utf-8") == "USER FILE", path.name


def test_the_self_heal_leaves_rows_still_inside_the_ttl_window(_isolated_mint: Path):
    live = _mint_shaped(_isolated_mint, alias="linear")
    live.write_text("{}", encoding="utf-8")
    mint._write_mint_manifest({str(live): time.time()})

    mint._sweep_mint_specs()

    # Age is the trigger; a row inside the window belongs to a mint that may still
    # be running, so it is left alone even though every conjunct holds.
    assert live.is_file()
    assert str(live) in mint._read_mint_manifest()


def test_releasing_a_spec_drops_its_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    manifest = tmp_path / "mint-specs.json"
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: manifest)
    mine = tmp_path / f"kirocrew-mint-notion-{os.getpid()}-abcdef12.json"
    mine.write_text("{}", encoding="utf-8")
    mint._write_mint_manifest({str(mine): time.time()})

    mint._remove_mint_agent_spec(str(mine))

    assert not mine.exists()
    assert mint._read_mint_manifest() == {}


def test_an_unreadable_manifest_reads_as_empty_and_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    manifest = tmp_path / "mint-specs.json"
    manifest.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: manifest)
    bystander = tmp_path / "bystander.json"
    bystander.write_text("{}", encoding="utf-8")

    assert mint._read_mint_manifest() == {}
    mint._sweep_mint_specs()

    assert bystander.is_file()


AGED = -(mint._MINT_SPEC_ORPHAN_SECONDS + 60)


def _plant_row(path: Path) -> None:
    """Record ``path`` as a manifest row old enough for the self-heal to consider."""
    mint._write_mint_manifest({str(path): time.time() + AGED})


def _mint_shaped(name_dir: Path, alias: str = "notion") -> Path:
    return name_dir / f"kirocrew-mint-{alias}-{os.getpid()}-abcdef12.json"


@pytest.mark.asyncio
async def test_the_mint_pid_is_protected_while_readiness_is_still_stalled(monkeypatch):
    # The child is spawned partway THROUGH ensure_ready. Nothing claims it as a
    # session, so until it is registered the orphan sweep can kill a mint that is
    # still initializing -- waiting for readiness leaves that whole window open.
    protected: list[int] = []
    monkeypatch.setattr(mint, "register_protected_pid", protected.append)
    monkeypatch.setattr(mint, "grant_present", lambda url: False)
    monkeypatch.setattr(mint, "_write_mint_agent_spec", lambda slug: ("agent", "/tmp/s.json"))

    ready = asyncio.Event()

    class _SlowReady:
        def __init__(self, **kwargs):
            self._pid = 0

        async def ensure_ready(self):
            # The spawn assigns the PID, then initialization drags on.
            self._pid = 4242
            await ready.wait()

        def pop_pending_oauth_requests(self):
            return []

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _SlowReady)

    async def _fake_dispose(holdings):
        return None

    monkeypatch.setattr(mint, "_dispose_mint", _fake_dispose)
    flow = asyncio.get_running_loop().create_task(mint.start_oauth_mint("notion", _URL))
    try:
        # Readiness has NOT completed, yet the PID is already shielded.
        await asyncio.wait_for(_until(lambda: protected == [4242]), timeout=5)
    finally:
        ready.set()
        await asyncio.wait_for(flow, timeout=5)
        mint._mints.pop("notion", None)


async def _until(predicate, interval: float = 0.01) -> None:
    """Poll until the predicate holds. Bounded by the caller's wait_for."""
    while not predicate():
        await asyncio.sleep(interval)


def test_a_failed_release_unlink_keeps_its_manifest_row(monkeypatch):
    # The row is the only thing authorizing a delete of this file, so dropping it
    # on a failed unlink strands a real spec that no later sweep can see.
    agents_dir = mint._agent.kiro_agents_dir_path()
    spec = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    spec.write_text("{}", encoding="utf-8")
    mint._write_mint_manifest({str(spec): time.time()})

    real_unlink = Path.unlink

    def _refuse(self, *a, **k):
        if self == spec:
            raise OSError("busy")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _refuse)
    mint._remove_mint_agent_spec(str(spec))

    # Row retained and the file still there: a retry is still possible.
    assert str(spec) in mint._read_mint_manifest()
    assert spec.exists()


def test_a_successful_release_drops_its_manifest_row():
    agents_dir = mint._agent.kiro_agents_dir_path()
    spec = agents_dir / "kirocrew-mint-notion-4243-abcdef02.json"
    spec.write_text("{}", encoding="utf-8")
    mint._write_mint_manifest({str(spec): time.time()})

    mint._remove_mint_agent_spec(str(spec))

    assert not spec.exists()
    assert str(spec) not in mint._read_mint_manifest()


def test_a_hard_stop_between_the_row_and_the_file_leaves_only_a_harmless_row(monkeypatch):
    # Row-before-file: if the process dies after the row lands and before the spec
    # is published, what survives is bookkeeping, not a selectable agent. The
    # opposite order leaves a file no sweep can ever see.
    agents_dir = mint._agent.kiro_agents_dir_path()
    (agents_dir / mint.AGENT_FILENAME).write_text(
        json.dumps({"mcpServers": {mint.mcp_server_alias("notion"): {"url": _URL}}}),
        encoding="utf-8",
    )

    class _HardStop(BaseException):
        pass

    real_write = mint._agent._atomic_json_write

    def _die_on_the_spec(path, data, *a, **k):
        # Only the spec publish dies; the manifest write must still land, which is
        # the whole point of doing it first.
        if Path(path).name.startswith("kirocrew-mint-"):
            raise _HardStop()
        return real_write(path, data, *a, **k)

    monkeypatch.setattr(mint._agent, "_atomic_json_write", _die_on_the_spec)
    with pytest.raises(_HardStop):
        mint._write_mint_agent_spec("notion")

    # The row exists; the file does not.
    rows = mint._read_mint_manifest()
    assert len(rows) == 1
    recorded = next(iter(rows))
    assert not Path(recorded).exists()

    # Age it past the orphan window and sweep: the row goes, nothing is stranded.
    mint._write_mint_manifest({recorded: time.time() - mint._MINT_SPEC_ORPHAN_SECONDS - 1})
    mint._sweep_mint_specs()
    assert recorded not in mint._read_mint_manifest()


def test_a_row_survives_a_failed_unlink_so_a_later_sweep_retries(monkeypatch):
    # Dropping the row on a failed unlink would abandon a real file that nothing
    # else is authorized to delete -- the row IS the authorization.
    agents_dir = mint._agent.kiro_agents_dir_path()
    orphan = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    orphan.write_text("{}", encoding="utf-8")
    aged = time.time() - mint._MINT_SPEC_ORPHAN_SECONDS - 1
    mint._write_mint_manifest({str(orphan): aged})

    real_unlink = Path.unlink
    refusing = [True]

    def _refuse(self, *a, **k):
        if refusing[0] and self == orphan:
            raise OSError("busy")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _refuse)
    mint._sweep_mint_specs()
    # Row retained, file still there: the sweep gave up on the file, not the row.
    assert str(orphan) in mint._read_mint_manifest()
    assert orphan.exists()

    # The next sweep retries, because the row was kept as its authorization.
    refusing[0] = False
    mint._sweep_mint_specs()
    assert not orphan.exists()
    assert str(orphan) not in mint._read_mint_manifest()


def test_a_spec_whose_row_cannot_be_recorded_is_not_left_behind(monkeypatch, tmp_path):
    # A swallowed manifest-write error would leave the file with no row: invisible
    # to the sweep forever, and still selectable, since agent discovery globs every
    # JSON in the agents dir. The file must not outlive its row.
    agents_dir = mint._agent.kiro_agents_dir_path()
    (agents_dir / mint.AGENT_FILENAME).write_text(
        json.dumps({"mcpServers": {mint.mcp_server_alias("notion"): {"url": _URL}}}),
        encoding="utf-8",
    )
    # A real write failure, not a stubbed return: the manifest's parent cannot be
    # created because a regular file occupies the path.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: blocker / "sub" / "m.json")

    with pytest.raises(OSError):
        mint._write_mint_agent_spec("notion")

    # Nothing of ours is left in the agents dir.
    assert not list(agents_dir.glob("kirocrew-mint-*.json"))


@pytest.mark.asyncio
async def test_a_reconnect_that_short_circuits_still_reaps_aged_orphans(monkeypatch):
    # An orphan too young at its last sweep stays behind. Once a grant exists every
    # future reconnect returns before any spec write, so unless the sweep runs
    # first the row is never looked at again.
    agents_dir = mint._agent.kiro_agents_dir_path()
    orphan = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    orphan.write_text("{}", encoding="utf-8")
    aged = time.time() - mint._MINT_SPEC_ORPHAN_SECONDS - 1
    mint._write_mint_manifest({str(orphan): aged})
    monkeypatch.setattr(mint, "grant_present", lambda url: True)

    await mint.start_oauth_mint("notion", _URL)

    # The short-circuit still ran the sweep, so the orphan and its row are gone.
    assert not orphan.exists()
    assert str(orphan) not in mint._read_mint_manifest()
    assert mint._mints["notion"]["state"] == "granted"
    mint._mints.pop("notion", None)


def test_a_planted_absolute_victim_path_is_never_unlinked(
    _isolated_mint: Path, tmp_path: Path
):
    victim = tmp_path / "precious.json"
    victim.write_text("VICTIM", encoding="utf-8")
    _plant_row(victim)

    mint._sweep_mint_specs()

    # Conjunct 2: the resolved path is not inside the agents dir.
    assert victim.read_text(encoding="utf-8") == "VICTIM"
    assert str(victim) not in mint._read_mint_manifest()


def test_a_traversal_row_is_never_unlinked(_isolated_mint: Path, tmp_path: Path):
    victim = _mint_shaped(tmp_path)
    victim.write_text("VICTIM", encoding="utf-8")
    _plant_row(_isolated_mint / ".." / victim.name)

    mint._sweep_mint_specs()

    # Conjunct 2: resolve() collapses the `..` before containment is checked, so a
    # mint-shaped name one level up is still outside.
    assert victim.read_text(encoding="utf-8") == "VICTIM"


def test_a_symlink_redirecting_outside_the_agents_dir_is_never_followed(
    _isolated_mint: Path, tmp_path: Path
):
    victim = tmp_path / "outside.json"
    victim.write_text("VICTIM", encoding="utf-8")
    link = _mint_shaped(_isolated_mint)
    link.symlink_to(victim)
    _plant_row(link)

    mint._sweep_mint_specs()

    # Conjunct 2: resolve() follows the link first, so the target's real location is
    # what containment sees -- neither the link nor its target is unlinked.
    assert victim.read_text(encoding="utf-8") == "VICTIM"
    assert link.is_symlink()


def test_a_planted_row_naming_a_real_agent_spec_is_never_unlinked(_isolated_mint: Path):
    handwritten = _isolated_mint / "my-own-agent.json"
    handwritten.write_text("VICTIM", encoding="utf-8")
    _plant_row(handwritten)

    mint._sweep_mint_specs()

    # Conjunct 3: inside the agents dir, but not this module's name shape.
    assert handwritten.read_text(encoding="utf-8") == "VICTIM"


def test_a_planted_row_naming_one_of_our_managed_specs_is_never_unlinked(
    monkeypatch: pytest.MonkeyPatch, _isolated_mint: Path
):
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

    owned = _isolated_mint / OWNED_KIRO_AGENT_FILES[0]
    owned.write_text("VICTIM", encoding="utf-8")
    # Force the name-shape conjunct to pass so conjunct 4 is what refuses.
    monkeypatch.setattr(mint, "_MINT_NAME_RE", re.compile(r".*"))
    _plant_row(owned)

    mint._sweep_mint_specs()

    # Conjunct 4: our own long-lived managed specs are never mint leftovers.
    assert owned.read_text(encoding="utf-8") == "VICTIM"


def test_a_genuine_stale_mint_spec_is_still_reaped(_isolated_mint: Path):
    stranded = _mint_shaped(_isolated_mint)
    stranded.write_text("{}", encoding="utf-8")
    _plant_row(stranded)

    mint._sweep_mint_specs()

    # All four conjuncts hold, so the orphan self-heal still works -- the point of
    # not adopting "drop rows, never unlink".
    assert not stranded.exists()
    assert str(stranded) not in mint._read_mint_manifest()


def test_an_unrecorded_mint_shaped_file_in_the_agents_dir_is_never_unlinked(
    _isolated_mint: Path,
):
    # Satisfies conjuncts 2, 3 and 4 -- inside the agents dir, our name shape, not
    # an owned spec -- and is still refused, because it is in no manifest of ours.
    # This is a SIBLING GATEWAY's live mint spec: its manifest is not ours to read,
    # so unlinking it would break a mint that is mid-activation elsewhere.
    sibling = _mint_shaped(_isolated_mint, alias="linear")
    sibling.write_text("SIBLING", encoding="utf-8")
    old = time.time() + AGED
    os.utime(sibling, (old, old))

    mint._sweep_mint_specs()

    assert sibling.read_text(encoding="utf-8") == "SIBLING"


def test_an_owned_name_symlinked_to_a_mint_shaped_file_is_never_unlinked(
    _isolated_mint: Path,
):
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

    # The check/act attack: every name-based conjunct would pass on the RESOLVED
    # target (in-dir, mint-shaped, not owned) while unlink acts on the owned
    # lexical path. Judging the lexical path -- and refusing symlinks outright --
    # is what keeps the owned spec.
    target = _mint_shaped(_isolated_mint)
    target.write_text("{}", encoding="utf-8")
    owned = _isolated_mint / OWNED_KIRO_AGENT_FILES[0]
    owned.unlink(missing_ok=True)  # the fixture wrote a real one; swap in the attack
    owned.symlink_to(target)
    _plant_row(owned)

    mint._sweep_mint_specs()

    assert owned.is_symlink()
    assert target.is_file()


def test_a_symlinked_mint_shaped_name_is_also_refused(_isolated_mint: Path):
    # Even when the link's OWN name is mint-shaped: a name and its target disagree
    # by construction, so there is no safe way to check one and unlink the other.
    target = _isolated_mint / "real-agent.json"
    target.write_text("VICTIM", encoding="utf-8")
    link = _mint_shaped(_isolated_mint, alias="vercel")
    link.symlink_to(target)
    _plant_row(link)

    mint._sweep_mint_specs()

    assert target.read_text(encoding="utf-8") == "VICTIM"
    assert link.is_symlink()


def test_a_spec_name_is_unique_per_flow(tmp_path: Path):
    first = mint._mint_spec_name("notion")
    second = mint._mint_spec_name("notion")

    assert first != second
    assert first.startswith(f"kirocrew-mint-notion-{os.getpid()}-")


def test_removal_deletes_only_the_exact_path_it_is_given(tmp_path: Path):
    mine = tmp_path / f"kirocrew-mint-notion-{os.getpid()}-abcdef12.json"
    sibling = tmp_path / f"kirocrew-mint-notion-{os.getpid() + 1}-beefcafe.json"
    for path in (mine, sibling):
        path.write_text("{}", encoding="utf-8")

    mint._remove_mint_agent_spec(str(mine))

    assert not mine.exists()
    assert sibling.is_file()


def test_removal_is_a_no_op_without_a_path(tmp_path: Path):
    handmade = tmp_path / "kirocrew-mint-notion.json"
    handmade.write_text("{}", encoding="utf-8")

    # The main-agent fallback records no path, so there is nothing to delete.
    mint._remove_mint_agent_spec("")

    assert handmade.is_file()


def test_a_provider_absent_from_the_main_spec_falls_back_to_the_managed_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / "kirocrew.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    assert mint._write_mint_agent_spec("notion") == ("kirocrew", "")


def test_writing_a_spec_lands_a_uniquely_named_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / "kirocrew.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": _URL}}}), encoding="utf-8"
    )

    name, path = mint._write_mint_agent_spec("notion")

    assert name.startswith(f"kirocrew-mint-notion-{os.getpid()}-")
    assert Path(path) == tmp_path / f"{name}.json"
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    assert list(body["mcpServers"]) == ["notion"]
    # Single-server spec: no rename can be needed, so the key is absent.
    assert "toolAliases" not in body


def test_writing_refuses_to_overwrite_an_existing_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / "kirocrew.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": _URL}}}), encoding="utf-8"
    )
    monkeypatch.setattr(mint, "_mint_spec_name", lambda alias: "kirocrew-mint-fixed-1-aaaaaaaa")
    (tmp_path / "kirocrew-mint-fixed-1-aaaaaaaa.json").write_text("PRECIOUS", encoding="utf-8")

    with pytest.raises(FileExistsError):
        mint._write_mint_agent_spec("notion")

    assert (tmp_path / "kirocrew-mint-fixed-1-aaaaaaaa.json").read_text(
        encoding="utf-8"
    ) == "PRECIOUS"


# ── grant detection ──


def test_a_grant_requires_both_paired_artifacts(tmp_path: Path):
    key = mint.grant_key(_URL)
    assert mint.grant_present(_URL, cache_dir=tmp_path) is False

    (tmp_path / f"{key}.token.json").write_text("{}", encoding="utf-8")
    # A lone token file also matches the single-file SSO naming in this dir.
    assert mint.grant_present(_URL, cache_dir=tmp_path) is False

    (tmp_path / f"{key}.registration.json").write_text("{}", encoding="utf-8")
    assert mint.grant_present(_URL, cache_dir=tmp_path) is True


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://mcp.example.com/mcp", "https://MCP.Example.com/mcp"),
        ("https://mcp.example.com/mcp", "https://mcp.example.com:443/mcp"),
        ("https://mcp.example.com", "https://mcp.example.com/"),
    ],
)
def test_the_grant_key_normalizes_the_way_the_runtime_does(left: str, right: str):
    assert mint.grant_key(left) == mint.grant_key(right)


def test_the_grant_key_separates_distinct_endpoints():
    assert mint.grant_key(_URL) != mint.grant_key("https://mcp.example.com/other")


# ── the grant read stays off the event loop ──
#
# The predicate stats the user's home. That is sub-millisecond locally and
# unbounded on a network-mounted home, and on the loop an unbounded stat takes the
# gateway's heartbeat with it. Both call sites therefore run it in a worker thread.
# ``grant_present`` is left UNPATCHED in these two -- only wrapped to record the
# thread -- so each asserts the verdict still resolves through the wrapped path
# rather than only that a thread was used.


@pytest.mark.asyncio
async def test_the_reconnect_short_circuit_reads_a_real_grant_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    _write_paired_grant_artifacts(_URL)
    seen: list[int] = []
    monkeypatch.setattr(mint, "grant_present", _grant_reads_recorded(seen))

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    assert _FakeClient.instances == []
    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_the_watcher_reads_a_real_grant_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"

    # After the spawn, so the short-circuit above does not consume the grant and
    # the recorder only sees the watcher's own reads.
    _write_paired_grant_artifacts(_URL)
    seen: list[int] = []
    monkeypatch.setattr(mint, "grant_present", _grant_reads_recorded(seen))

    await asyncio.wait_for(entry["watcher"], timeout=5)

    assert mint._mints["notion"]["state"] == "granted"
    assert seen and threading.get_ident() not in seen


# ── the grant-presence stat is SEL-audited ──
#
# The paired artifacts live under ``~/.aws/sso/cache``, a directory
# ``security._SENSITIVE_HOME_DIRS`` classifies. They are stat-ed and never opened,
# so no token material enters the process and there is no content for
# ``hooks.safe_read_file_internal`` to gate -- and the per-provider name is a
# digest, so there is no fixed path to register with it either. What the access
# still owes is a trail, which is what these pin.


def _audit_calls_recorded(monkeypatch: pytest.MonkeyPatch, recorded: bool = True) -> list[Any]:
    """Capture ``(read_id, outcome, thread)`` per audit emission, suppressing SEL."""
    seen: list[Any] = []

    def fake(read_id: str, outcome: str) -> bool:
        seen.append((read_id, outcome, threading.get_ident()))
        return recorded

    monkeypatch.setattr(mint._hooks, "emit_internal_read_audit", fake)
    return seen


def test_the_grant_presence_read_id_is_registered_for_audit():
    """Registration is what makes the audit real, not the call site.

    ``emit_internal_read_audit`` rejects an unregistered ``read_id`` and returns
    False WITHOUT emitting, so a call site whose id is missing from the registry
    records nothing while looking audited at the point of use.
    """
    assert mint._GRANT_PRESENCE_READ_ID in hooks._AUDIT_ONLY_READ_IDS


@pytest.mark.asyncio
async def test_observing_a_grant_audits_once_off_the_loop(monkeypatch: pytest.MonkeyPatch):
    seen = _audit_calls_recorded(monkeypatch)
    _write_paired_grant_artifacts(_URL)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    assert [(rid, outcome) for rid, outcome, _ in seen] == [
        (mint._GRANT_PRESENCE_READ_ID, "success")
    ]
    # Critical SEL events drain the queue on the calling thread.
    assert threading.get_ident() not in [thread for _, _, thread in seen]


@pytest.mark.asyncio
async def test_polling_for_an_absent_grant_never_audits(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    """Otherwise one flow writes a synchronous event every poll for the whole TTL."""
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    seen = _audit_calls_recorded(monkeypatch)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"
    # Several polls, every one of them negative.
    await asyncio.sleep(0.05)

    assert seen == []
    # Releases the still-waiting watcher, so the poll loop does not outlive the test.
    await mint._dispose_mint(entry)


@pytest.mark.asyncio
async def test_an_unrecordable_audit_does_not_withhold_the_grant(
    monkeypatch: pytest.MonkeyPatch,
):
    """Deliberately fail-OPEN, unlike ``safe_read_file_internal``.

    Nothing sensitive crosses this boundary, so denying on an unrecordable audit
    would only strand a Connect the user actually completed.
    """
    seen = _audit_calls_recorded(monkeypatch, recorded=False)
    _write_paired_grant_artifacts(_URL)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    assert len(seen) == 1


# ── every OTHER filesystem touch stays off the event loop too ──
#
# Same reasoning as the grant read above, applied to the rest of the module: the
# agents directory and our own state dir can sit on the same network mount as the
# home, and an unbounded operation on the loop stalls every request. Each test
# WRAPS the real function to record the thread rather than patching it away, so it
# asserts the outcome still resolves through the wrapped path -- a to_thread hop
# that lost its result would pass a thread-only assertion.


def _calls_recorded(fn: Any, seen: list[int]) -> Any:
    """``fn`` itself, plus the id of the thread each call ran on."""

    def recorded(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return fn(*args, **kwargs)

    return recorded


@pytest.mark.asyncio
async def test_the_spec_removal_runs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    spec_path = Path(entry["spec_path"])
    assert spec_path.exists()
    seen: list[int] = []
    monkeypatch.setattr(
        mint, "_remove_mint_agent_spec", _calls_recorded(mint._remove_mint_agent_spec, seen)
    )

    await mint._dispose_mint(entry)

    # Off the loop, and the file it was meant to release is really gone.
    assert seen and threading.get_ident() not in seen
    assert not spec_path.exists()


@pytest.mark.asyncio
async def test_a_teardown_cancelled_from_outside_still_releases_the_spec_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # The removal is the last act of a teardown that can be cancelled mid-flight,
    # so it is shielded: moving it to a worker thread must not make a cancellation
    # able to drop it, or a released spec stays on disk as a selectable agent.
    class _HangingShutdown(_FakeClient):
        async def shutdown(self) -> None:
            self.shutdowns += 1
            await asyncio.Event().wait()

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _HangingShutdown)
    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    spec_path = Path(entry["spec_path"])
    seen: list[int] = []
    monkeypatch.setattr(
        mint, "_remove_mint_agent_spec", _calls_recorded(mint._remove_mint_agent_spec, seen)
    )

    task = asyncio.create_task(mint._dispose_mint(entry))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert not spec_path.exists()
    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_the_agents_dir_read_runs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, _isolated_mint: Path
):
    class _NoChallenge(_FakeClient):
        def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
            return []

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _NoChallenge)
    # A concurrent uninstall: the real predicate has to actually find the entry
    # gone, so the recorded verdict is the one the flow acts on.
    (_isolated_mint / "kirocrew.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    seen: list[int] = []
    monkeypatch.setattr(
        mint, "_agent_spec_entry_missing", _calls_recorded(mint._agent_spec_entry_missing, seen)
    )

    await mint.start_oauth_mint("notion", _URL)

    assert seen and threading.get_ident() not in seen
    assert mint._mints["notion"]["reason"] == "mint_server_absent"


@pytest.mark.asyncio
async def test_the_data_home_resolution_runs_off_the_loop(monkeypatch: pytest.MonkeyPatch):
    # Resolving the data home CREATES it under a KIROCREW_HOME override, so it is a
    # write and not a path join.
    seen: list[int] = []
    monkeypatch.setattr(mint, "data_home", _calls_recorded(mint.data_home, seen))

    await mint.start_oauth_mint("notion", _URL)

    assert seen and threading.get_ident() not in seen
    # The resolved value still reaches the client it was resolved for.
    assert _FakeClient.instances[-1].kwargs["work_dir"] == mint.data_home() / "connections" / "mint"
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_the_outcome_audit_runs_off_the_loop(monkeypatch: pytest.MonkeyPatch):
    # Only the APPEND is queued to SEL's writer thread. The first sel() of a
    # process constructs the log first -- trust dir, key validation, a backward
    # scan of the existing log -- on whichever thread calls it, so the audit has
    # to be reached off the loop even though later calls are cheap.
    seen: list[int] = []
    monkeypatch.setattr(mint, "_log_mint_outcome", _calls_recorded(mint._log_mint_outcome, seen))

    await mint.start_oauth_mint("notion", _URL)

    assert seen and threading.get_ident() not in seen
    await mint._dispose_mint(mint._mints["notion"])


# ── the invariant itself: no coroutine here may touch the filesystem ──
#
# Derived from the module's own AST rather than a maintained list, so a NEW helper
# that reaches the filesystem is covered the moment it is called from a coroutine.
# This is what makes the rule a property of the module instead of a fact about the
# call sites that happen to exist today.

# Filesystem primitives, as they appear in a call. Ambiguous string methods
# (``replace``, ``rename``) are deliberately absent: any helper that would use
# them here also stats or writes, so it is already reached through those.
_FS_ATTRS = frozenset(
    {
        "unlink",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "is_file",
        "is_dir",
        "is_symlink",
        "exists",
        "stat",
        "lstat",
        "mkdir",
        "makedirs",
        "rmdir",
        "iterdir",
        "glob",
        "rglob",
        "listdir",
        "scandir",
        "home",
        "_atomic_json_write",
        "_load_json",
        "kiro_agents_dir_path",
        # Reaches the filesystem through the SEL singleton (see ``sel`` below):
        # the event is marked critical, so it drains the queue on the calling
        # thread rather than merely enqueueing. Listed so a future DIRECT call
        # from a coroutine fails here instead of silently blocking the loop --
        # ``_grant_observed`` passes it to ``asyncio.to_thread`` today.
        "emit_internal_read_audit",
    }
)
# Bare-name calls that reach the filesystem: the builtin, the imported resolvers
# that create the directory they answer for, and the audit singleton -- whose FIRST
# call in a process constructs the log (trust dir, key, backward scan) on the
# caller's thread, even though every call after that only enqueues.
_FS_NAMES = frozenset({"open", "data_home", "sel"})


def _called_names(node: Any) -> set[str]:
    """Every function NAME this node calls directly.

    A reference that is merely PASSED -- ``asyncio.to_thread(f, x)`` -- is a name
    load and not a call, so it is absent here by construction. That is precisely
    the distinction the invariant rests on.
    """
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                out.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                out.add(sub.func.attr)
    return out


def test_no_coroutine_in_the_mint_module_touches_the_filesystem_directly():
    tree = ast.parse(inspect.getsource(mint))
    sync = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    coros = {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    assert sync and coros, "module shape changed; this guard is reading the wrong tree"

    # Which synchronous helpers reach the filesystem, transitively within the
    # module. Fixed point, so a wrapper around a wrapper is still caught.
    touches = {
        name: bool(_called_names(node) & (_FS_ATTRS | _FS_NAMES)) for name, node in sync.items()
    }
    changed = True
    while changed:
        changed = False
        for name, node in sync.items():
            if touches[name]:
                continue
            if any(touches.get(callee) for callee in _called_names(node)):
                touches[name] = changed = True
    fs_helpers = {name for name, hit in touches.items() if hit}
    # The known set, so a helper silently losing its filesystem work (and with it
    # this guard's coverage) is visible rather than a quietly weaker test.
    assert fs_helpers == {
        "kiro_oauth_cache_dir",
        "grant_present",
        "_is_reapable_spec",
        "_mint_manifest_path",
        "_read_mint_manifest",
        "_write_mint_manifest",
        "_sweep_mint_specs",
        "_record_mint_spec",
        "_forget_mint_spec",
        "_write_mint_agent_spec",
        "_remove_mint_agent_spec",
        "_agent_spec_entry_missing",
        "_log_mint_outcome",
    }

    offenders = {
        f"{coro} -> {callee}"
        for coro, node in coros.items()
        for callee in _called_names(node) & (fs_helpers | _FS_ATTRS | _FS_NAMES)
    }
    assert not offenders, (
        "filesystem work on the event loop: "
        + ", ".join(sorted(offenders))
        + " -- route it through asyncio.to_thread"
    )


# ── boot cost ──


def test_the_handlers_package_does_not_import_the_mint_engine():
    """The gateway imports the handlers package at boot; the mint must not ride along.

    Run in a subprocess because this test module imports the mint directly, so an
    in-process ``sys.modules`` check would always find it.
    """
    probe = (
        "import sys; import kiro_crew.dashboard.handlers;"
        " print('MINT' if 'kiro_crew.connections.mint' in sys.modules else 'CLEAN')"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=180
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("CLEAN"), out.stdout


# ── grant-detection drift guard ──
#
# grant_key mirrors an UNDOCUMENTED internal of an external binary (kiro-cli's
# ``compute_key``) and the artifact layout it writes, and that pairing is the
# mint's only consent-completion signal. If a kiro-cli release changes either, the
# watcher never observes the grant, holds the process to the full TTL and reports
# ``expired`` after a consent that actually succeeded -- a silent degradation.
# These literals are recorded, not recomputed from the implementation, so drift
# fails loudly here instead of in the field.

_RECORDED_GRANT_KEYS = {
    "https://mcp.notion.com/mcp": (
        "1cbd18bf1818c780c9568384b0324fff99a5335bd4a17e102be74712b007e62c"
    ),
    "https://mcp.linear.app/mcp": (
        "fb39103c7d2edac291c92d23247e0a7d90470b1b349c07b146aba4ee2c81591f"
    ),
    "https://mcp.atlassian.com/v1/sse": (
        "834761c496c5a564116b2f1c55805d4425c32caee9e86596590d3d6e332a3240"
    ),
    "https://mcp.vercel.com": (
        "27af4e7d14d9aa7579dff853f8b7033ffbaaf6fb734bf15aec53f68776bb4111"
    ),
    "https://mcp.sentry.dev/mcp": (
        "956f74053c03bea04f650e2341a266b5e3162116bc5c7f74b1f4d5afb4654b72"
    ),
    "https://api.githubcopilot.com/mcp/": (
        "ca602dd499edea084d35bd6584aea648dec2778e67b48e10ab008ae0c1d06284"
    ),
    # Mixed case and an explicit default port, recorded against the SAME key as
    # their canonical spelling: both halves of the normalization are pinned, not
    # just the hash of an already-canonical string.
    "https://MCP.Notion.COM/mcp": (
        "1cbd18bf1818c780c9568384b0324fff99a5335bd4a17e102be74712b007e62c"
    ),
    "https://mcp.notion.com:443/mcp": (
        "1cbd18bf1818c780c9568384b0324fff99a5335bd4a17e102be74712b007e62c"
    ),
}


@pytest.mark.parametrize(("mcp_url", "recorded"), sorted(_RECORDED_GRANT_KEYS.items()))
def test_the_grant_key_matches_its_recorded_value(mcp_url: str, recorded: str):
    assert mint.grant_key(mcp_url) == recorded


def test_the_grant_key_formula_is_sha256_of_origin_and_path():
    """Independent restatement of the rule, so a rewrite cannot silently redefine it."""
    expected = hashlib.sha256(b"https://mcp.notion.com/mcp").hexdigest()

    assert mint.grant_key("https://mcp.notion.com/mcp") == expected


def test_the_artifact_layout_assumptions_are_pinned():
    # The directory and the suffix PAIR are as much of the contract as the hash:
    # a layout move breaks detection exactly as silently as a key change.
    assert mint._KIRO_OAUTH_CACHE_RELATIVE == (".aws", "sso", "cache")
    assert mint._TOKEN_SUFFIX == ".token.json"
    assert mint._REGISTRATION_SUFFIX == ".registration.json"
    assert Path("/home/u").joinpath(*mint._KIRO_OAUTH_CACHE_RELATIVE) == Path(
        "/home/u/.aws/sso/cache"
    )


# ── HTTP surface ──


async def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    app.router.add_get("/api/connections/mint", connections.api_connections_mint_state)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_a_row_whose_parent_is_a_symlink_is_never_unlinked(tmp_path, monkeypatch):
    # The leaf is a real file, so a leaf-only symlink check passes it, and the
    # resolved parent equals the agents dir -- yet unlink() re-resolves the link at
    # act time, so the check never governed the path being deleted.
    agents_dir = mint._agent.kiro_agents_dir_path()
    link_dir = tmp_path / "agents-link"
    link_dir.symlink_to(agents_dir, target_is_directory=True)
    victim = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    victim.write_text("{}", encoding="utf-8")

    assert mint._is_reapable_spec(str(link_dir / victim.name)) is False
    # The direct, lexical spelling of the same file stays reapable.
    assert mint._is_reapable_spec(str(victim)) is True


def test_a_relative_row_is_never_unlinked():
    # Relative paths resolve against the process cwd, which is not a property the
    # gateway controls; only the absolute form the writer recorded is reapable.
    assert mint._is_reapable_spec("kirocrew-mint-notion-4242-abcdef01.json") is False


@pytest.mark.asyncio
async def test_cancelling_a_mint_still_releases_what_it_holds(monkeypatch):
    # CancelledError is a BaseException, so it bypasses the failure handler while
    # the child process, its listener, the protected PID and the spec are held.
    released: list[dict] = []

    async def _fake_dispose(holdings):
        released.append(dict(holdings))

    class _CancelDuringReady:
        def __init__(self, **kwargs):
            self._pid = 0

        async def ensure_ready(self):
            raise asyncio.CancelledError()

    monkeypatch.setattr(mint, "_dispose_mint", _fake_dispose)
    monkeypatch.setattr(mint, "grant_present", lambda url: False)
    # Cancellation lands in ensure_ready, with the spec written and the client
    # already spawned -- the exact window the failure handler cannot see.
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _CancelDuringReady)

    with pytest.raises(asyncio.CancelledError):
        await mint.start_oauth_mint("notion", "https://example.test/mcp")
    # The finally ran and released what the flow had already taken ownership of.
    assert released and released[-1].get("spec_path")
    mint._mints.pop("notion", None)


@pytest.mark.asyncio
async def test_a_mint_with_no_entry_left_fails_instead_of_reporting_granted(monkeypatch):
    # A concurrent uninstall removed the entry between the Connect writing it and
    # this flow initializing. No entry means no challenge -- which must not read as
    # a granted connection, or the card shows connected with no server behind it
    # and no way back.
    class _NoChallenge:
        def __init__(self, **kwargs):
            self._pid = 0

        async def ensure_ready(self):
            return None

        def pop_pending_oauth_requests(self):
            return []

    async def _fake_dispose(holdings):
        return None

    monkeypatch.setattr(mint, "_dispose_mint", _fake_dispose)
    monkeypatch.setattr(mint, "grant_present", lambda url: False)
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _NoChallenge)
    monkeypatch.setattr(mint, "_write_mint_agent_spec", lambda slug: ("agent", "/tmp/spec.json"))

    monkeypatch.setattr(mint, "_agent_spec_entry_missing", lambda slug: True)
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )
    await mint.start_oauth_mint("notion", "https://example.test/mcp")
    assert mint._mints["notion"]["state"] == "failed"
    assert mint._mints["notion"]["reason"] == "mint_server_absent"
    # A failed outcome audits as an error, like every other failure path.
    assert logged == ["error reason=mint_server_absent"]
    mint._mints.pop("notion", None)

    # With the entry present, the same no-challenge result IS a real grant.
    monkeypatch.setattr(mint, "_agent_spec_entry_missing", lambda slug: False)
    await mint.start_oauth_mint("notion", "https://example.test/mcp")
    assert mint._mints["notion"]["state"] == "granted"
    mint._mints.pop("notion", None)


@pytest.mark.asyncio
async def test_a_dead_holder_becomes_really_expired_not_just_reported(monkeypatch):
    # The view already refuses to show a dead holder's URL, but the abandon fence
    # only acts on 'expired'. A view-only verdict left the stored row 'waiting', so
    # the entry could never be claimed and the card stuck on needs-attention.
    monkeypatch.setattr(mint, "_mint_holder_alive", lambda entry: False)

    async def _fake_dispose(holdings):
        return None

    monkeypatch.setattr(mint, "_dispose_mint", _fake_dispose)
    mint._mints["notion"] = {
        "state": "waiting",
        "started": 1.0,
        "token": "tok5",
        "oauth_url": "https://example.test/authorize",
    }
    try:
        await mint.expire_dead_holder("notion")
        assert mint._mints["notion"]["state"] == "expired"
        # The verdict is committed to the row, not merely reported by the view --
        # the card reads a state the stored row actually has.
        assert mint.pending_mint_for("notion")["state"] == "expired"
    finally:
        mint._mints.pop("notion", None)


@pytest.mark.asyncio
async def test_the_row_is_visible_before_the_post_answers(monkeypatch: pytest.MonkeyPatch):
    # Repro of the visibility window: a terminal row from a PREVIOUS attempt is
    # still in the table when the tab polls. If the POST answers before installing
    # this attempt's row, that poll returns the old terminal row and the card reads
    # it as the verdict on the new attempt, clearing the wait for good.
    displaced_token = "a" * 32
    mint._mints["notion"] = {"state": "expired", "started": 1.0, "token": displaced_token}
    seen: list[dict] = []

    async def _fake_start(slug, url, token=None, prior=None):
        seen.append({"token": token, "prior_state": (prior or {}).get("state")})

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    client = await _client(monkeypatch)
    try:
        body = await (await client.post("/api/connections/mint", json={"slug": "notion"})).json()
        # The row a GET would answer with, at the instant the POST returned.
        row = mint.pending_mint_for("notion")
        assert row is not None
        # The POST names the row it just reserved -- NOT the displaced one. Both
        # halves are load-bearing: comparing against an int would pass for free
        # now that tokens are strings.
        assert row["token"] == body["token"]
        assert body["token"] != displaced_token
        assert row["state"] == "minting"
        await asyncio.sleep(0)
        # The displaced terminal row is handed to the flow, not left in the table.
        assert seen and seen[0]["prior_state"] == "expired"
    finally:
        await client.close()
        mint._mints.pop("notion", None)


def test_row_tokens_do_not_repeat_across_a_gateway_restart():
    # A counter restarts at the same value with the process, so a tab holding a
    # pre-restart token would match a post-restart row and act on a flow it never
    # started. Each gateway is a fresh interpreter, so that is what this compares --
    # importlib.reload re-executes into the same namespace and would keep any
    # module state alive, hiding exactly the bug under test.
    probe = (
        # Import in the gateway's own order: the handlers package first, then the
        # mint engine it defers. A cold `import kiro_crew.connections.mint` trips a
        # pre-existing cycle in kiro_crew.session_pid that production never hits.
        "import kiro_crew.dashboard.handlers;"
        "from kiro_crew.connections.mint import _new_mint_token as t; print(t())"
    )
    first = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    second = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert first and second and first != second
    # Still unique within one process, which is what the row guards rely on.
    assert len({mint._new_mint_token() for _ in range(64)}) == 64


@pytest.mark.asyncio
async def test_a_stale_token_from_before_a_restart_never_matches_a_live_row():
    # The pre-restart tab's token is a different uuid, so every token-guarded write
    # declines rather than acting on the successor row.
    token, _prior = await mint.reserve_mint_row("notion")
    try:
        stale = "0" * 32
        assert stale != token
        assert mint._mints["notion"]["token"] == token
        # A guard written the way every call site writes it refuses the stale id.
        assert mint._mints["notion"].get("token") != stale
    finally:
        mint._mints.pop("notion", None)


@pytest.mark.asyncio
async def test_post_returns_the_row_token_it_started(monkeypatch: pytest.MonkeyPatch):
    started: list[tuple[str, str, str]] = []

    async def _fake_start(slug: str, url: str, token: str | None = None, prior=None) -> None:
        started.append((slug, url, str(token or "")))

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    client = await _client(monkeypatch)
    try:
        body = await (await client.post("/api/connections/mint", json={"slug": "notion"})).json()
        await asyncio.sleep(0)
        # The response names the row this caller started, so one tab can tell its
        # own terminal state from a sibling tab's.
        assert isinstance(body["token"], str) and len(body["token"]) == 32
        assert started and started[0][2] == body["token"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_carries_the_row_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        mint,
        "pending_mint_for",
        lambda slug: {"state": "expired", "reason": "mint_process_gone", "token": "tok77"},
    )
    client = await _client(monkeypatch)
    try:
        body = await (await client.get("/api/connections/mint?slug=notion")).json()
        # Present on the terminal view too -- that is the case a sibling tab would
        # otherwise act on.
        assert body["token"] == "tok77"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_schedules_a_mint_for_a_registry_provider(monkeypatch: pytest.MonkeyPatch):
    started: list[tuple[str, str]] = []

    async def _fake_start(slug: str, url: str, token: str | None = None, prior=None) -> None:
        started.append((slug, url))

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    client = await _client(monkeypatch)
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "notion"})
        assert resp.status == 200
        assert (await resp.json())["state"] == "minting"
        await asyncio.sleep(0)
        assert started and started[0][0] == "notion"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "slug",
    ["", "../../etc/passwd", "Notion; rm -rf /", "no-such-provider", "x" * 65],
)
@pytest.mark.asyncio
async def test_post_refuses_anything_that_is_not_a_shipped_provider(
    monkeypatch: pytest.MonkeyPatch, slug: str
):
    spawned: list[str] = []

    async def _fake_start(slug_: str, url: str, token: str | None = None, prior=None) -> None:
        spawned.append(slug_)

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    client = await _client(monkeypatch)
    try:
        resp = await client.post("/api/connections/mint", json={"slug": slug})
        # Registry membership bounds what a caller can make the gateway spawn.
        assert resp.status == 400
        assert (await resp.json())["code"] == "unknown_provider"
        await asyncio.sleep(0)
        assert spawned == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_a_non_object_body(monkeypatch: pytest.MonkeyPatch):
    client = await _client(monkeypatch)
    try:
        resp = await client.post("/api/connections/mint", data=b"not json")
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_body"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_serves_the_url_once_the_mint_is_waiting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        mint,
        "pending_mint_for",
        lambda slug: {"state": "waiting", "oauth_url": _AUTHORIZE},
    )
    client = await _client(monkeypatch)
    try:
        resp = await client.get("/api/connections/mint?slug=notion")
        assert await resp.json() == {
            "slug": "notion",
            "state": "waiting",
            "oauth_url": _AUTHORIZE,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_distinguishes_no_mint_from_a_mint_that_produced_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    client = await _client(monkeypatch)
    monkeypatch.setattr(mint, "pending_mint_for", lambda slug: None)
    try:
        resp = await client.get("/api/connections/mint?slug=notion")
        assert await resp.json() == {"slug": "notion", "state": "idle"}

        monkeypatch.setattr(
            mint,
            "pending_mint_for",
            lambda slug: {"state": "failed", "reason": "mint_timeouterror"},
        )
        resp = await client.get("/api/connections/mint?slug=notion")
        assert await resp.json() == {
            "slug": "notion",
            "state": "failed",
            "reason": "mint_timeouterror",
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_never_serves_a_url_outside_the_waiting_state(monkeypatch: pytest.MonkeyPatch):
    await mint.start_oauth_mint("notion", _URL)
    mint._mints["notion"]["state"] = "granted"
    client = await _client(monkeypatch)
    try:
        resp = await client.get("/api/connections/mint?slug=notion")
        assert "oauth_url" not in await resp.json()
    finally:
        await client.close()
        await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_get_refuses_an_unknown_provider(monkeypatch: pytest.MonkeyPatch):
    client = await _client(monkeypatch)
    try:
        resp = await client.get("/api/connections/mint?slug=no-such-provider")
        assert resp.status == 400
    finally:
        await client.close()
