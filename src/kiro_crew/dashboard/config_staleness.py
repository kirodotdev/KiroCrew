"""Whether a live chat runs on outdated config, for the dashboard badge and agents.

The detection is :mod:`kiro_crew.dashboard.stale_config`; this module ties it to
a chat slot. A turn records the fingerprint of the config its provider was
spawned under (:func:`record_spawn_config`), and :func:`refresh_config_stale`
recomputes it and publishes ``config_stale`` and the display-safe labels of what
changed on the slot. It runs at each turn's end, on request, right after the
gateway itself writes config (:func:`config_write_refresh_middleware`), and on a
stat-guarded sweep every :data:`CONFIG_STALE_SWEEP_SECS` for edits made outside
it (:func:`config_stale_sweep_loop`), so an idle chat is badged too. Nothing here relaunches a
process: applying a change is the person's Reload action, as it always was.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.agent import ensure_agent_materialized, require_fresh_derived_spec
from kiro_crew.agent_sdk import SpawnConfigCarrier
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import kiro_agents_dir, project_agents_dir
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.stale_config import (
    DEFAULT_BACKEND_KEY,
    ConfigFingerprint,
    SpawnConfigRecord,
    SpawnInputs,
    adopt_first_reads,
    carry_forward,
    changed_inputs,
    compute_fingerprint,
    input_signature,
    is_stale,
    make_record,
)
from kiro_crew.mcp_hot_reload import provider_hot_reloads

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: How often the sweep looks for config edits made outside the gateway (a
#: hand-edited agent spec or ``mcp.json``). Each tick costs a few ``lstat``
#: calls per live chat; a chat is re-fingerprinted only when one changed.
CONFIG_STALE_SWEEP_SECS = 60.0

#: At most this many chats are re-fingerprinted at once, each off the loop.
_REFRESH_CONCURRENCY = 4

#: Request paths through which the gateway itself writes an agent spec or an
#: MCP server a chat's fingerprint reads. A successful write through any of them
#: refreshes every live chat at once. The gateway config (the ACP backend) is not
#: here: :func:`subscribe_backend_changes` hears it from the process ConfigWatch,
#: whichever writer changed it. Neither are the ``/api/mcp-gateway/`` writes: they
#: touch ``mcp_gateway.*`` in ``config.json`` or an in-process resolve cache,
#: inputs the fingerprint deliberately leaves out.
_CONFIG_WRITE_PREFIXES = (
    "/api/agents",
    "/api/agent/config",
    "/api/capability/",
    "/api/mcp/",
)

#: The ``config.json`` paths a fingerprint reads: the ACP backend a chat is
#: compared against (``current_config_fingerprint``).
_BACKEND_CONFIG_PATHS = (DEFAULT_BACKEND_KEY, "agent.member_acp_backend")
_MEMBER_BACKEND_KEY = _BACKEND_CONFIG_PATHS[1]
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def turn_spawn_inputs(slot: "_ChatSlot", kiro_agent: str | None) -> SpawnInputs:
    """The agent and project a turn hands the provider factory.

    ``kiro_agent`` first -- it is the spec that answers for a crew alias -- then
    the slot's own pick, then the name the ACP provider falls back to when it
    is handed none, so the spec the fingerprint hashes is the one kiro-cli loads.
    """
    return SpawnInputs(agent=kiro_agent or slot.agent or "kirocrew", project=slot.project or "")


def current_config_fingerprint(
    cfg: Any, session_key: str, inputs: SpawnInputs
) -> ConfigFingerprint:
    """The fingerprint a session for *inputs* spawned right now would carry.

    Blocking file IO: run off the loop. The backend is resolved through the
    same member-aware gate the provider factory uses, so a member DM routed to
    ``agent.member_acp_backend`` is compared against the backend it actually got.
    """
    # circular import: members sits above config in the layering.
    from kiro_crew.members import is_member_session_key, select_provider_backend

    backend = select_provider_backend(
        session_key, cfg.agent.member_acp_backend, cfg.agent.acp_backend
    )
    # The key named when the backend changes: a member DM resolves from its own.
    backend_key = _MEMBER_BACKEND_KEY if is_member_session_key(session_key) else DEFAULT_BACKEND_KEY
    return compute_fingerprint(inputs, backend=backend, backend_key=backend_key)


def spawn_config_fingerprint(cfg: Any, session_key: str, inputs: SpawnInputs) -> ConfigFingerprint:
    """:func:`current_config_fingerprint` of the config a process about to spawn reads.

    A spawn runs two steps before its child starts: it heals a missing managed
    default spec (:func:`kiro_crew.agent.ensure_agent_materialized`) and then
    passes the freshness gate (:func:`kiro_crew.agent.require_fresh_derived_spec`),
    which re-derives a worker mirror older than the default. Both run here first,
    in the spawn's own order, so the fingerprint hashes the specs the child will
    read: one taken before them records a spec as absent or outdated, and the
    turn's end, reading it back healed, would flag the chat stale with no config
    change behind it. Each is a no-op for a spec already current and for any agent
    neither step covers. A gate refusal raises, as it does for the spawn, which is
    refused too; the callers record no fingerprint then. Blocking file IO: run
    off the loop.
    """
    ensure_agent_materialized(inputs.agent)
    require_fresh_derived_spec(inputs.agent, inputs.project)
    return current_config_fingerprint(cfg, session_key, inputs)


def records_provider(slot: "_ChatSlot", provider: object) -> bool:
    """Whether *slot* already holds the spawn record for *provider*."""
    record = slot._spawn_config
    return isinstance(record, SpawnConfigRecord) and record.describes(provider)


def record_spawn_config(
    slot: "_ChatSlot",
    provider: SpawnConfigCarrier,
    inputs: SpawnInputs,
    fingerprint: ConfigFingerprint,
) -> None:
    """Record *fingerprint* for *provider* unless a record already describes it.

    Only a provider the slot has no record for is recorded, so the record stays
    the config the process was started under: a later turn on the same provider
    must not overwrite it with newer config, or the difference it exists to
    detect would be erased.

    A process claimed from the warm pool carries the fingerprint the pool took
    before starting it (``pool_spawn_config``, attached by
    ``session_pool._fill_warm_pool`` through :func:`pool_spawn_config`). That
    one is recorded instead, so an edit made between the pool's spawn and the
    claim is seen. It is used only when it was taken for the same spawn
    selection; otherwise the caller's own fingerprint is recorded, which only
    ever errs toward a missed difference.
    """
    if records_provider(slot, provider):
        return
    pooled = provider.pool_spawn_config
    if (
        isinstance(pooled, tuple)
        and len(pooled) == 2
        and pooled[0] == inputs
        and isinstance(pooled[1], ConfigFingerprint)
    ):
        fingerprint = pooled[1]
    slot._spawn_config = make_record(provider, inputs, fingerprint)


def pool_spawn_config(agent: str, cwd: str) -> tuple[SpawnInputs, ConfigFingerprint]:
    """The fingerprint a warm-pool process for *agent* in *cwd* starts under.

    Installed as ``SessionManager.spawn_config_reader``. A pooled process is
    spawned with no session key, so it runs the factory's default backend (the
    pool refuses member keys for that reason), and the inputs mirror what
    :func:`turn_spawn_inputs` yields for a chat that can claim it. Blocking
    file IO: the pool calls it off the loop.
    """
    cfg = KiroCrewConfig.load()
    inputs = SpawnInputs(agent=agent or "kirocrew", project=cwd or "")
    return inputs, spawn_config_fingerprint(cfg, "", inputs)


def display_input(name: str, fingerprint: ConfigFingerprint) -> str:
    """A display-safe label for one config input: relative or ``~`` paths only."""
    parts = dict(fingerprint.parts)
    if name == "backend":
        return parts.get("backend_key") or DEFAULT_BACKEND_KEY
    if name == "workspace_mcp":
        return ".kiro/settings/mcp.json"
    if name == "global_mcp":
        return "~/.kiro/settings/mcp.json"
    raw = parts.get("spec_path", "")
    if not raw:
        return "agent spec"
    path = Path(raw)
    if parts.get("spec_ws"):
        # The workspace scope, relative to the project root it lives under.
        return (project_agents_dir(".") / path.name).as_posix()
    # The user-level scope, with the home directory shown as ``~``.
    agents_dir = kiro_agents_dir()
    try:
        return "~/" + (agents_dir.relative_to(Path.home()) / path.name).as_posix()
    except ValueError:
        return f"{agents_dir.name}/{path.name}"


async def config_stale_status(state: "DashboardState", slot: "_ChatSlot") -> dict[str, Any]:
    """Whether *slot*'s live session runs on config that has changed since it started.

    Compares the live provider's recorded fingerprint with the one the SAME
    spawn inputs (``SpawnConfigRecord.inputs``) would give now. A session with
    no live process, or one no turn has recorded yet, is not stale: its next
    start reads current config. A change the provider applies live (MCP edits
    on a hot-reloading kiro-cli) is not stale either.

    ``changed`` names what differs with display-safe labels. ``unreadable``
    names inputs that exist but could not be read; while any does and nothing
    else is stale, ``stale`` is ``None`` (unknown), never ``False``. Never
    raises: a recompute that fails reads as unknown too.
    """
    session_key = effective_session_key(slot)
    provider = state.sessions.get_provider(session_key)
    record = slot._spawn_config
    if (
        provider is None
        or not isinstance(record, SpawnConfigRecord)
        or not record.describes(provider)
    ):
        return {"stale": False, "changed": [], "unreadable": []}
    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        current = await asyncio.to_thread(
            current_config_fingerprint, cfg, session_key, record.inputs
        )
    except Exception:
        logger.warning("Config fingerprint failed for slot %s", slot.key, exc_info=True)
        return {"stale": None, "changed": [], "unreadable": []}
    # The reads above suspend. A Reload (or any reset) that lands meanwhile
    # replaces the process and clears the badge; this answer describes the
    # process that is gone, so it is dropped rather than published on the new one.
    if (
        effective_session_key(slot) != session_key
        or state.sessions.get_provider(session_key) is not provider
        or slot._spawn_config is not record
    ):
        return {"stale": None, "changed": [], "unreadable": []}
    baseline = adopt_first_reads(record.fingerprint, current)
    if baseline is not record.fingerprint:
        # An input unreadable at spawn is recorded on its first read, once:
        # adopting it again on every check would hide each later edit to it.
        record.fingerprint = baseline
    compared = carry_forward(baseline, current)
    stale = is_stale(baseline, compared, hot_reloads=provider_hot_reloads(provider))
    changed = (
        sorted(
            {display_input(n, compared) for n in changed_inputs(baseline, compared)}
            or {"session configuration"}
        )
        if stale
        else []
    )
    return {
        "stale": True if stale else (None if current.unreadable else False),
        "changed": changed,
        "unreadable": sorted(display_input(n, current) for n in current.unreadable),
    }


def set_config_stale(
    state: "DashboardState", slot: "_ChatSlot", stale: bool, changed: list[str]
) -> None:
    """Publish the badge state, broadcasting only on a change."""
    inputs = ", ".join(changed) if stale else ""
    if slot.config_stale != stale or slot.config_stale_inputs != inputs:
        slot.config_stale = stale
        slot.config_stale_inputs = inputs
        state.push_slots_update()


async def refresh_config_stale(state: "DashboardState", slot: "_ChatSlot") -> dict[str, Any]:
    """Recompute :func:`config_stale_status` and publish it on the slot.

    An unknown reading (``stale`` None) leaves the badge as it was rather than
    flapping it. Returns the status.
    """
    status = await config_stale_status(state, slot)
    if status["stale"] is not None:
        set_config_stale(state, slot, bool(status["stale"]), status["changed"])
    return status


def _live_record(state: "DashboardState", slot: "_ChatSlot") -> SpawnConfigRecord | None:
    """*slot*'s record when it describes the slot's live process, else ``None``."""
    record = slot._spawn_config
    provider = state.sessions.get_provider(effective_session_key(slot))
    if provider is None or not isinstance(record, SpawnConfigRecord):
        return None
    return record if record.describes(provider) else None


async def refresh_all_config_stale(state: "DashboardState", *, guarded: bool) -> int:
    """Refresh the badge of every chat with a live, recorded process. Returns how many.

    A chat mid-turn is skipped (its turn's end refreshes it). A chat with no
    live process, or none recorded yet, has nothing to compare: its badge is
    cleared, so a chat whose session expired idle does not keep one. With *guarded*,
    a chat whose inputs' stat signature (:func:`stale_config.input_signature`)
    is unchanged since its last check is skipped too, so a sweep over idle chats
    costs a few ``lstat`` calls each; a gateway-side write passes
    ``guarded=False`` and re-checks every chat. Never raises.
    """
    sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)
    refreshed = 0

    async def _one(slot: "_ChatSlot") -> None:
        nonlocal refreshed
        record = _live_record(state, slot)
        if record is None:
            # No live process (an idle session that expired, a reset) or no
            # record of one: nothing runs on outdated config, so a badge left
            # from before is cleared.
            set_config_stale(state, slot, False, [])
            return
        if slot.running or getattr(slot, "_in_stage_execution", False):
            return
        async with sem:
            try:
                # Before the fingerprint: an edit landing between the two is a
                # signature change the next tick sees, never one it misses.
                signature = await asyncio.to_thread(input_signature, record.inputs)
                if guarded and signature == slot._config_stat_sig:
                    return
                status = await refresh_config_stale(state, slot)
            except Exception:  # noqa: BLE001 - a change detector never fails the sweep
                logger.debug("Config stale refresh failed for slot %s", slot.key, exc_info=True)
                return
            refreshed += 1
            # A reading left unknown by an input that cannot be read is still
            # an answer for this signature: without the guard such a chat would
            # be re-fingerprinted on every tick until the input changes, which
            # moves the signature. An unknown left by a failed or superseded
            # recompute (nothing named unreadable) is retried next tick.
            if status["stale"] is not None or status["unreadable"]:
                slot._config_stat_sig = signature

    await asyncio.gather(*(_one(slot) for slot in list(state._slots.values())))
    return refreshed


def schedule_refresh_all(state: "DashboardState") -> None:
    """Refresh every live chat's badge in the background, coalescing bursts.

    A write burst (a multi-server MCP sync) schedules one pass, plus at most
    one more when a write lands while that pass runs, so the last write is
    always seen.
    """
    if getattr(state, "_config_stale_refresh_running", False):
        state._config_stale_refresh_again = True
        return

    async def _run() -> None:
        try:
            while True:
                state._config_stale_refresh_again = False
                await refresh_all_config_stale(state, guarded=False)
                if not state._config_stale_refresh_again:
                    break
        finally:
            state._config_stale_refresh_running = False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # Set before the task runs, so a second write in the same tick coalesces.
    state._config_stale_refresh_running = True
    task = loop.create_task(_run())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def is_config_write(method: str, path: str) -> bool:
    """Whether a request is one of the gateway's own config writes."""
    return method not in _READ_METHODS and path.startswith(_CONFIG_WRITE_PREFIXES)


def config_write_refresh_middleware(state: "DashboardState") -> Any:
    """A middleware refreshing every live chat's badge after a successful config write."""

    @web.middleware
    async def _config_write_refresh(request: web.Request, handler: Any) -> web.StreamResponse:
        response = await handler(request)
        if is_config_write(request.method, request.path) and response.status < 400:
            schedule_refresh_all(state)
        return response

    # Marked so a test of a built app can find it, as ``_is_token_auth`` is.
    _config_write_refresh._is_config_write_refresh = True  # type: ignore[attr-defined]
    return _config_write_refresh


async def config_stale_sweep_loop(
    state: "DashboardState", interval: float = CONFIG_STALE_SWEEP_SECS
) -> None:
    """Every *interval* seconds, re-check chats whose config files' stats changed."""
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_all_config_stale(state, guarded=True)
        except Exception:  # noqa: BLE001 - one bad tick must not end the sweep
            logger.warning("Config stale sweep tick failed", exc_info=True)


def subscribe_backend_changes(state: "DashboardState") -> live.Subscription:
    """Refresh every live chat when ConfigWatch sees the ACP backend change.

    ``config.json`` has one watcher in the gateway (:mod:`kiro_crew.config.live`);
    this subscribes to it rather than stat'ing the file again, so an edit from
    any writer -- a route, the CLI, ``$EDITOR`` -- reaches the badge on its next
    reload. The caller holds the returned subscription.
    """

    def _on_backend_change(change: live.ConfigChange) -> None:
        if change.touched(*_BACKEND_CONFIG_PATHS):
            schedule_refresh_all(state)

    return live.subscribe(
        *_BACKEND_CONFIG_PATHS, callback=_on_backend_change, name="stale-config-backend"
    )


async def stop_config_stale_detection(state: "DashboardState") -> None:
    """Cancel and await the sweep, and cancel the ConfigWatch subscription.

    The gateway's cleanup hook: neither may outlive the app it was armed for.
    """
    sub = getattr(state, "_config_stale_backend_sub", None)
    if sub is not None:
        sub.cancel()
        state._config_stale_backend_sub = None
    task = getattr(state, "_config_stale_sweep", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        state._config_stale_sweep = None


def start_config_stale_sweep(state: "DashboardState") -> "asyncio.Task[None]":
    """Start :func:`config_stale_sweep_loop` as a tracked gateway task."""
    task = asyncio.get_running_loop().create_task(config_stale_sweep_loop(state))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return task
