"""The ``harness`` label: closed value set, resource attribute, per-session points.

Pins four things:

* the value set is ``ACP_BACKENDS_KNOWN`` in config spelling plus
  ``claude_code`` / ``unknown`` / ``other``, derived rather than listed, and an
  unknown backend id becomes ``other``;
* every exported point carries the ``kirocrew.harness`` resource attribute with
  the CONFIGURED default, and a rebuild after a config change relabels it;
* ``kirocrew.session.started`` and ``kirocrew.turn.duration`` carry the
  session's EFFECTIVE harness, so a per-session override shows up even when the
  install default differs;
* the providers this reads are the shapes the session map's label already
  resolves, so the two cannot disagree.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
    TurnUsage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    backend_to_stored,
)
from kiro_crew.config.loader import AgentConfig, KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics import harness as h
from kiro_crew.metrics import provider as metrics_provider
from kiro_crew.metrics import sessions as sess
from kiro_crew.metrics.provider import get_recorder, reset_for_testing
from kiro_crew.metrics.provider import shutdown as provider_shutdown
from kiro_crew.metrics.turns import TURN_METRIC
from kiro_crew.providers.acp import AcpProvider, provider_backend
from kiro_crew.session_background import _ProviderBgSession

# -- helpers -----------------------------------------------------------------


def _acp_provider(backend: str) -> AcpProvider:
    """An unstarted ``AcpProvider`` on *backend*, constructed without spawning."""
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


def _started_provider(backend: str) -> AcpProvider:
    """The shape after startup: the client is swapped for an ``AcpSessionProvider``."""
    provider = _acp_provider(backend)
    runtime = MagicMock(spec=AcpRuntime)
    runtime.acp_backend = backend
    provider._client = AcpSessionProvider(MagicMock(), runtime)  # type: ignore[assignment]
    return provider


class _CapturingRecorder:
    enabled = True

    def __init__(self) -> None:
        self.hist: list = []
        self.counters: list = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.hist.append({"name": name, "attrs": dict(attrs or {})})

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "attrs": dict(attrs or {})})


@pytest.fixture(autouse=True)
def _clean_seam():
    h.reset_for_testing()
    yield
    h.reset_for_testing()


def _patch_config(monkeypatch, *, acp_backend: str, provider: str = "acp", **tel):
    fake = KiroCrewConfig(telemetry=TelemetryConfig(**tel))
    fake.agent = AgentConfig(acp_backend=acp_backend, provider=provider)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake))
    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)
    return fake


def _last_resource(metrics_dir) -> dict:
    shards = sorted(metrics_dir.glob("metrics-*.jsonl"))
    assert shards, "no shard written by the final flush"
    last = json.loads(shards[-1].read_text().splitlines()[-1])
    return last["resource_metrics"][0]["resource"]["attributes"]


# -- the closed value set ----------------------------------------------------


def test_the_value_set_is_the_known_backends_plus_three():
    expected = {backend_to_stored(b) for b in ACP_BACKENDS_KNOWN} | {
        "claude_code",
        "unknown",
        "other",
    }
    assert h.HARNESS_VALUES == expected
    # kiro-cli is the empty runtime id; the label must use the config word.
    assert "" not in h.HARNESS_VALUES
    assert "kiro" in h.HARNESS_VALUES


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_every_known_backend_maps_to_its_config_spelling(backend):
    assert h.harness_for_backend(backend) == backend_to_stored(backend)


@pytest.mark.parametrize("value", ["gemini", "KAS", " kas", "kiro", 3, ["kas"]])
def test_an_unknown_backend_becomes_other(value):
    # "kiro" is the CONFIG spelling, not a runtime id, so as a runtime id it is
    # unknown to the gate and must not pass through.
    assert h.harness_for_backend(value) == h.HARNESS_OTHER


def test_no_backend_is_unknown_and_the_seam_wins():
    assert h.harness_for_backend(None) == h.HARNESS_UNKNOWN
    assert h.harness_for_backend(ACP_BACKEND_KAS, claude_code_seam=True) == "claude_code"


def test_configured_harness_reads_the_agent_section():
    assert h.configured_harness(AgentConfig(acp_backend=ACP_BACKEND_KAS)) == "kas"
    assert h.configured_harness(AgentConfig(acp_backend=ACP_BACKEND_KIRO)) == "kiro"
    seam = AgentConfig(acp_backend=ACP_BACKEND_KAS)
    seam.provider = "claude_code"
    assert h.configured_harness(seam) == "claude_code"
    assert h.configured_harness(None) == h.HARNESS_UNKNOWN


def test_the_resource_key_has_one_spelling():
    assert metrics_provider.RESOURCE_ATTR_HARNESS == h.RESOURCE_ATTR_HARNESS == "kirocrew.harness"


# -- per-session resolution --------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        (ACP_BACKEND_KAS, "kas"),
        (ACP_BACKEND_KIRO, "kiro"),
        (ACP_BACKEND_CLAUDE, "claude"),
        (ACP_BACKEND_CODEX, "codex"),
    ],
)
def test_each_surface_resolves_to_its_harness(backend, expected):
    assert h.harness_of(_acp_provider(backend)) == expected
    assert h.harness_of(_started_provider(backend)) == expected
    # The bare session provider a shared subagent session hands out.
    runtime = MagicMock(spec=AcpRuntime)
    runtime.acp_backend = backend
    assert h.harness_of(AcpSessionProvider(MagicMock(), runtime)) == expected


def test_the_claude_code_seam_wins_over_the_session_backend():
    h.set_configured(AgentConfig(acp_backend=ACP_BACKEND_KAS, provider="claude_code"))
    assert h.harness_of(_acp_provider(ACP_BACKEND_CLAUDE)) == "claude_code"


def test_a_foreign_shape_is_unknown_never_kiro():
    # kiro-cli's own id is "", so a blind getattr default would read every miss
    # as kiro. A shape that is not a provider must be ``unknown``.
    assert provider_backend(object()) is None
    assert provider_backend(MagicMock()) is None
    assert h.harness_of(object()) == h.HARNESS_UNKNOWN
    assert h.harness_of(MagicMock()) == h.HARNESS_UNKNOWN
    assert h.harness_of(None) == h.HARNESS_UNKNOWN


def _bare_handle(backend: str) -> AcpSessionHandle:
    """A runtime-owned session handle, the shape ``get_bg_session`` returns."""
    handle = object.__new__(AcpSessionHandle)
    runtime = MagicMock(spec=AcpRuntime)
    runtime.acp_backend = backend
    handle._runtime = runtime
    return handle


def _provider_bg_session(backend: str) -> _ProviderBgSession:
    """The provider-backed background handle ``get_bg_session`` can return."""
    entry = MagicMock()
    entry.provider = _started_provider(backend)
    return _ProviderBgSession(entry)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO, ACP_BACKEND_CODEX])
def test_the_background_handle_shapes_resolve_too(backend):
    """Background one-liners pass a handle, not a provider, as ``model_source``."""
    expected = backend_to_stored(backend)
    assert h.harness_of(_bare_handle(backend)) == expected
    assert h.harness_of(_provider_bg_session(backend)) == expected
    runtime = MagicMock(spec=AcpRuntime)
    runtime.acp_backend = backend
    assert h.harness_of(runtime) == expected


def test_the_session_map_label_ignores_the_handle_shapes():
    """``provider_label`` keys the session map; the handle branches must not reach it."""
    from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
    from kiro_crew.providers.acp import provider_label

    assert provider_label(_bare_handle(ACP_BACKEND_KAS)) == PROVIDER_LABEL_DEFAULT
    assert provider_label(_provider_bg_session(ACP_BACKEND_KAS)) == PROVIDER_LABEL_DEFAULT


def test_a_provider_with_no_client_is_unknown_not_kiro():
    provider = _acp_provider(ACP_BACKEND_KAS)
    provider._client = None  # type: ignore[assignment]
    assert provider_backend(provider) is None
    assert h.harness_of(provider) == h.HARNESS_UNKNOWN


def test_turn_duration_from_a_background_one_liner_names_its_harness(monkeypatch):
    """The path the review caught: ``model_source`` is the background handle."""
    from kiro_crew.dashboard.handlers import usage

    rec = _CapturingRecorder()
    monkeypatch.setattr(usage, "_write_token_record", lambda record, now: None)
    event = MagicMock()
    event.duration_ms = 800
    event.stop_reason = None
    with patch("kiro_crew.metrics.turns.get_recorder", return_value=rec):
        for source in (_bare_handle(ACP_BACKEND_KAS), _provider_bg_session(ACP_BACKEND_KAS)):
            asyncio.run(
                usage.persist_token_record_async(
                    "_bg", "", event, "acp", elapsed_ms=800, model_source=source
                )
            )
    labels = [p["attrs"]["harness"] for p in rec.hist if p["name"] == TURN_METRIC]
    assert labels == ["kas", "kas"]


def test_an_unregistered_backend_on_a_live_provider_is_other():
    provider = _acp_provider(ACP_BACKEND_KAS)
    provider._client.backend = "some-edition-harness"
    assert h.harness_of(provider) == h.HARNESS_OTHER


# -- point attributes follow a per-session override --------------------------


def test_session_started_carries_the_effective_harness(tmp_path, monkeypatch):
    """The install default is KAS; a member session overridden to Codex counts as codex."""
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    rec = _CapturingRecorder()
    h.set_configured(AgentConfig(acp_backend=ACP_BACKEND_KAS))
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        asyncio.run(
            sess.record_session_started("dashboard:chat-1", _acp_provider(ACP_BACKEND_CODEX))
        )
        asyncio.run(
            sess.record_session_started("dashboard:chat-2", _started_provider(ACP_BACKEND_KAS))
        )
        asyncio.run(sess.record_session_started("dashboard:chat-3"))
    with sess._live_lock:
        sess._live_starts.clear()
    started = [c for c in rec.counters if c["name"] == sess.SESSION_STARTED_COUNTER]
    assert [c["attrs"]["harness"] for c in started] == ["codex", "kas", "unknown"]


def test_turn_duration_from_the_shared_boundary_carries_the_effective_harness(monkeypatch):
    from kiro_crew.dashboard.handlers import usage

    rec = _CapturingRecorder()
    monkeypatch.setattr(usage, "_write_token_record", lambda record, now: None)
    event = MagicMock()
    event.duration_ms = 1200
    event.stop_reason = None
    with patch("kiro_crew.metrics.turns.get_recorder", return_value=rec):
        asyncio.run(
            usage.persist_token_record_async(
                "cron:job-1",
                "",
                event,
                "acp",
                elapsed_ms=1200,
                model_source=_started_provider(ACP_BACKEND_CODEX),
            )
        )
    turns = [p for p in rec.hist if p["name"] == TURN_METRIC]
    assert len(turns) == 1
    assert turns[0]["attrs"]["harness"] == "codex"
    # The usage family does not carry it in this change.
    assert all("harness" not in p["attrs"] for p in rec.hist if p["name"] != TURN_METRIC)
    assert all("harness" not in c["attrs"] for c in rec.counters)


def test_turn_duration_from_the_dashboard_owner_carries_the_effective_harness():
    from kiro_crew.dashboard import chat_runner

    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.turns.get_recorder", return_value=rec):
        chat_runner._emit_turn_metric(
            900,
            None,
            "dashboard:chat-1",
            harness=h.harness_of(_started_provider(ACP_BACKEND_CLAUDE)),
        )
        chat_runner._emit_turn_metric(900, None, "dashboard:chat-2")
        chat_runner._emit_turn_metric(900, None, "dashboard:chat-3", harness="not-a-harness")
    labels = [p["attrs"]["harness"] for p in rec.hist if p["name"] == TURN_METRIC]
    assert labels == ["claude", "unknown", "other"]


class _CodexBackedProvider(ScriptedProvider):
    """A scripted turn client fronting a live provider on the Codex harness.

    The harness reads it the way it reads the background handle: the scripted
    client is not an ACP shape itself, so the label comes from the provider it
    names.
    """

    @property
    def provider(self) -> AcpSessionProvider:
        runtime = MagicMock(spec=AcpRuntime)
        runtime.acp_backend = ACP_BACKEND_CODEX
        return AcpSessionProvider(MagicMock(), runtime)


async def _dashboard_turn_harness(provider: type[ScriptedProvider] | None) -> list[str]:
    """Run one real dashboard turn; return the ``harness`` on its duration samples."""
    rec = _CapturingRecorder()
    landed = AcpEvent(
        kind=EVENT_COMPLETE,
        stop_reason=STOP_REASON_END_TURN,
        usage=TurnUsage(input_tokens=10, output_tokens=5, duration_ms=1500),
    )
    with patch("kiro_crew.metrics.turns.get_recorder", return_value=rec):
        await run_turn(
            TurnScript(
                events=[AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"), landed],
                provider=provider,
            )
        )
    return [p["attrs"]["harness"] for p in rec.hist if p["name"] == TURN_METRIC]


@pytest.mark.asyncio
async def test_a_dashboard_turn_labels_its_duration_with_the_turn_clients_harness():
    """The dashboard owner reads the harness off the client that ran the turn."""
    assert await _dashboard_turn_harness(_CodexBackedProvider) == ["codex"]
    # A client that names no backend is ``unknown``, not the install default.
    assert await _dashboard_turn_harness(None) == ["unknown"]


# -- the resource attribute --------------------------------------------------


def test_every_exported_point_carries_the_configured_harness(tmp_path, monkeypatch):
    reset_for_testing()
    metrics_dir = tmp_path / "m"
    _patch_config(
        monkeypatch,
        acp_backend=ACP_BACKEND_KAS,
        enabled=True,
        local_dir=str(metrics_dir),
        export_interval_seconds=3600,
    )
    try:
        rec = get_recorder()
        assert rec.enabled is True
        # A process-level instrument with no session: it still carries the label.
        rec.counter("kirocrew.session.idle_expired", attrs={"turn_active": False})
        provider_shutdown()
        assert _last_resource(metrics_dir)["kirocrew.harness"] == "kas"
    finally:
        reset_for_testing()


def test_a_config_change_relabels_the_rebuilt_recorder(tmp_path, monkeypatch):
    reset_for_testing()
    metrics_dir = tmp_path / "m"
    tel = {"enabled": True, "local_dir": str(metrics_dir), "export_interval_seconds": 3600}
    _patch_config(monkeypatch, acp_backend=ACP_BACKEND_KAS, **tel)
    try:
        get_recorder().counter("kirocrew.session.idle_expired", attrs={"turn_active": False})
        assert _last_resource_after_flush(metrics_dir) == "kas"

        # The operator switches the default to Kiro CLI. The config watcher's
        # applier is what the gateway runs on that write.
        _patch_config(monkeypatch, acp_backend=ACP_BACKEND_KIRO, **tel)
        asyncio.run(metrics_provider._on_config_change(object()))
        # A shutdown hands the next caller a no-op and rebuilds on a worker in
        # production; a test resets that so the rebuild is synchronous.
        reset_for_testing()
        get_recorder().counter("kirocrew.session.idle_expired", attrs={"turn_active": False})
        assert _last_resource_after_flush(metrics_dir) == "kiro"

        _patch_config(monkeypatch, acp_backend="unregistered", **tel)
        reset_for_testing()
        get_recorder().counter("kirocrew.session.idle_expired", attrs={"turn_active": False})
        assert _last_resource_after_flush(metrics_dir) == "other"
    finally:
        reset_for_testing()


def _last_resource_after_flush(metrics_dir) -> str:
    provider_shutdown()
    return str(_last_resource(metrics_dir)["kirocrew.harness"])


def test_the_watcher_rebuilds_on_the_harness_settings(monkeypatch):
    """``agent.acp_backend`` and ``agent.provider`` move the resource, so they rebuild it."""
    from kiro_crew.config import live

    seen: list = []

    def _subscribe(*prefixes, callback, name=None):
        seen.extend(prefixes)
        return object()

    monkeypatch.setattr(live, "subscribe", _subscribe)
    monkeypatch.setattr(metrics_provider, "_config_sub", None)
    metrics_provider.watch_config()
    assert {"telemetry", "agent.acp_backend", "agent.provider"} <= set(seen)


def test_a_probe_without_config_omits_the_attribute():
    assert "kirocrew.harness" not in metrics_provider._resource_attributes()
    assert metrics_provider._resource_attributes("codex")["kirocrew.harness"] == "codex"
