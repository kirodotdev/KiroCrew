"""The ``chat_runtime_sharing`` preview puts KAS on Crew's own engine by itself.

With the preview on, a KAS spawn takes the bundle route without
``agent.kas_bundle``, so the preview works from the UI alone. Off, routing is
exactly the relay rules. A stored Kiro CLI pick and the operator's
``KIROCREW_KIRO_BIN`` still never move. An engine that cannot be installed or
started falls back to the default route instead of refusing the chat.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import kas_route
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.session_handle import (
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeOverloaded,
)


@pytest.fixture
def preview(monkeypatch):
    import kiro_crew.acp.chat_runtime_sharing as crs

    state = {"on": True, "vault": True, "kas": True}
    monkeypatch.setattr(crs, "chat_runtime_sharing_enabled", lambda cfg: state["on"])
    monkeypatch.setattr(
        crs, "chat_runtime_sharing_harness_enabled", lambda cfg, backend: state["kas"]
    )
    monkeypatch.setattr(
        kas_route, "automatic_switch_allowed", lambda pin="": pin != kas_route.STORED_BACKEND_KIRO
    )

    def resolve(policy=None):
        source = kas_route.sign_in.Source.VAULT if state["vault"] else object()
        return SimpleNamespace(source=source)

    monkeypatch.setattr(kas_route.sign_in, "resolve", resolve)
    return state


def test_a_host_signed_in_only_through_kiro_cli_keeps_the_relay(preview, caplog):
    """The bundle starts on Crew's sign-in alone, so without one the preview never moves it."""
    preview["vault"] = False
    assert not kas_route.sharing_preview_selects_bundle(environ={})
    assert "no usable sign-in" in caplog.text


def test_a_failed_preview_bundle_start_stops_the_preview_choosing_it(preview):
    assert kas_route.sharing_preview_selects_bundle(environ={})
    kas_route.note_preview_bundle_failed("node exited 1")
    assert not kas_route.sharing_preview_selects_bundle(environ={})
    assert not kas_route.resolve_kas_route(environ={}).reason == kas_route.REASON_SHARING_PREVIEW


def test_the_preview_on_selects_the_bundle(preview):
    assert kas_route.sharing_preview_selects_bundle(environ={})
    route = kas_route.resolve_kas_route(environ={})
    assert route.bundle and route.reason == kas_route.REASON_SHARING_PREVIEW
    assert route.automatic
    assert "ACP runtime sharing preview" in route.describe()


def test_the_preview_off_leaves_the_relay_rules(preview):
    preview["on"] = False
    assert not kas_route.sharing_preview_selects_bundle(environ={})


def test_the_kas_switch_off_leaves_the_relay_rules(preview):
    """KAS's own switch off: no KAS chat can share, so the spawn keeps its route."""
    preview["kas"] = False
    assert not kas_route.sharing_preview_selects_bundle(environ={})
    assert kas_route.resolve_kas_route(environ={}).reason != kas_route.REASON_SHARING_PREVIEW
    assert not kas_harness._chat_sharing_preview_on()
    preview["kas"] = True
    assert kas_harness._chat_sharing_preview_on()


def test_a_kiro_cli_pin_stays_on_kiro_cli(preview):
    assert not kas_route.sharing_preview_selects_bundle(pin="kiro", environ={})


def test_the_operator_binary_is_never_second_guessed(preview):
    assert not kas_route.sharing_preview_selects_bundle(
        environ={"KIROCREW_KIRO_BIN": "/opt/kiro-cli"}
    )


def _ctx(tmp_path):
    return SpawnContext(agent="a", work_dir=str(tmp_path), model=None, environ={}, home=tmp_path)


class _RelayReached(Exception):
    """Raised where the default route starts, so a test can see it was taken."""


@pytest.mark.asyncio
async def test_the_spawn_takes_the_bundle_with_the_preview_on(preview, monkeypatch, tmp_path):
    from kiro_crew.acp import kas_bundle

    bundle = SimpleNamespace(
        version="0.66.104", node_version="22", node_bin=tmp_path / "node", acp_server=tmp_path / "s"
    )
    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: bundle)
    monkeypatch.setattr(kas_bundle, "_probe_node", lambda node_bin, version: None)
    harness = kas_harness.KasHarness()
    plan = await harness.resolve_spawn(_ctx(tmp_path))
    assert plan.spawn_label == kas_harness.KAS_BUNDLE_SPAWN_LABEL
    assert harness.bundle_engine_version == "0.66.104"
    assert harness.spawn_route_reason == kas_route.REASON_SHARING_PREVIEW


