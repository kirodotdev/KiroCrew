"""Chat runtime sharing telemetry: closed values, and every call site firing.

Each call-site test drives the production function that owns the step and
asserts the counter it should add, so deleting or moving an emit reddens here.
The value lists are pinned against the vocabularies they mirror, so a new
refusal branch, stop reason or backend fails here instead of exporting
``other``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import kiro_crew.runtime_ownership as ro
from kiro_crew.metrics import chat_runtime_share as crs
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


class _Rec:
    enabled = True

    def __init__(self) -> None:
        self.counters: list[dict] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "attrs": dict(attrs or {})})

    def histogram(self, *a, **k) -> None:
        pass


@pytest.fixture
def rec():
    r = _Rec()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


@pytest.fixture(autouse=True)
def _fresh_tables():
    ro._reset_for_tests()
    crs._in_use = False
    yield
    ro._reset_for_tests()
    crs._in_use = False


def _named(rec: _Rec, name: str) -> list[dict]:
    return [c["attrs"] for c in rec.counters if c["name"] == name]


class _Runtime:
    """What the ownership table probes: ``pid`` and ``is_alive``."""

    def __init__(self, offset: int = 0) -> None:
        self.pid = UNALLOCATABLE_PID + offset

    def is_alive(self) -> bool:
        return True


def _leases(runtime: Any, n: int, *, key: Any = "share-key") -> None:
    """Put *n* chat leases on *runtime* under *key*, as placement does."""

    async def spawn():
        return runtime

    async def take():
        for i in range(n):
            await ro.RUNTIME_OWNERSHIP.acquire(key, f"dashboard:chat-{i}-1", spawn, cap=10)

    asyncio.run(take())


# -- closed value lists ---------------------------------------------------------


def test_refusal_reasons_are_every_branch_the_eligibility_rule_returns():
    from kiro_crew.acp import chat_runtime_sharing as sharing

    tree = ast.parse(inspect.getsource(sharing.chat_sharing_ineligible_reason))
    returned = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.value
    }
    assert returned, "the AST harvest found nothing; this guard would be vacuous"
    assert set(crs.REFUSAL_REASONS) == returned | {"native_subagents"}


def test_metrics_spells_no_preview_reader_of_its_own():
    """The preview is read only by ``chat_runtime_sharing_enabled``, through agent_sdk."""
    assert not hasattr(crs, "PREVIEW_ID") and not hasattr(crs, "preview_on")


def test_turn_labels_come_from_the_shared_classifiers():
    """The stall and harness splits use the classifiers ``turn.completed`` uses."""
    from kiro_crew.metrics.harness import harness_for_backend
    from kiro_crew.metrics.turns import turn_outcome

    for stop in (
        "end_turn",
        "stop",
        "cancelled",
        "error: tool stall",
        "stale_recover",
        "timeout",
        "request timeout",
        "error: cancel unacked",
        None,
    ):
        assert crs._turn_outcome(stop) == turn_outcome(stop)
    assert crs._turn_outcome(5) == "error"
    assert not hasattr(crs, "BACKENDS") and not hasattr(crs, "TURN_OUTCOMES")
    assert harness_for_backend("") == "kiro"


@pytest.mark.parametrize(
    ("fn", "values"),
    [
        (crs.tenants_bucket, crs.TENANT_BUCKETS),
        (crs.delay_bucket, crs.DELAY_BUCKETS),
        (crs.rss_bucket, crs.RSS_BUCKETS),
    ],
)
def test_every_bucket_answers_inside_its_own_set(fn, values):
    for raw in (
        0,
        1,
        2,
        3,
        4,
        99,
        0.5,
        1.0,
        59.9,
        60.0,
        61,
        512,
        513,
        9000,
        -1,
        None,
        "x",
        True,
        float("nan"),
        float("inf"),
        object(),
        "",
        "end_turn",
        "error: x",
    ):
        assert fn(raw) in (*values, crs.OTHER)


def test_bucket_edges_are_inclusive():
    assert crs.delay_bucket(1.0) == "le_1s"
    assert crs.delay_bucket(1.01) == "le_2s"
    assert crs.delay_bucket(60.0) == "le_60s"
    assert crs.delay_bucket(60.01) == "gt_60s"
    assert crs.delay_bucket(-3) == "le_1s"
    assert crs.rss_bucket(512) == "le_512m"
    assert crs.rss_bucket(8193) == "gt_8g"
    assert crs.tenants_bucket(1) == "1"
    assert crs.tenants_bucket(7) == "4+"
    assert crs.tenants_bucket(True) == crs.OTHER


# -- adoption -------------------------------------------------------------------


def _cfg(on: bool):
    from kiro_crew.acp.chat_runtime_sharing import CHAT_RUNTIME_SHARING_PREVIEW
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.dashboard.feature_previews = {CHAT_RUNTIME_SHARING_PREVIEW: True} if on else {}
    return cfg


def _toggle_applier():
    from aiohttp import web

    from kiro_crew.dashboard.server import _register_config_watch

    app = web.Application()
    state = SimpleNamespace(workflow_service=None, channel_manager=None)
    state.set_dynamic_cards_enabled = MagicMock()
    _register_config_watch(app, state, None)
    subs = {s.name: s for s in app["config_watch_subscriptions"]}
    return subs["dashboard.chat_runtime_sharing_toggle"].callback()


def test_the_dashboard_counts_each_flip_from_any_writer_and_no_held_value(rec):
    from kiro_crew.config.live import ConfigChange

    apply = _toggle_applier()
    changed = frozenset({"dashboard.feature_previews.chat_runtime_sharing"})
    for old, new in ((False, True), (True, True), (True, False)):
        apply(ConfigChange(old=_cfg(old), new=_cfg(new), changed=changed))
    assert _named(rec, crs.TOGGLED) == [{"state": "on"}, {"state": "off"}]


# -- placement --------------------------------------------------------------------


def test_a_founding_and_a_joining_placement_are_counted_with_their_tenants(rec):
    from chat_runtime_sharing_helpers import confirming, make_placement

    rt = _Runtime(1)

    async def spawn():
        return rt

    async def scenario():
        for n in (1, 2):
            p = make_placement(session_key=f"dashboard:chat-{n}-1790000000")
            p._read_confirmation = confirming(("gen-1", "acct-A", "auto", False, True))
            await p.place(spawn)

    asyncio.run(scenario())
    assert _named(rec, crs.PLACED) == [
        {"outcome": "spawned", "reason": "none", "tenants": "1"},
        {"outcome": "joined", "reason": "none", "tenants": "2"},
    ]


def test_a_refused_start_is_counted_with_its_reason(rec):
    from chat_runtime_sharing_helpers import _PlacementProvider

    from kiro_crew.acp_backends import ACP_BACKEND_KAS
    from kiro_crew.providers import chat_share
    from kiro_crew.providers.chat_share import resolve_chat_share

    async def scenario():
        with (
            patch("kiro_crew.config.live.snapshot", return_value=_cfg(True)),
            patch.object(chat_share, "kas_chat_sharing_ready", return_value=True),
        ):
            return await resolve_chat_share(
                _PlacementProvider(backend=ACP_BACKEND_KAS),
                work_dir="/home/u/.kirocrew/workspace",
                agent="kirocrew",
                sandbox_mode="auto",
                extra_env={},
                mcp_gateway_overlay=None,
                mcp_gateway_socket=None,
                configured_model="",
                launch_agent="",
                remote_workspace=True,
            )

    assert asyncio.run(scenario()) is None
    assert _named(rec, crs.PLACED) == [
        {"outcome": "refused", "reason": "session_environment", "tenants": "none"}
    ]


def test_the_preview_off_counts_no_placement(rec):
    from chat_runtime_sharing_helpers import _PlacementProvider

    from kiro_crew.providers.chat_share import resolve_chat_share

    async def scenario():
        with patch("kiro_crew.config.live.snapshot", return_value=_cfg(False)):
            return await resolve_chat_share(
                _PlacementProvider(),
                work_dir="/w",
                agent="kirocrew",
                sandbox_mode="auto",
                extra_env={},
                mcp_gateway_overlay=None,
                mcp_gateway_socket=None,
                configured_model="",
                launch_agent="",
                remote_workspace=True,
            )

    assert asyncio.run(scenario()) is None
    assert _named(rec, crs.PLACED) == []
    assert crs.read_census() is None, "an unused preview must publish no census"


# -- effect: the census gauges -----------------------------------------------------


def _collect() -> dict[str, list]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    crs.register_chat_runtime_share_gauges(provider.get_meter("test"))
    data = reader.get_metrics_data()
    provider.shutdown()
    out: dict[str, list] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = [dp.value for dp in metric.data.data_points]
    return out


def test_the_census_counts_shared_processes_and_their_chats_only():
    shared, alone, unshared = _Runtime(1), _Runtime(2), _Runtime(3)
    _leases(shared, 3, key="key-a")
    _leases(alone, 1, key="key-b")
    # A registration lease is keyed by the runtime itself: not placed by sharing.
    _leases(unshared, 1, key=unshared)

    assert _collect() == {}, "nothing is published before this process used sharing"
    crs._note_sharing_in_use()
    got = _collect()
    assert got[crs.GAUGE_PROCESSES] == [2]
    assert got[crs.GAUGE_TENANTS] == [4]


# -- effect: memory, reclaim, crashes ----------------------------------------------


def test_a_shared_process_tick_is_sampled_and_its_reclaim_counted_once(rec):
    from kiro_crew.session_cleanup_spare import note_rss_measured, rss_limit_for

    shared, unshared = _Runtime(501), _Runtime(502)
    _leases(shared, 2, key="key-a")
    _leases(unshared, 1, key="key-b")
    note_rss_measured(shared.pid, 3000)
    note_rss_measured(unshared.pid, 3000)
    assert _named(rec, crs.RSS_SAMPLED) == [{"tenants": "2", "rss": "le_4g"}]

    cleanup = SimpleNamespace(state=SimpleNamespace(rss_max_mb=1000))
    for _ in range(2):  # two co-tenants read the same over-ceiling figure
        assert rss_limit_for(cleanup, shared.pid, 3000) == 2000
    assert _named(rec, crs.RECLAIMED) == [{"reason": "rss"}]


def test_a_death_is_counted_with_the_chats_it_took_down(rec):
    from kiro_crew import runtime_death

    runtime_death.announce(
        _Runtime(9), reason="process exited (rc=1)", expected=False, leases=3, acp_sessions=3
    )
    runtime_death.announce(
        _Runtime(10), reason="runtime stopped", expected=True, leases=1, acp_sessions=1
    )
    assert _named(rec, crs.PROCESS_DIED) == [
        {"expected": "false", "tenants": "3"},
        {"expected": "true", "tenants": "1"},
    ]


class _DeadRuntime(_Runtime):
    def is_alive(self) -> bool:
        return False


def test_a_reaped_shared_crash_still_counts_the_chats_it_held(rec):
    """The exit watcher reaps first, so the kill gate's live-only count reads 0."""
    from kiro_crew import runtime_death

    runtime = _Runtime(11)
    _leases(runtime, 3, key="key-a")
    runtime.__class__ = _DeadRuntime  # the process exited before _mark_dead ran
    assert ro.outstanding_leases(runtime) == 0
    runtime_death.announce(
        runtime, reason="process exited (rc=1)", expected=False, leases=0, acp_sessions=3
    )
    assert _named(rec, crs.PROCESS_DIED) == [{"expected": "false", "tenants": "3"}]


