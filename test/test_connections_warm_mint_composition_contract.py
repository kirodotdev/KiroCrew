"""The warm/mint composition contract: characterization of the rules the split must keep.

Two halves. The first characterizes behaviour the existing warm and mint suites leave
implicit -- process-identity verdicts, marker integrity, ownership refusals, single-flight
recovery, token-fenced watchers and claim rollback -- so an owner moving between modules
cannot change it unnoticed. The second pins the composition itself: which names the facades
keep, that each re-export is the implementation object, that a facade patch seam still
intercepts the owner that moved behind it, and that the boot path and the ACP boundary stay
where the gates expect them.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import test_connections_warm as warm_suite
from test_connections_warm import (
    _claim,
    _mimic_spec,
    _ours_shaped_spec_text,
    _parked_session,
    _provider,
    _QueuedOauthHandle,
    _rearm_registry,
    _result,
    _returns,
    _Runtime,
)

from kiro_crew import hooks
from kiro_crew.connections import mint, warm
from kiro_crew.connections.mint import _mints

_PC = warm.platform_compat

# The warm suite's fixtures, registered here by name: the two autouse resets of the module
# singletons, the isolated agents dir and data home, and the neutralized activation.
_clean_mint_table = warm_suite._clean_mint_table
_clean_warm_runtime = warm_suite._clean_warm_runtime
_agents_dir = warm_suite._agents_dir
_private_warm_home = warm_suite._private_warm_home
_stub_activation = warm_suite._stub_activation


def _pin_start_ids(monkeypatch: pytest.MonkeyPatch, current: str | None) -> None:
    """Pin both start-id reads, so no verdict depends on ``IS_WINDOWS`` -- which stays real:
    the marker and spec writes read it too, in ``replace_with_retry``'s Windows retry."""
    monkeypatch.setattr(_PC, "get_process_start_id", lambda pid: current)
    monkeypatch.setattr(_PC, "process_start_time", lambda pid: current)


def _marker(work_dir: Path, **fields: Any) -> None:
    body = {
        "sentinel": warm._WARM_GENERATION_SENTINEL,
        "version": warm._WARM_GENERATION_MARKER_VERSION,
        "gateway_pid": 0,
        "gateway_started": "",
        "runtime_pid": 0,
        "runtime_started": "",
    }
    body.update(fields)
    warm._warm_generation_marker_path(work_dir).write_text(json.dumps(body), encoding="utf-8")


# ── process identity: False is proof of death, None is "keep the tree" ──


@pytest.mark.parametrize(
    ("liveness", "recorded", "current", "expected"),
    [
        (_PC.PID_DEAD, "123", "123", False),
        (_PC.PID_ALIVE, "123", "123", True),
        (_PC.PID_UNSIGNALABLE, "123", "123", True),
        # A live PID whose comparable start id differs names ANOTHER process: reuse.
        (_PC.PID_ALIVE, "123", "456", False),
        # The retired ps-rendered spelling is unknown, never different.
        (_PC.PID_ALIVE, "Wed Sep  3 10:00:00 2026", "456", None),
        (_PC.PID_ALIVE, "123", None, None),
        ("unknown", "123", "123", None),
    ],
)
def test_process_identity_is_tri_state(
    monkeypatch: pytest.MonkeyPatch,
    liveness: str,
    recorded: str,
    current: str | None,
    expected: bool | None,
):
    monkeypatch.setattr(_PC, "pid_liveness", lambda pid: liveness)
    _pin_start_ids(monkeypatch, current)
    assert warm._process_identity_live(4242, recorded) is expected


@pytest.mark.parametrize(("pid", "started"), [(0, "123"), (-1, "123"), (4242, "")])
def test_an_identity_with_no_pid_or_no_recorded_start_is_unprovable(
    monkeypatch: pytest.MonkeyPatch, pid: int, started: str
):
    monkeypatch.setattr(_PC, "pid_liveness", lambda pid: _PC.PID_DEAD)
    assert warm._process_identity_live(pid, started) is None