@pytest.mark.asyncio
async def test_a_node_that_cannot_run_here_falls_back_before_launch(preview, monkeypatch, tmp_path):
    """A restored bundle whose Node is for another architecture never reaches the launch."""
    from kiro_crew.acp import kas_bundle

    bundle = SimpleNamespace(
        version="0.66.104", node_version="22", node_bin=tmp_path / "n2", acp_server=tmp_path / "s"
    )

    def wrong_arch(node_bin, version):
        raise kas_bundle.KasBundleError("the extracted Node runtime exited 126")

    async def relay_bin(**_kw):
        raise _RelayReached

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: bundle)
    monkeypatch.setattr(kas_bundle, "_probe_node", wrong_arch)
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", relay_bin)
    harness = kas_harness.KasHarness()
    with pytest.raises(_RelayReached):
        await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.spawn_route_reason == ""
    assert not kas_route.sharing_preview_selects_bundle(environ={})


class _FailingStartRuntime:
    """The runtime fields ``_fell_back_from_preview_bundle`` reads, with a scripted start."""

    def __init__(self, reason, *, remote=False):
        self._harness = SimpleNamespace(spawn_route_reason=reason)
        self._remote_workspace = remote
        self.starts = 0
        self.revived = 0

    async def _revive_after_failed_start(self):
        self.revived += 1
        return True

    async def _spawn_admitted_rederiving_once(self):
        self.starts += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [AcpRuntimeDead("process exited (rc=1)"), AcpRequestTimeout("initialize timed out")],
)
async def test_a_preview_bundle_that_fails_after_launch_retries_on_the_default_route(
    preview, failure
):
    from kiro_crew.acp import runtime_spawn_entry

    rt = _FailingStartRuntime(kas_route.REASON_SHARING_PREVIEW)
    ok = await runtime_spawn_entry._fell_back_from_preview_bundle(rt, failure)
    assert ok and rt.revived == 1 and rt.starts == 1
    # The retried start's resolve_spawn takes the default route.
    assert not kas_route.sharing_preview_selects_bundle(environ={})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        AcpRuntimeError("the derived spec changed during the start"),
        AcpRuntimeOverloaded("initialize went unanswered while the agents slice is throttled"),
    ],
)
async def test_a_failure_the_engine_did_not_cause_is_not_retried_or_recorded(preview, failure):
    """A stale spec or a throttled slice would fail the default route the same way."""
    from kiro_crew.acp import runtime_spawn_entry

    rt = _FailingStartRuntime(kas_route.REASON_SHARING_PREVIEW)
    ok = await runtime_spawn_entry._fell_back_from_preview_bundle(rt, failure)
    assert not ok and rt.starts == 0 and rt.revived == 0
    assert kas_route.sharing_preview_selects_bundle(environ={})


def test_a_failed_engine_start_keeps_the_default_route_only_for_the_cooldown(preview, monkeypatch):
    from kiro_crew.testing.clock import ManualClock

    clock = ManualClock()
    clock.install(monkeypatch, kas_route)
    kas_route.note_preview_bundle_failed("node exited 1")
    assert not kas_route.sharing_preview_selects_bundle(environ={})
    clock.advance(kas_route.PREVIEW_BUNDLE_COOLDOWN_SECS - 1)
    assert not kas_route.sharing_preview_selects_bundle(environ={})
    clock.advance(2)
    assert kas_route.sharing_preview_selects_bundle(environ={})


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [kas_route.REASON_CONFIGURED, ""])
async def test_a_configured_bundle_or_relay_start_failure_is_not_retried(preview, reason):
    from kiro_crew.acp import runtime_spawn_entry

    rt = _FailingStartRuntime(reason)
    ok = await runtime_spawn_entry._fell_back_from_preview_bundle(rt, AcpRuntimeError("boom"))
    assert not ok and rt.starts == 0
    assert kas_route.sharing_preview_selects_bundle(environ={})


@pytest.mark.asyncio
async def test_an_engine_that_cannot_start_falls_back_to_the_default_route(
    preview, monkeypatch, tmp_path, caplog
):
    async def no_engine(self, ctx, route, **_kw):
        raise AcpRuntimeError("KAS engine download failed")

    async def relay_bin(**_kw):
        raise _RelayReached

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(kas_harness.KasHarness, "_bundle_spawn_plan", no_engine)
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", relay_bin)
    with pytest.raises(_RelayReached):
        await kas_harness.KasHarness().resolve_spawn(_ctx(tmp_path))
    assert "could not start (KAS engine download failed)" in caplog.text