# -- lag and stalls: one real turn through the session handle -------------------


class _Clock:
    """The turn module's monotonic clock, moved by the backend instead of by sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Backend:
    """Answers one prompt with a chunk and *stop*, *delay* clock-seconds after it was sent."""

    def __init__(
        self, queue: asyncio.Queue, clock: _Clock, *, delay: float, stop: str, backend: str
    ) -> None:
        self.pid = UNALLOCATABLE_PID + 4343
        self.is_alive = MagicMock(return_value=True)
        self.supports_image_prompt = False
        self.acp_backend = backend
        self._last_activity = time.monotonic()
        self._queue = queue
        self._clock = clock
        self._delay = delay
        self._stop = stop

    def session_activity_at(self, session_id: str):
        return None

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_request(self, method: str, params: dict) -> int:
        from kiro_crew.acp.types import METHOD_SESSION_UPDATE, JsonRpcMessage

        async def answer():
            # Runs once the turn waits on its queue, after it stamped the send.
            self._clock.now += self._delay
            self._queue.put_nowait(
                JsonRpcMessage(
                    method=METHOD_SESSION_UPDATE,
                    params={
                        "sessionId": "sA",
                        "update": {"sessionUpdate": "agent_message_chunk", "text": "pong"},
                    },
                )
            )
            self._queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": self._stop}))

        asyncio.ensure_future(answer())
        return 1

    async def send_notification(self, method: str, params: dict) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leases", "session_key", "delay", "stop", "backend", "want"),
    [
        (2, "dashboard:chat-0-1", 12.0, "end_turn", "kas", ("2", "le_20s", "ok", "kas")),
        # A subagent on its host chat's shared process holds no lease there.
        (2, "subagent:sub-1", 0.0, "end_turn", "kas", ("0", "le_1s", "ok", "kas")),
        (0, "", 0.0, "error: tool stall", "", ("0", "le_1s", "tool_stall", "kiro")),
    ],
)
async def test_a_turn_counts_its_start_delay_and_how_it_ended(
    rec, leases, session_key, delay, stop, backend, want
):
    from kiro_crew.acp import session_handle as handle_facade
    from kiro_crew.acp.session_handle import AcpSessionHandle

    queue: asyncio.Queue = asyncio.Queue()
    clock = _Clock()
    runtime = _Backend(queue, clock, delay=delay, stop=stop, backend=backend)

    async def spawn():
        return runtime

    for i in range(leases):
        await ro.RUNTIME_OWNERSHIP.acquire("key-a", f"dashboard:chat-{i}-1", spawn, cap=10)
    handle = AcpSessionHandle("sA", queue, runtime, session_key=session_key)
    # Through the facade: it mirrors the patch onto every session_handle owner.
    with patch.object(handle_facade, "time", clock):
        async for _ev in handle.prompt("hello", timeout=FALLBACK_TIMEOUT_SECS):
            pass

    tenants, bucket, outcome, label = want
    assert _named(rec, crs.TURN_STARTED) == [
        {"tenants": tenants, "delay": bucket, "harness": label}
    ]
    assert _named(rec, crs.TURN_ENDED) == [
        {"tenants": tenants, "outcome": outcome, "harness": label}
    ]


def test_a_kas_queued_notice_is_counted(rec):
    from kiro_crew.acp.session_handle import AcpSessionHandle

    runtime = _Runtime(77)
    _leases(runtime, 2, key="key-a")
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._runtime = runtime
    handle._session_key = "dashboard:chat-1-1"  # holds one of the two leases
    update = {
        "sessionUpdate": "session_info_update",
        "_meta": {"kiro": {"kind": "queued", "activeSessionId": "other-session"}},
    }
    assert handle._handle_kas_session_info(update) == []
    assert _named(rec, crs.KAS_QUEUED) == [{"tenants": "2"}]


# -- fallbacks: the engine download --------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "outcome"),
    [(None, "installed"), ("bundle", "failed"), ("host", "host_unsupported")],
)
def test_an_engine_install_is_counted_with_its_outcome(rec, raised, outcome):
    from kiro_crew.acp import kas_bundle, kas_engine_install

    def install(base_url, on_progress=None):
        if raised == "bundle":
            raise kas_bundle.KasBundleError("offline")
        if raised == "host":
            raise kas_bundle.KasHostUnsupported("no glibc")
        return SimpleNamespace(version="0.66.104", root="/x")

    with (
        patch.object(kas_bundle, "installed_bundle", return_value=None),
        patch.object(kas_bundle, "pinned_version", return_value=kas_bundle.KAS_PIN_VERSION),
        patch.object(kas_bundle, "install_pinned", side_effect=install),
        patch.object(kas_engine_install, "_inside_agent_sandbox", return_value=False),
    ):
        if raised is None:
            kas_engine_install.install_pinned_engine("https://x", caller="spawn")
        else:
            with pytest.raises(kas_bundle.KasBundleError):
                kas_engine_install.install_pinned_engine("https://x", caller="spawn")
    assert _named(rec, crs.KAS_ENGINE_INSTALL) == [{"outcome": outcome, "caller": "spawn"}]


# -- registration ---------------------------------------------------------------------


def test_one_failing_gauge_set_neither_raises_nor_costs_the_other(caplog):
    """Each shared-process gauge set registers in its own try."""
    from kiro_crew.metrics import mcp_gateway_gauges
    from kiro_crew.metrics.shared_process_gauges import register_shared_process_gauges

    meter = MagicMock()
    with (
        caplog.at_level("WARNING", logger="kiro_crew.metrics.provider"),
        patch.object(mcp_gateway_gauges, "register_mcp_gateway_gauges", side_effect=OSError),
        patch.object(crs, "register_chat_runtime_share_gauges") as share,
    ):
        register_shared_process_gauges(meter)
    share.assert_called_once_with(meter)
    assert "mcp gateway gauges unavailable" in caplog.text

    caplog.clear()
    with (
        caplog.at_level("WARNING", logger="kiro_crew.metrics.provider"),
        patch.object(mcp_gateway_gauges, "register_mcp_gateway_gauges") as pool,
        patch.object(crs, "register_chat_runtime_share_gauges", side_effect=OSError),
    ):
        register_shared_process_gauges(meter)
    pool.assert_called_once_with(meter)
    assert "chat runtime share gauges unavailable" in caplog.text