@pytest.mark.parametrize(
    ("is_windows", "filetime", "expected"),
    [(False, "133700000000000000", None), (True, "133700000000000000", "133700000000000000")],
)
def test_the_filetime_leg_answers_only_on_windows(
    monkeypatch: pytest.MonkeyPatch, is_windows: bool, filetime: str, expected: str | None
):
    """The remaining POSIX leg is the locale-rendered ``ps`` output, which a destructive
    reader must never compare, so off Windows an unanswered start id stays unknown."""
    asked: list[int] = []

    def _process_start_time(pid: int) -> str:
        asked.append(pid)
        return filetime

    monkeypatch.setattr(_PC, "IS_WINDOWS", is_windows)
    monkeypatch.setattr(_PC, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(_PC, "process_start_time", _process_start_time)
    assert warm._marker_process_start_id(77) == expected
    assert asked == ([77] if is_windows else [])


def test_a_raising_filetime_read_is_unknown_rather_than_fatal(monkeypatch: pytest.MonkeyPatch):
    def _boom(pid: int) -> str:
        raise OSError("access denied")

    monkeypatch.setattr(_PC, "IS_WINDOWS", True)
    monkeypatch.setattr(_PC, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(_PC, "process_start_time", _boom)
    assert warm._marker_process_start_id(77) is None


@pytest.mark.parametrize("is_windows", [False, True])
def test_pinning_the_start_ids_leaves_the_platform_flag_alone(
    monkeypatch: pytest.MonkeyPatch, is_windows: bool
):
    """A pinned ``IS_WINDOWS`` would switch off the sharing-violation retry on the generation
    writes this helper's callers make, a Windows-only flake. Both values, so any host sees it."""
    monkeypatch.setattr(_PC, "IS_WINDOWS", is_windows)
    _pin_start_ids(monkeypatch, "1")
    assert _PC.IS_WINDOWS is is_windows


# ── generation ownership: markers, links, and the release/scavenge verdicts ──


def test_release_keeps_a_tree_whose_marker_carries_a_legacy_identity(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The release path must share the scavenger's migration rule: unknown keeps the tree."""
    monkeypatch.setattr(_PC, "pid_liveness", lambda pid: _PC.PID_ALIVE)
    _pin_start_ids(monkeypatch, "99999")
    work_dir = warm._create_warm_generation_dir()
    _marker(work_dir, runtime_pid=747474, runtime_started="Wed Sep  3 10:00:00 2026")

    class _Process:
        pid = 747474

    runtime = _Process()
    setattr(runtime, warm._WARM_RUNTIME_DIR_ATTR, str(work_dir))

    assert warm._release_runtime_generation(runtime) is False
    assert work_dir.is_dir()
    assert getattr(runtime, warm._WARM_RUNTIME_DIR_ATTR) == str(work_dir)


def test_the_scavenger_keeps_a_provisional_marker_even_when_its_gateway_is_dead(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """``runtime_pid: 0`` means a spawn may be alive with no recorded identity to check."""
    monkeypatch.setattr(_PC, "pid_liveness", lambda pid: _PC.PID_DEAD)
    _pin_start_ids(monkeypatch, "1")
    work_dir = warm._create_warm_generation_dir()
    _marker(work_dir, gateway_pid=424242, gateway_started="1", runtime_pid=0)

    assert warm._scavenge_warm_generation_dirs() == 0
    assert work_dir.is_dir()


@pytest.mark.parametrize(
    "corrupt",
    ["wrong_sentinel", "wrong_version", "list_body", "invalid_json", "oversized", "directory"],
)
def test_a_malformed_owner_marker_is_unowned_and_its_tree_is_kept(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt: str,
):
    monkeypatch.setattr(_PC, "pid_liveness", lambda pid: _PC.PID_DEAD)
    _pin_start_ids(monkeypatch, "1")
    work_dir = warm._create_warm_generation_dir()
    marker = warm._warm_generation_marker_path(work_dir)
    marker.unlink()
    good = {
        "sentinel": warm._WARM_GENERATION_SENTINEL,
        "version": warm._WARM_GENERATION_MARKER_VERSION,
        "gateway_pid": 11,
        "gateway_started": "1",
        "runtime_pid": 12,
        "runtime_started": "1",
    }
    if corrupt == "wrong_sentinel":
        marker.write_text(json.dumps({**good, "sentinel": "someone-else"}), encoding="utf-8")
    elif corrupt == "wrong_version":
        marker.write_text(json.dumps({**good, "version": 2}), encoding="utf-8")
    elif corrupt == "list_body":
        marker.write_text(json.dumps([good]), encoding="utf-8")
    elif corrupt == "invalid_json":
        marker.write_text("{not json", encoding="utf-8")
    elif corrupt == "oversized":
        padded = {**good, "pad": "x" * warm._WARM_GENERATION_MARKER_MAX_BYTES}
        marker.write_text(json.dumps(padded), encoding="utf-8")
    else:
        marker.mkdir()

    assert warm._read_warm_generation_owner(work_dir) is None
    assert warm._remove_warm_generation_dir(work_dir) is False
    assert warm._scavenge_warm_generation_dirs() == 0
    assert work_dir.is_dir()


def test_a_non_numeric_recorded_pid_keeps_the_tree(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(_PC, "pid_liveness", lambda pid: _PC.PID_DEAD)
    _pin_start_ids(monkeypatch, "1")
    work_dir = warm._create_warm_generation_dir()
    _marker(work_dir, gateway_pid="eleven", gateway_started="1", runtime_pid=12)

    assert warm._scavenge_warm_generation_dirs() == 0
    assert work_dir.is_dir()


@pytest.mark.parametrize("flagged", ["marker", "work_dir"])
def test_a_link_or_junction_at_the_marker_or_generation_is_never_owned(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    flagged: str,
):
    """The reparse/junction refusal is the Windows half of the no-follow rule; it is driven
    through the platform seam so the verdict is proven on every host."""
    monkeypatch.setattr(_PC, "pid_liveness", lambda pid: _PC.PID_DEAD)
    _pin_start_ids(monkeypatch, "1")
    work_dir = warm._create_warm_generation_dir()
    _marker(work_dir, gateway_pid=11, gateway_started="1", runtime_pid=12, runtime_started="1")
    target = warm._warm_generation_marker_path(work_dir) if flagged == "marker" else work_dir
    real = _PC.is_link_or_junction
    monkeypatch.setattr(_PC, "is_link_or_junction", lambda path: Path(path) == target or real(path))

    if flagged == "marker":
        assert warm._read_warm_generation_owner(work_dir) is None
    else:
        assert warm._is_plain_warm_generation_dir(work_dir) is False
    assert warm._remove_warm_generation_dir(work_dir) is False
    assert warm._scavenge_warm_generation_dirs() == 0
    assert work_dir.is_dir()


@pytest.mark.parametrize("level", ["generation", ".kiro", "agents", "marker"])
def test_a_failure_below_the_root_removes_the_half_built_generation(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
):
    """Every step after the random directory exists is inside the cleanup, so a refused
    ACL or marker write can never leave a loose, unowned generation behind."""
    real_restrict = _PC.restrict_dir_to_owner

    def _restrict(path: Any) -> None:
        name = Path(path).name
        if (level == "generation" and name.startswith("generation-")) or name == level:
            raise PermissionError(f"ACL write refused for {name}")
        real_restrict(path)

    monkeypatch.setattr(_PC, "restrict_dir_to_owner", _restrict)
    if level == "marker":

        def _refuse_write(path: Any, body: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(warm._agent, "_atomic_json_write", _refuse_write)

    with pytest.raises(OSError):
        warm._create_warm_generation_dir()

    generations = warm._warm_generations_dir()
    assert generations.is_dir()
    assert list(generations.iterdir()) == []


# ── spec ownership inside a private generation ──


def test_a_private_generation_spec_that_is_oversized_or_linked_reads_as_foreign(
    _agents_dir: Path,
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    work_dir = warm._create_warm_generation_dir()
    private_agents = warm._warm_generation_agents_dir(work_dir)
    ours = private_agents / f"{warm._WARM_BASE_AGENT}.json"
    ours.write_text(_ours_shaped_spec_text(warm._WARM_BASE_AGENT), encoding="utf-8")
    assert warm._warm_spec_is_foreign(ours) is False

    with pytest.MonkeyPatch.context() as capped:
        capped.setattr(hooks, "MAX_FILE_BYTES", ours.stat().st_size - 1)
        assert warm._warm_spec_is_foreign(ours) is True
    assert warm._warm_spec_is_foreign(ours) is False

    real = _PC.is_link_or_junction
    monkeypatch.setattr(_PC, "is_link_or_junction", lambda path: Path(path) == ours or real(path))
    assert warm._warm_spec_is_foreign(ours) is True


@pytest.mark.asyncio
async def test_a_foreign_private_spec_aborts_the_spawn_and_releases_its_generation(
    _agents_dir: Path,
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A spec is activated BY NAME, so a planted file at a planned name must stop the spawn
    before any runtime exists, and the generation created for it must not outlive it."""
    events: list[tuple[Any, ...]] = []
    built: list[Any] = []
    real_write = warm._write_warm_mint_specs

    def _write_then_plant(plan: Any, agents_dir: Path) -> None:
        real_write(plan, agents_dir)
        _mimic_spec(agents_dir, warm._WARM_BASE_AGENT)

    monkeypatch.setattr(warm, "_write_warm_mint_specs", _write_then_plant)
    monkeypatch.setattr(warm, "_log_warm_event", lambda *args, **kw: events.append(args))
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: built.append)

    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is None
    assert built == []
    assert ("connections_warm_mint_spawn", "unowned_specs:1", "refused") in events
    assert list(warm._warm_generations_dir().iterdir()) == []


# ── entry derivation: what a provider's registry entry asks the warm process for ──


def _preregistered(slug: str = "github") -> Any:
    return {
        "slug": slug,
        "mcp_url": f"https://{slug}.example/mcp",
        "auth": {"mode": "preregistered"},
        "l0_expectations": {"dcr": False},
    }


def _operator_client(slug: str) -> Any:
    from kiro_crew.connections.oauth_clients import ResolvedOAuthClient

    return ResolvedOAuthClient(
        slug=slug,
        client_id="operator-client",
        client_id_source="config",
        client_secret=None,
        client_secret_source=None,
        redirect_uri="http://127.0.0.1:43123/callback",
    )


@pytest.fixture
def _operator_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    """Resolve the operator's client from a recorded call instead of the real config/vault."""
    from kiro_crew import config, secrets
    from kiro_crew.config import loader
    from kiro_crew.connections import oauth_clients

    seen: list[Any] = []
    answer: list[Any] = [None]

    def _unreadable() -> dict[str, Any]:
        raise OSError("config.json is locked")

    def _resolve(provider: Any, *, config: Any, vault: Any) -> Any:
        seen.append(config)
        return answer[0]

    monkeypatch.setattr(loader, "read_config_for_update", _unreadable)
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(secrets, "SecretVault", lambda root: object())
    monkeypatch.setattr(oauth_clients, "resolve_oauth_client", _resolve)
    seen.append(answer)
    return seen


def test_a_preregistered_provider_without_an_operator_client_is_never_warmed(
    _operator_config: list[Any],
):
    """Nothing the warm process could authorize against: the card already says so. An
    unreadable config reads as "not configured" rather than failing the plan."""
    provider = _preregistered()
    assert warm._registry_server_entry(provider) is None
    assert warm._warm_mintable_entry(provider, None) is None
    assert _operator_config[1:] == [{}, {}], "the unreadable config was resolved as empty"


def test_a_preregistered_entry_carries_the_operator_client_and_vetoes_a_divergent_config(
    _operator_config: list[Any],
):
    provider = _preregistered()
    _operator_config[0][0] = _operator_client("github")

    entry = warm._registry_server_entry(provider)
    assert entry is not None
    assert warm._auth_shape(entry)[2] == "operator-client"
    # A Connect click stores ``{url}`` alone; compared as the runtime will see it, it matches.
    assert warm._warm_mintable_entry(provider, {"url": provider["mcp_url"]}) == entry
    assert warm._warm_mintable_entry(provider, {"url": "https://elsewhere.example/mcp"}) is None


def test_a_non_dcr_provider_warms_only_with_a_registry_client_id():
    bare = {"slug": "acme", "mcp_url": "https://acme.example/mcp", "l0_expectations": {}}
    assert warm._warm_mintable_entry(bare, None) is None  # type: ignore[arg-type]
    registered = {**bare, "client_id": "registry-client", "recommended_scopes": ["read"]}
    entry = warm._warm_mintable_entry(registered, None)  # type: ignore[arg-type]
    assert entry is not None
    assert warm._auth_shape(entry) == ("https://acme.example/mcp", ("read",), "registry-client")


# ── single flight: the runtime lock, the replacement budget, per-slug re-arms ──


@pytest.mark.asyncio
async def test_concurrent_activations_serialize_on_the_lock_and_spawn_once(
    _agents_dir: Path,
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    built: list[Any] = []

    class _Spawnable:
        pid = 4343

        def __init__(self, **kwargs: Any) -> None:
            built.append(self)

        async def spawn(self, start_priority=None) -> None:
            await asyncio.sleep(0)

        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs: Any) -> _QueuedOauthHandle:
            return _QueuedOauthHandle({"linear": 0})

        async def kill(self, **kwargs: Any) -> None:
            return None

    linear = _provider("linear")
    monkeypatch.setattr(warm, "_warm_candidate_scan", lambda: ([linear], [linear]))
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _Spawnable)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)
    _pin_start_ids(monkeypatch, "1")

    first, second = await asyncio.wait_for(
        asyncio.gather(warm._warm_mint.mint_for(), warm._warm_mint.mint_for()), 10
    )

    assert len(built) == 1, "the second activation must reuse the process the first spawned"
    assert first is not None and second is not None
    assert first.generation == second.generation
    assert first.activation != second.activation
    assert {first.activation, second.activation} <= set(warm._warm_mint._sessions)


@pytest.mark.asyncio
async def test_a_rearm_is_declined_while_an_explicit_warm_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    started: list[int] = []

    async def _rearm(generation: int) -> list[str]:
        started.append(generation)
        return []

    monkeypatch.setattr(warm, "_rearm_dead_warm_mint", _rearm)
    warm._warm_mint.arm_supervision()
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    await warm._warm_mint._lock.acquire()
    try:
        assert warm._warm_mint.start_rearm(3) is None
        assert warm._warm_mint._rearms_remaining == 1, "a declined re-arm spends no budget"
    finally:
        warm._warm_mint._lock.release()
    task = warm._warm_mint.start_rearm(3)
    assert task is not None
    await asyncio.wait_for(task, 5)
    assert started == [3]
    assert warm._warm_mint._rearms_remaining == 0


@pytest.mark.asyncio
async def test_a_rearm_outlives_the_reaper_that_started_it(monkeypatch: pytest.MonkeyPatch):
    """Replacing the dead generation cancels the observing reaper, so the replacement must be
    owned by the runtime, not by the task that happened to notice the death."""
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def _rearm(generation: int) -> list[str]:
        entered.set()
        await asyncio.wait_for(gate.wait(), 5)
        return ["linear"]

    monkeypatch.setattr(warm, "_rearm_dead_warm_mint", _rearm)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    warm._warm_mint.arm_supervision()
    monkeypatch.setattr(warm._warm_mint, "_generation", 6)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))

    reaper = asyncio.get_running_loop().create_task(warm._warm_mint_reaper(6))
    await asyncio.wait_for(entered.wait(), 5)
    rearm = warm._warm_mint._rearm_task
    assert rearm is not None
    reaper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reaper
    assert not rearm.cancelled()
    gate.set()
    assert await asyncio.wait_for(rearm, 5) == ["linear"]
    await asyncio.sleep(0)
    assert warm._warm_mint._rearm_task is None


@pytest.mark.asyncio
async def test_invalidation_rearms_are_single_flight_per_slug_not_across_slugs(
    monkeypatch: pytest.MonkeyPatch,
):
    release = asyncio.Event()
    started: list[str] = []

    async def _blocked(slug: str) -> list[str]:
        started.append(slug)
        await asyncio.wait_for(release.wait(), 5)
        return [slug]

    monkeypatch.setattr(warm, "_rearm_invalidated_provider", _blocked)
    async with _rearm_registry() as registry:
        warm.rearm_invalidated_provider("linear")
        warm.rearm_invalidated_provider("linear")
        warm.rearm_invalidated_provider("vercel")
        linear, vercel = registry["linear"], registry["vercel"]
        assert linear is not vercel
        for _ in range(3):
            await asyncio.sleep(0)
        assert sorted(started) == ["linear", "vercel"], "one task per slug, none serialized"

        other = asyncio.get_running_loop().create_future()
        warm._forget_invalidation_rearm("linear", other)  # type: ignore[arg-type]
        assert registry["linear"] is linear, "only the registered task may forget itself"

        release.set()
        await asyncio.wait_for(asyncio.gather(linear, vercel), 5)
        await asyncio.sleep(0)
        assert "linear" not in registry and "vercel" not in registry


# ── token-fenced rows: watchers, displacement, expiry, rollback ──


@pytest.mark.asyncio
async def test_absorb_arms_the_grant_watcher_with_the_claim_token(
    _stub_activation: None, monkeypatch: pytest.MonkeyPatch
):
    armed: list[tuple[str, str, str]] = []

    async def _watcher(slug: str, mcp_url: str, token: str) -> None:
        armed.append((slug, mcp_url, token))

    monkeypatch.setattr(warm, "_mint_watcher", _watcher)
    linear = _provider("linear", "https://linear.example/mcp")
    claims = await _claim("linear")
    minted = await warm._absorb_warm_requests(
        _result([linear], [{"serverName": "linear", "oauthUrl": "https://l/consent"}]), claims
    )
    await asyncio.sleep(0)

    assert minted == ["linear"]
    assert armed == [("linear", "https://linear.example/mcp", claims["linear"])]
    assert _mints["linear"]["token"] == claims["linear"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["displaced", "expired"])
async def test_a_warm_row_leaving_the_table_takes_its_watcher_with_it(
    _stub_activation: None,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
):
    """A watcher outliving its row expires the NEW row on the OLD row's deadline."""
    watcher = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    row = {
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://l/consent",
        "generation": 2,
        "activation": 1,
        "token": "old-row",
        "watcher": watcher,
    }
    _mints["linear"] = row  # type: ignore[assignment]
    if route == "displaced":
        monkeypatch.setattr(warm, "_warm_activate", _returns(None))
        assert await warm.warm_mint_all([_provider("linear")]) == []
    else:
        assert await warm._expire_shared_mints("mint_process_gone", generation=2) == ["linear"]
    await asyncio.sleep(0)

    assert watcher.cancelled()
    assert "watcher" not in row


@pytest.mark.asyncio
async def test_a_cancel_inside_redrain_recovery_releases_its_claims(
    _stub_activation: None, monkeypatch: pytest.MonkeyPatch
):
    async def _cancelled(result: Any, claims: dict[str, str]) -> list[str]:
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_absorb_warm_requests", _cancelled)
    late = _result([_provider("linear")], [{"serverName": "linear", "oauthUrl": "https://l/c"}])

    with pytest.raises(asyncio.CancelledError):
        await warm._recover_redrained_requests(late)
    assert _mints == {}


@pytest.mark.asyncio
async def test_a_tainted_late_frame_is_released_and_named_by_slug_only(
    _stub_activation: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """The re-drain path reaches the same credential gate as an activation."""
    events: list[tuple[Any, ...]] = []
    secret_url = "https://auth.example/authorize?client_secret=AKIAIOSFODNN7EXAMPLE"
    monkeypatch.setattr(warm, "_log_warm_event", lambda *args, **kw: events.append(args))
    monkeypatch.setattr(warm, "_credential_bearing_slugs", lambda urls: set(urls))
    late = _result([_provider("notion")], [{"serverName": "notion", "oauthUrl": secret_url}])

    with caplog.at_level("WARNING", logger=warm.logger.name):
        assert await warm._recover_redrained_requests(late) == []

    assert "notion" not in _mints
    assert ("connections_warm_mint_url", "provider:notion", "refused") in events
    assert all(secret_url not in record.getMessage() for record in caplog.records)
    assert all(secret_url not in repr(event) for event in events)


@pytest.mark.asyncio
async def test_a_baseline_with_an_unreadable_current_stat_is_not_proof(
    monkeypatch: pytest.MonkeyPatch,
):
    """With a baseline, only an OBSERVED change proves a new credential; an unreadable stat
    must never fall back to the revalidation spawn reserved for a missing baseline."""
    readings = iter([None, (1, 2), (3, 4)])

    async def _never(slug: str, mcp_url: str) -> bool:
        raise AssertionError("a baseline exists, so no revalidation may run")

    monkeypatch.setattr(mint, "grant_fingerprint", lambda url: next(readings))
    monkeypatch.setattr(mint, "_validate_existing_grant", _never)

    ledger: list[float] = []
    verdicts = [
        await mint._grant_change_proven("notion", "https://n/mcp", (1, 2), ledger) for _ in range(3)
    ]
    assert verdicts == [False, False, True]
    assert ledger == [], "a baseline is never spent on the revalidation ledger"


# ── parked generations and session bookkeeping ──


@pytest.mark.asyncio
async def test_the_drain_withdraws_a_parked_generation_that_died_while_parked(
    monkeypatch: pytest.MonkeyPatch,
):
    kills: list[Any] = []

    async def _kill(runtime: Any) -> bool:
        kills.append(runtime)
        return True

    dead = _Runtime(False)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm, "_kill_quietly", _kill)
    monkeypatch.setattr(warm, "_dispose_mint", _noop_dispose)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, dead)])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {9: _parked_session(5)})
    _mints["linear"] = {
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://l/consent",
        "generation": 5,
        "activation": 9,
        "token": "t",
    }

    await asyncio.wait_for(warm._drain_parked_generations(), 5)

    assert _mints["linear"]["state"] == "expired"
    assert _mints["linear"]["reason"] == "mint_process_gone"
    assert kills == [dead]
    assert warm._warm_mint._retiring == []
    assert warm._warm_mint._sessions == {}