@pytest.mark.asyncio
async def test_a_configured_bundle_still_refuses_when_it_cannot_start(
    preview, monkeypatch, tmp_path
):
    async def no_engine(self, ctx, route, **_kw):
        raise AcpRuntimeError("KAS engine download failed")

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: True)
    monkeypatch.setattr(kas_harness.KasHarness, "_bundle_spawn_plan", no_engine)
    with pytest.raises(AcpRuntimeError):
        await kas_harness.KasHarness().resolve_spawn(_ctx(tmp_path))


@pytest.mark.asyncio
async def test_the_kas_start_path_takes_the_preview_fallback(preview):
    """Wired at the launch boundary: the first start fails, the retried one succeeds."""
    from kiro_crew.acp import runtime_spawn_entry

    class Rt(_FailingStartRuntime):
        _kas_extract_broken_message = ""

        def _broken_kas_server_path(self):
            return None

        async def _spawn_admitted_rederiving_once(self):
            self.starts += 1
            if self.starts == 1:
                raise AcpRuntimeDead("node exited during initialize")

    rt = Rt(kas_route.REASON_SHARING_PREVIEW)
    await runtime_spawn_entry._spawn_admitted_repairing_kas_extract(rt)
    assert rt.starts == 2 and rt.revived == 1


@pytest.mark.asyncio
async def test_a_non_engine_failure_on_the_start_path_starts_no_second_process(preview):
    from kiro_crew.acp import runtime_spawn_entry

    class Rt(_FailingStartRuntime):
        _kas_extract_broken_message = ""

        def _broken_kas_server_path(self):
            return None

        async def _spawn_admitted_rederiving_once(self):
            self.starts += 1
            raise AcpRuntimeError("the derived spec changed during the start")

    rt = Rt(kas_route.REASON_SHARING_PREVIEW)
    with pytest.raises(AcpRuntimeError, match="derived spec"):
        await runtime_spawn_entry._spawn_admitted_repairing_kas_extract(rt)
    assert rt.starts == 1 and rt.revived == 0


@pytest.mark.asyncio
async def test_a_fallback_to_a_queueing_relay_is_marked_unshareable(preview, monkeypatch, tmp_path):
    """The bundle failed before launch; the relay it fell back to runs 0.66.102."""
    import kiro_crew.acp.chat_runtime_sharing as crs

    async def no_engine(self, ctx, route, **_kw):
        raise AcpRuntimeError("KAS engine download failed")

    async def relay_bin(**_kw):
        return str(tmp_path / "kiro-cli")

    async def no_vault():
        return False

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(kas_harness.KasHarness, "_bundle_spawn_plan", no_engine)
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", relay_bin)
    monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: (3, 2, 1))
    monkeypatch.setattr(kas_harness, "vault_holds_identity_off_loop", no_vault)
    monkeypatch.setattr(crs, "relay_engine_shares_chats", lambda: False)
    monkeypatch.setattr(kas_route, "relay_unusable_reason", lambda **_kw: "")
    harness = kas_harness.KasHarness()
    harness.spawn_engine_shares_chats = True
    plan = await harness.resolve_spawn(_ctx(tmp_path))
    assert plan.argv
    assert harness.spawn_engine_shares_chats is False


@pytest.mark.asyncio
async def test_the_bundle_at_the_floor_is_marked_shareable(preview, monkeypatch, tmp_path):
    from kiro_crew.acp import kas_bundle

    bundle = SimpleNamespace(
        version="0.66.104", node_version="22", node_bin=tmp_path / "n3", acp_server=tmp_path / "s"
    )
    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: bundle)
    monkeypatch.setattr(kas_bundle, "_probe_node", lambda node_bin, version: None)
    harness = kas_harness.KasHarness()
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.spawn_engine_shares_chats is True


class _Rt:
    def __init__(self, pid, joinable=True):
        self.pid = pid
        self.chat_joinable = joinable

    def is_alive(self):
        return True


def test_an_unshareable_runtime_is_published_closed_to_joins():
    """Its founder keeps it; a waiter parked on the launch and a later arrival found their own."""
    import asyncio

    from kiro_crew.runtime_ownership import RuntimeOwnership

    table = RuntimeOwnership()
    made = []

    async def scenario():
        gate = asyncio.Event()

        async def spawn_first():
            await gate.wait()
            made.append(_Rt(100, joinable=False))
            return made[-1]

        async def spawn_other():
            made.append(_Rt(100 + len(made) + 1))
            return made[-1]

        founder = asyncio.ensure_future(table.acquire("k", "s1", spawn_first, cap=10))
        await asyncio.sleep(0)
        waiter = asyncio.ensure_future(table.acquire("k", "s2", spawn_other, cap=10))
        await asyncio.sleep(0)
        gate.set()
        a, b = await founder, await waiter
        c = await table.acquire("k", "s3", spawn_other, cap=10)
        return a, b, c

    a, b, c = asyncio.run(scenario())
    assert a.runtime.pid == 100 and not a.joined
    assert b.runtime.pid != 100 and not b.joined
    assert c.runtime.pid != 100


