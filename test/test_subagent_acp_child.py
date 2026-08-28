"""Dedicated ACP children stay on the live parent harness.

Shared-runtime multiplexing is kiro-only. Spec-adapter parents take the
dedicated path, which used the factory snapshot — a Settings switch then
made spawn_run from a goose chat start kiro children, and a kiro-namespace
pin (including ``"auto"``) was offered to an adapter that rejects it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from kiro_crew.acp.types import ACP_BACKEND_GOOSE, ACP_BACKEND_KIRO
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import build_provider_factory
from kiro_crew.session import SessionManager, _Session
from kiro_crew.subagent import dedicated_child_factory_kwargs, parent_live_harness


class TestDedicatedChildFactoryKwargs:
    def test_no_parent_keeps_the_factory_snapshot(self) -> None:
        assert (
            dedicated_child_factory_kwargs(
                parent_backend=None, advertised=["goose-model"], preferred_model="auto"
            )
            == {}
        )

    def test_kiro_parent_pins_the_empty_backend_id(self) -> None:
        kwargs = dedicated_child_factory_kwargs(
            parent_backend=ACP_BACKEND_KIRO,
            advertised=["auto", "claude-sonnet-4.6"],
            preferred_model="",
        )
        assert kwargs == {"acp_backend": ACP_BACKEND_KIRO}

    def test_auto_is_omitted_when_the_parent_does_not_serve_it(self) -> None:
        kwargs = dedicated_child_factory_kwargs(
            parent_backend=ACP_BACKEND_GOOSE,
            advertised=["goose-kimi"],
            preferred_model="auto",
        )
        assert kwargs == {"acp_backend": ACP_BACKEND_GOOSE}
        assert "model" not in kwargs

    def test_auto_is_kept_when_the_parent_advertises_it(self) -> None:
        kwargs = dedicated_child_factory_kwargs(
            parent_backend=ACP_BACKEND_KIRO,
            advertised=["auto", "claude-sonnet-4.6"],
            preferred_model="auto",
        )
        assert kwargs["model"] == "auto"

    def test_an_unusable_pin_is_dropped(self) -> None:
        kwargs = dedicated_child_factory_kwargs(
            parent_backend=ACP_BACKEND_GOOSE,
            advertised=["goose-kimi"],
            preferred_model="claude-sonnet-4.6",
        )
        assert kwargs == {"acp_backend": ACP_BACKEND_GOOSE}

    def test_a_served_pin_is_kept(self) -> None:
        kwargs = dedicated_child_factory_kwargs(
            parent_backend=ACP_BACKEND_GOOSE,
            advertised=["goose-kimi"],
            preferred_model="goose-kimi",
        )
        assert kwargs == {"acp_backend": ACP_BACKEND_GOOSE, "model": "goose-kimi"}

    def test_empty_advertised_trusts_a_concrete_pin(self) -> None:
        """Entitlement unknown: resolve_usable_model keeps a concrete id."""
        kwargs = dedicated_child_factory_kwargs(
            parent_backend=ACP_BACKEND_GOOSE,
            advertised=[],
            preferred_model="goose-kimi",
        )
        assert kwargs["model"] == "goose-kimi"

    def test_empty_advertised_drops_auto(self) -> None:
        kwargs = dedicated_child_factory_kwargs(
            parent_backend=ACP_BACKEND_GOOSE,
            advertised=[],
            preferred_model="auto",
        )
        assert "model" not in kwargs


class TestLiveHarness:
    def test_missing_session_is_absence(self) -> None:
        mgr = SessionManager(KiroCrewConfig())
        assert mgr.live_harness("dashboard:gone") == (None, [])

    def test_empty_key_is_absence(self) -> None:
        mgr = SessionManager(KiroCrewConfig())
        assert mgr.live_harness("") == (None, [])

    def test_reads_provider_backend_and_advertised_ids(self) -> None:
        mgr = SessionManager(KiroCrewConfig())
        provider = SimpleNamespace(
            backend=ACP_BACKEND_GOOSE,
            available_models=lambda: [{"modelId": "goose-kimi"}, {"modelId": "other"}],
        )
        mgr._sessions["dashboard:slot1"] = _Session(provider=provider)
        assert mgr.live_harness("dashboard:slot1") == (
            ACP_BACKEND_GOOSE,
            ["goose-kimi", "other"],
        )

    def test_kiro_empty_backend_is_not_absence(self) -> None:
        mgr = SessionManager(KiroCrewConfig())
        provider = SimpleNamespace(backend="", available_models=lambda: [])
        mgr._sessions["dashboard:slot1"] = _Session(provider=provider)
        backend, advertised = mgr.live_harness("dashboard:slot1")
        assert backend == ACP_BACKEND_KIRO
        assert advertised == []

    def test_magicmock_backend_is_absence(self) -> None:
        """A mock's ``.backend`` is another mock; inventing acp:<MagicMock> is worse."""
        mgr = SessionManager(KiroCrewConfig())
        mgr._sessions["dashboard:slot1"] = _Session(provider=MagicMock())
        assert mgr.live_harness("dashboard:slot1")[0] is None

    def test_factory_crossover_keeps_a_live_adapter_parent(self) -> None:
        """A Kiro default still constructs a child for its live adapter parent."""
        cfg = KiroCrewConfig()
        mgr = SessionManager(cfg, provider_factory=build_provider_factory(cfg))

        factory = mgr._provider_factory_for_backend(ACP_BACKEND_GOOSE)
        provider = factory(session_key="subagent:child")

        assert provider.backend == ACP_BACKEND_GOOSE


class TestParentLiveHarness:
    def test_magicmock_store_is_absence(self) -> None:
        assert parent_live_harness(MagicMock(), "dashboard:slot1") == (None, [])

    def test_tuple_from_a_real_store_is_kept(self) -> None:
        store = SimpleNamespace(live_harness=lambda _key: ("goose", ["goose-kimi"]))
        assert parent_live_harness(store, "dashboard:slot1") == ("goose", ["goose-kimi"])