async def _noop_dispose(entry: Any) -> None:
    entry.pop("watcher", None)


@pytest.mark.asyncio
async def test_killing_one_generation_forgets_only_its_own_sessions(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _kill(runtime: Any) -> bool:
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _kill)
    monkeypatch.setattr(
        warm._warm_mint, "_sessions", {1: _parked_session(5), 2: _parked_session(6)}
    )

    assert await warm._warm_mint._kill_generation(5, object()) is True
    assert set(warm._warm_mint._sessions) == {2}


@pytest.mark.asyncio
async def test_the_generation_number_advances_only_on_a_handed_over_spawn(
    _agents_dir: Path,
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class _Refused:
        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError("no kiro-cli")

    class _Spawnable:
        pid = os.getpid()

        def __init__(self, **kwargs: Any) -> None:
            pass

        async def spawn(self, start_priority=None) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)
    _pin_start_ids(monkeypatch, "1")
    before = warm._warm_mint._generation

    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _Refused)
    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is None
    assert warm._warm_mint._generation == before

    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _Spawnable)
    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is not None
    assert warm._warm_mint._generation == before + 1
    reaper = warm._warm_mint._reaper
    assert reaper is not None and not reaper.done()
    reaper.cancel()


# ── the composition: the facade re-exports each owner, and the owners stay leaves ──