@pytest.mark.asyncio
@pytest.mark.parametrize("shares", [True, False])
async def test_a_kas_start_carries_its_engine_verdict_onto_the_runtime(shares):
    """``spawn`` copies the harness's verdict for the engine it ended on."""
    from kiro_crew.acp import runtime_spawn_entry
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    class Rt:
        _process = None
        acp_backend = _acp_backend = ACP_BACKEND_KAS
        chat_joinable = not shares

        def __init__(self):
            self._harness = SimpleNamespace(spawn_engine_shares_chats=shares)

        async def _spawn_admitted_repairing_kas_extract(self):
            pass

    rt = Rt()
    await runtime_spawn_entry.spawn(rt)
    assert rt.chat_joinable is shares


@pytest.mark.asyncio
async def test_with_the_preview_off_a_relay_spawn_asks_no_sharing_question(
    preview, monkeypatch, tmp_path
):
    """Preview off: no extract scan, no verdict, so the runtime keeps main's state."""
    import kiro_crew.acp.chat_runtime_sharing as crs

    preview["on"] = False
    asked = []

    async def relay_bin(**_kw):
        return str(tmp_path / "kiro-cli")

    async def no_vault():
        return False

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", relay_bin)
    monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: (3, 2, 1))
    monkeypatch.setattr(kas_harness, "vault_holds_identity_off_loop", no_vault)
    monkeypatch.setattr(kas_route, "relay_unusable_reason", lambda **_kw: "")
    monkeypatch.setattr(crs, "relay_engine_shares_chats", lambda: asked.append(1) or False)
    harness = kas_harness.KasHarness()
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.spawn_engine_shares_chats is None
    assert asked == []


@pytest.mark.asyncio
async def test_a_spawn_with_no_sharing_verdict_is_closed_to_joins():
    """The preview turned on while this start ran: no verdict, so no joiner lands on it."""
    from kiro_crew.acp import runtime_spawn_entry
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    class Rt:
        _process = None
        acp_backend = _acp_backend = ACP_BACKEND_KAS
        chat_joinable = True
        _harness = SimpleNamespace(spawn_engine_shares_chats=None)

        async def _spawn_admitted_repairing_kas_extract(self):
            pass

    rt = Rt()
    await runtime_spawn_entry.spawn(rt)
    assert rt.chat_joinable is False


def test_a_shared_placement_on_a_no_verdict_runtime_founds_its_own():
    """End to end at the table: cap > 1 and an unknown engine never take a joiner."""
    import asyncio

    from kiro_crew.runtime_ownership import RuntimeOwnership

    table = RuntimeOwnership()
    first, second = _Rt(300, joinable=False), _Rt(301)
    spawns = iter([first, second])

    async def spawn():
        return next(spawns)

    async def scenario():
        a = await table.acquire("k", "s1", spawn, cap=10)
        b = await table.acquire("k", "s2", spawn, cap=10)
        return a, b

    a, b = asyncio.run(scenario())
    assert a.runtime is first and b.runtime is second and not b.joined


def test_the_unshared_registration_logs_nothing(caplog):
    """cap 1 is every chat with the preview off: no sharing log line."""
    import asyncio

    from kiro_crew.runtime_ownership import RuntimeOwnership

    table = RuntimeOwnership()
    rt = _Rt(200, joinable=False)

    async def spawn():
        return rt

    with caplog.at_level(logging.INFO):
        acq = asyncio.run(table.acquire("k", "s1", spawn, cap=1))
    assert acq.runtime is rt
    assert "chat-runtime-sharing" not in caplog.text


def test_an_unshareable_runtime_registered_at_cap_one_takes_no_joiner_after_a_cap_raise():
    """The cap is read live and is not in the key: a cap-1 entry must not open later."""
    import asyncio

    from kiro_crew.runtime_ownership import RuntimeOwnership

    table = RuntimeOwnership()
    first, second = _Rt(400, joinable=False), _Rt(401)
    spawns = iter([first, second])

    async def spawn():
        return next(spawns)

    async def scenario():
        a = await table.acquire("k", "s1", spawn, cap=1)
        b = await table.acquire("k", "s2", spawn, cap=10)
        return a, b

    a, b = asyncio.run(scenario())
    assert a.runtime is first
    assert b.runtime is second and not b.joined