_OWNERS: dict[str, tuple[str, ...]] = {
    "spec_plan": (
        "_WarmSpecPlan",
        "_auth_shape",
        "_operator_oauth_client",
        "_plan_is_servable",
        "_registry_server_entry",
        "_resident_roster_is_asked_for",
        "_wanted_aliases",
        "_warm_mintable_entry",
    ),
    "start_identity": (
        "_CURRENT_START_ID_RE",
        "_marker_process_start_id",
        "_process_identity_live",
        "_start_ids_comparable",
    ),
    "shared_rows": (
        "_LIVE_STATES",
        "_activations_in_use",
        "_claim_shared_mints",
        "_dispose_displaced_rows",
        "_generation_holds_live_rows",
        "_live_row_count",
        "_mint_is_adopted",
        "_mint_is_cold_held",
        "_release_shared_claims",
        "_shared_mints_pending",
        "_warm_table_row",
    ),
}

#: The one facade name an owner may reach at call time, and the owner that reaches it.
_FACADE_REACH = {"shared_rows": frozenset({"_dispose_mint"})}

#: Every name a test or caller substitutes on the warm facade. An owner calling one of them by
#: its bare name would bind its own copy and silently stop seeing the substitution.
_WARM_SEAMS = frozenset(
    {
        "_absorb_warm_requests",
        "_acp_runtime_factory",
        "_credential_bearing_slugs",
        "_destroy_session_quietly",
        "_dispose_mint",
        "_drain_parked_generations",
        "_kill_quietly",
        "_log_warm_event",
        "_mint_watcher",
        "_process_identity_live",
        "_read_agent_spec",
        "_rearm_dead_warm_mint",
        "_rearm_invalidated_provider",
        "_remove_warm_mint_specs",
        "_settle_invalidation_rearms",
        "_unowned_plan_specs",
        "_warm_activate",
        "_warm_candidate_scan",
        "_write_warm_mint_specs",
        "data_home",
        "declared_tool_aliases",
        "get_visible_providers",
        "grant_present",
        "list_servers",
        "mintable_providers",
        "oauth_url_contains_credential",
        "rearm_invalidated_provider",
        "scavenge_warm_mint_artifacts",
        "shutdown_warm_mint",
        "warm_mint_all",
        "warm_spec_providers",
    }
)


def _owner_module(name: str) -> Any:
    import importlib

    return importlib.import_module(f"kiro_crew.connections.warm_runtime.{name}")


def _owner_trees() -> dict[str, ast.Module]:
    from kiro_crew.connections import warm_runtime

    package = Path(warm_runtime.__file__).parent
    trees = {
        path.stem: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(package.glob("*.py"))
    }
    assert set(trees) == {"__init__", *_OWNERS}, "an owner was added or removed; pin it here"
    return trees


def _defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


@pytest.mark.parametrize("owner", sorted(_OWNERS))
def test_every_owner_name_is_re_exported_by_the_facade_as_the_same_object(owner: str):
    """Identity, not equality: the await-protection and claim-loop guards read the source of
    ``warm.<name>``, and a test patching ``warm.<name>`` must replace what callers call."""
    module = _owner_module(owner)
    for name in _OWNERS[owner]:
        assert getattr(warm, name) is getattr(module, name), f"warm.{name} is not {owner}.{name}"
    defined = _defined_names(_owner_trees()[owner]) - {"_warm_facade", "logger"}
    assert defined == set(_OWNERS[owner]), "every owner definition is re-exported, and only those"


def test_the_facade_keeps_binding_the_helpers_its_moved_rules_import():
    """``warm.<name>`` and ``from ... warm import *`` resolved these before the rules that call
    them moved into ``spec_plan``; they stay bound to the same objects."""
    from kiro_crew import mcp_utils
    from kiro_crew.connections import registry

    assert warm.is_preregistered is registry.is_preregistered
    for name in ("kiro_entry_client_id", "kiro_entry_scopes", "kiro_oauth_wire_entry"):
        assert getattr(warm, name) is getattr(mcp_utils, name), name


def test_the_owners_and_the_facade_read_one_mint_table():
    rows = _owner_module("shared_rows")
    assert rows._mints is warm._mints is mint._mints
    assert rows._mints_lock is warm._mints_lock is mint._mints_lock
    assert rows._new_mint_token is warm._new_mint_token is mint._new_mint_token
    assert rows.MintState is warm.MintState is mint.MintState


def test_pinned_constructs_stay_defined_where_their_gates_look():
    """Each of these is held in its facade by a repository gate keyed on the file: the
    link-screen baseline, the agent-spec call-site ratchet, the session-registry exemption,
    the agent-SDK boundary baseline, or a security-spec citation."""
    for name in (
        "_is_plain_warm_generation_dir",
        "_read_warm_generation_owner",
        "_read_warm_spec_body",
        "_warm_spec_plan",
        "_credential_bearing_slugs",
        "_WarmMintRuntime",
        "_warm_spec_is_foreign",
        "_recorded_runtime_is_dead",
        "_scavenge_warm_generation_dirs",
    ):
        assert getattr(warm, name).__module__ == "kiro_crew.connections.warm", name
    for name in (
        "_write_mint_agent_spec",
        "_agent_spec_entry_missing",
        "_acp_client_factory",
        "_log_mint_outcome",
        "pending_mint_for",
        "start_oauth_mint",
        "MintState",
    ):
        assert getattr(mint, name).__module__ == "kiro_crew.connections.mint", name
    assert warm.logger.name == "kiro_crew.connections.warm"


def test_the_owners_are_leaves_that_reach_the_facade_only_for_their_declared_seam():
    for owner, tree in _owner_trees().items():
        text = ast.unparse(tree) + (ast.get_docstring(tree) or "")
        # The session-registry gate imports and judges any module containing this text.
        assert "_sessions" not in text, f"{owner} must not name the warm session table"
        reached: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.col_offset == 0:
                module = node.module or ""
                assert not module.startswith(("kiro_crew.acp", "kiro_crew.providers")), owner
                assert module != "kiro_crew.connections.warm", f"{owner} imports the facade"
                assert not (
                    module == "kiro_crew.connections" and any(a.name == "warm" for a in node.names)
                ), f"{owner} imports the facade at module scope"
            if isinstance(node, ast.Import):
                assert not any(
                    a.name.startswith(("kiro_crew.acp", "kiro_crew.providers")) for a in node.names
                ), owner
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in _WARM_SEAMS, f"{owner} calls the seam {func.id} directly"
                assert func.id != "_read_agent_spec", f"{owner} adds a ratcheted spec read"
            if isinstance(func, ast.Attribute):
                receiver = func.value
                if (
                    isinstance(receiver, ast.Call)
                    and isinstance(receiver.func, ast.Name)
                    and receiver.func.id == "_warm_facade"
                ):
                    reached.add(func.attr)
                if func.attr == "getLogger":
                    assert [ast.literal_eval(a) for a in node.args] == [
                        "kiro_crew.connections.warm"
                    ], f"{owner} logs under a name the operator's filters do not know"
        assert reached == _FACADE_REACH.get(owner, frozenset()), owner


@pytest.mark.asyncio
async def test_the_claim_rollback_disposes_through_the_facade_seam(
    _stub_activation: None, monkeypatch: pytest.MonkeyPatch
):
    disposed: list[str] = []

    async def _recording(entry: Any) -> None:
        disposed.append(str(entry.get("token")))

    claims = await _claim("linear")
    monkeypatch.setattr(warm, "_dispose_mint", _recording)
    await warm._release_shared_claims(claims)
    await warm._dispose_displaced_rows([{"token": "displaced"}])  # type: ignore[list-item]

    assert disposed == [claims["linear"], "displaced"]
    assert "linear" not in _mints


def test_the_public_warm_surface_keeps_its_signatures():
    import inspect

    expected = {
        "adopt_shared_mint": ("(slug: 'str', mcp_url: 'str') -> 'str | None'", True),
        "connections_tool_aliases": ("(server_aliases: 'list[str]') -> 'dict[str, str]'", False),
        "expire_dead_mints": ("() -> 'list[str]'", True),
        "mintable_providers": ("() -> 'list[Provider]'", False),
        "rearm_invalidated_provider": ("(slug: 'str') -> 'None'", False),
        "scavenge_warm_mint_artifacts": ("() -> 'int'", False),
        "shutdown_warm_mint": ("() -> 'None'", True),
        "warm_mint_all": (
            "(providers: 'list[Provider] | None' = None, *, arm_supervision: 'bool' = True)"
            " -> 'list[str]'",
            True,
        ),
        "warm_spec_providers": ("() -> 'list[Provider]'", False),
        "_audited_mintable_providers": ("() -> 'tuple[list[Provider], bool]'", False),
    }
    for name, (signature, is_coroutine) in expected.items():
        func = getattr(warm, name)
        assert str(inspect.signature(func)) == signature, name
        assert inspect.iscoroutinefunction(func) is is_coroutine, name


def _python(probe: str, cwd: Path) -> str:
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        cwd=cwd,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip()


def test_each_owner_imports_cold_without_loading_the_facade(tmp_path: Path):
    """A cold import is what the session-registry gate and any direct importer perform; an
    owner that needed the facade at import time would fail it with a circular import."""
    owners = ", ".join(f"'kiro_crew.connections.warm_runtime.{name}'" for name in _OWNERS)
    probe = (
        "import importlib, sys\n"
        f"for name in ({owners}):\n"
        "    importlib.import_module(name)\n"
        "print('FACADE' if 'kiro_crew.connections.warm' in sys.modules else 'LEAF')\n"
        "from kiro_crew.connections import warm\n"
        "from kiro_crew.connections.warm_runtime import shared_rows\n"
        "print(warm._claim_shared_mints is shared_rows._claim_shared_mints)\n"
    )
    assert _python(probe, tmp_path).splitlines()[-2:] == ["LEAF", "True"]


def test_no_boot_path_module_loads_any_warm_or_mint_module(tmp_path: Path):
    """The facades' own boot probes match exact module names; this one matches the prefix, so
    an owner reached from the boot path without its facade is caught too."""
    probe = (
        "import sys\n"
        "import kiro_crew.dashboard.handlers, kiro_crew.mcp_discovery\n"
        "import kiro_crew.connections.status, kiro_crew.connections.ownership\n"
        "prefixes = ('kiro_crew.connections.warm', 'kiro_crew.connections.mint')\n"
        "leaked = sorted(m for m in sys.modules if m.startswith(prefixes))\n"
        "print('LEAKED:' + ','.join(leaked) if leaked else 'CLEAN')\n"
    )
    assert _python(probe, tmp_path).endswith("CLEAN")
