"""Tests for Feishu gateway boot (maybe_start_feishu) and approval resolution."""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.util
import logging
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.feishu.gateway import _resolve_approval_mode, maybe_start_feishu
from kiro_crew.feishu.transport import SPOOL_DM_ROUTE_PREFIX, FeishuTransport
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.testing.wait import async_wait_until, default_timeout


class _FakeState:
    def __init__(self) -> None:
        self.registered: list = []

    def register_channel_transport(self, transport) -> None:
        self.registered.append(transport)


def _orch(**overrides):
    base = dict(
        _feishu_enabled=True,
        _feishu_app_id="app-id-1",
        _feishu_app_secret="secret-1",
        _feishu_allowed_open_ids=["ou_abc123"],
        _feishu_allow_group=False,
        _feishu_allowed_group_ids=[],
        sessions=object(),
        ctx_builder=object(),
        conv_log=None,
        _approval_mode=None,
        _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        dashboard_state=_FakeState(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestResolveApprovalMode:
    def test_yolo_resolves_auto(self) -> None:
        orch = _orch(_approval_mode="yolo")
        assert _resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_explicit_auto_resolves_auto(self) -> None:
        orch = _orch(_approval_mode=APPROVAL_AUTO)
        assert _resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_interactive_resolves_interactive(self) -> None:
        orch = _orch(_approval_mode="interactive")
        assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE

    def test_none_falls_back_to_cfg(self) -> None:
        orch = _orch(
            _approval_mode=None,
            _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        )
        assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE

    def test_none_falls_back_to_cfg_auto(self) -> None:
        orch = _orch(
            _approval_mode=None,
            _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode=APPROVAL_AUTO)),
        )
        assert _resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_unknown_mode_resolves_interactive(self) -> None:
        orch = _orch(_approval_mode="something_else")
        assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE


@patch("kiro_crew.feishu.gateway._LARK_MODULES", ())
@patch("kiro_crew.feishu.gateway.LarkClient")
@patch("kiro_crew.feishu.gateway.FeishuTransport")
@patch("kiro_crew.feishu.gateway.FeishuDispatcher")
class TestMaybeStartFeishu:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self, mock_dispatcher, mock_transport, mock_client) -> None:
        orch = _orch(_feishu_enabled=False)
        result = await maybe_start_feishu(orch)
        assert result is None
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path(self, mock_dispatcher, mock_transport_cls, mock_client_cls) -> None:
        fake_client = MagicMock()
        mock_client_cls.return_value = fake_client

        fake_transport = MagicMock()
        fake_transport.connect = AsyncMock()
        mock_transport_cls.return_value = fake_transport

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch()
        result = await maybe_start_feishu(orch)

        assert result is fake_client
        mock_client_cls.assert_called_once_with(app_id="app-id-1", app_secret="secret-1")
        fake_client.set_message_handler.assert_called_once_with(fake_transport.receive)
        assert fake_disp.client is fake_client
        fake_transport.connect.assert_awaited_once()
        assert orch.dashboard_state.registered == [fake_transport]

    @pytest.mark.asyncio
    async def test_dashboard_state_none_no_crash(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls
    ) -> None:
        fake_client = MagicMock()
        mock_client_cls.return_value = fake_client

        fake_transport = MagicMock()
        fake_transport.connect = AsyncMock()
        mock_transport_cls.return_value = fake_transport

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch(dashboard_state=None)
        result = await maybe_start_feishu(orch)

        assert result is fake_client
        # No crash — register_channel_transport simply not called.

    @pytest.mark.asyncio
    async def test_empty_allow_list_warns_but_starts(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls, caplog
    ) -> None:
        fake_client = MagicMock()
        mock_client_cls.return_value = fake_client

        fake_transport = MagicMock()
        fake_transport.connect = AsyncMock()
        mock_transport_cls.return_value = fake_transport

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch(_feishu_allowed_open_ids=[])
        with caplog.at_level("WARNING"):
            result = await maybe_start_feishu(orch)

        assert result is fake_client
        assert any("allowed_open_ids is empty" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_import_error_swallowed(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls, caplog
    ) -> None:
        mock_client_cls.side_effect = ImportError("No module named 'lark_oapi'")

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch()
        with caplog.at_level("ERROR"):
            result = await maybe_start_feishu(orch)

        assert result is None
        # Its OWN message, not the generic one: a missing optional extra is the
        # single failure here a user can fix without reading a stack trace, so
        # both the log and the settings badge name the install command.
        assert any("needs the lark-oapi extra" in r.getMessage() for r in caplog.records)
        assert orch.dashboard_state.feishu_connected is False
        # The badge names the dependency and the restart that loads it, and no
        # install command: Settings' card owns that, naming this gateway's
        # interpreter, and the badge outlives the install until the restart.
        badge = orch.dashboard_state.feishu_connect_error
        assert (
            badge
            == "The gateway started without lark-oapi. Restart it once lark-oapi is installed."
        )
        assert "pip" not in badge
        # The log line keeps the short pip hint, naming the DEPENDENCY rather
        # than `kirocrew[feishu]`: this project is not on an index.
        log = next(
            r.getMessage() for r in caplog.records if "needs the lark-oapi extra" in r.getMessage()
        )
        assert "pip install" in log and "kirocrew[" not in log

    @pytest.mark.asyncio
    async def test_generic_exception_swallowed(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls, caplog
    ) -> None:
        mock_client_cls.side_effect = RuntimeError("boom")

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch()
        with caplog.at_level("ERROR"):
            result = await maybe_start_feishu(orch)

        assert result is None
        assert any("Failed to start Feishu" in r.message for r in caplog.records)
        # A non-ImportError failure must still publish a reason; a bare
        # "not connected" badge sends the user to the logs for no reason.
        assert orch.dashboard_state.feishu_connected is False
        assert "RuntimeError: boom" in orch.dashboard_state.feishu_connect_error

    @pytest.mark.asyncio
    async def test_status_observer_is_wired_before_connect(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls
    ) -> None:
        """The badge follows the client's own transitions, and the observer is
        attached BEFORE connect() — a transition landing on a missing callback
        would be swallowed by the client's dedupe and never re-reported."""
        fake_client = MagicMock()
        fake_client.on_state_change = None
        mock_client_cls.return_value = fake_client
        fake_transport = MagicMock()
        observed: list[bool] = []

        async def _connect() -> None:
            # Whatever the client reports at connect time must already have
            # somewhere to land.
            observed.append(fake_client.on_state_change is not None)

        fake_transport.connect = _connect
        mock_transport_cls.return_value = fake_transport
        mock_dispatcher.return_value = MagicMock()

        orch = _orch()
        result = await maybe_start_feishu(orch)

        assert result is fake_client
        assert observed == [True]

        fake_client.on_state_change(True, "")
        assert orch.dashboard_state.feishu_connected is True
        assert orch.dashboard_state.feishu_connect_error == ""

        fake_client.on_state_change(False, "receiver stopped: RuntimeError")
        assert orch.dashboard_state.feishu_connected is False
        assert orch.dashboard_state.feishu_connect_error == "receiver stopped: RuntimeError"

        # Reasons are clamped so a pathological error string cannot bloat the
        # settings payload.
        fake_client.on_state_change(False, "x" * 500)
        assert len(orch.dashboard_state.feishu_connect_error) == 120


class _SlowLarkFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves stub ``lark_oapi`` packages whose import waits for the event loop.

    The import blocks until a task on the loop releases it. Run on the loop
    thread, the import starves that task and the wait runs out, which only
    names a hang: the bound is the runner's, not a guess at its speed.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.released = threading.Event()
        self.import_threads: list[threading.Thread] = []
        self.timed_out = False

    def find_spec(self, name, path=None, target=None):
        if name == "lark_oapi" or name.startswith("lark_oapi."):
            return importlib.util.spec_from_loader(name, self, is_package=True)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        self.import_threads.append(threading.current_thread())
        self.started.set()
        if not self.released.wait(default_timeout()):
            self.timed_out = True


@pytest.fixture
def slow_lark(monkeypatch):
    for name in [m for m in sys.modules if m == "lark_oapi" or m.startswith("lark_oapi.")]:
        monkeypatch.delitem(sys.modules, name)
    finder = _SlowLarkFinder()
    sys.meta_path.insert(0, finder)
    try:
        yield finder
    finally:
        finder.released.set()
        sys.meta_path.remove(finder)
        for name in [m for m in sys.modules if m == "lark_oapi" or m.startswith("lark_oapi.")]:
            del sys.modules[name]


def _sdk_importing_constructor(**_kwargs):
    # The real LarkClient imports the SDK inline in its constructor.
    importlib.import_module("lark_oapi")
    return MagicMock()


async def _during_import(slow_lark: _SlowLarkFinder, action=lambda: None) -> None:
    """Run *action* on the loop while the SDK import is in progress, then release it."""
    await async_wait_until(slow_lark.started.is_set, describe=lambda: "SDK import never started")
    action()
    slow_lark.released.set()


@pytest.mark.asyncio
async def test_slow_lark_import_does_not_block_the_event_loop(slow_lark) -> None:
    """A slow SDK import runs off the loop, so the loop keeps running meanwhile."""
    loop_thread = threading.current_thread()
    fake_transport = MagicMock()
    fake_transport.connect = AsyncMock()
    with (
        patch("kiro_crew.feishu.gateway.LarkClient", side_effect=_sdk_importing_constructor),
        patch("kiro_crew.feishu.gateway.FeishuTransport", return_value=fake_transport),
        patch("kiro_crew.feishu.gateway.FeishuDispatcher"),
    ):
        releaser = asyncio.create_task(_during_import(slow_lark))
        result = await maybe_start_feishu(_orch())
        await releaser

    assert result is not None
    assert slow_lark.import_threads, "the stub SDK was never imported"
    assert loop_thread not in slow_lark.import_threads
    assert not slow_lark.timed_out, "the SDK import blocked the event loop"


def _live_feishu(allowed_open_ids, *, allow_group=False, group_ids=(), degraded=False):
    return SimpleNamespace(
        feishu=SimpleNamespace(
            allowed_open_ids=list(allowed_open_ids),
            allow_group=allow_group,
            allowed_group_ids=list(group_ids),
        ),
        degraded_sections=frozenset({"feishu"} if degraded else ()),
    )


async def _start_with_edit_during_import(
    slow_lark, monkeypatch, edited, *, deferred=False, audit=None
):
    """Boot Feishu with ``ou_abc123`` allowed while *edited* is adopted mid-import.

    The watcher's replay hands the dispatcher's subscription the adopted
    section, as the section applier does, or defers it when it is degraded.
    """
    built: list = []

    def _transport(*args, **kwargs):
        transport = FeishuTransport(*args, **kwargs)
        transport.connect = AsyncMock()
        built.append(transport)
        return transport

    dispatcher = MagicMock()
    replayed: list = []

    def _replay(sub):
        replayed.append(sub)
        if not deferred:
            dispatcher.transport.reconfigure(edited.feishu)

    monkeypatch.setattr(
        "kiro_crew.feishu.transport.sel", lambda: audit if audit is not None else MagicMock()
    )
    snapshot = {"value": _live_feishu(["ou_abc123"])}
    with (
        patch("kiro_crew.feishu.gateway.LarkClient", side_effect=_sdk_importing_constructor),
        patch("kiro_crew.feishu.gateway.FeishuTransport", side_effect=_transport),
        patch("kiro_crew.feishu.gateway.FeishuDispatcher", return_value=dispatcher),
        patch("kiro_crew.feishu.gateway.live.snapshot", side_effect=lambda: snapshot["value"]),
        patch(
            "kiro_crew.feishu.gateway.live.watch",
            return_value=SimpleNamespace(replay=_replay),
        ),
    ):
        releaser = asyncio.create_task(
            _during_import(slow_lark, lambda: snapshot.__setitem__("value", edited))
        )
        result = await maybe_start_feishu(_orch())
        await releaser
    assert result is not None and len(built) == 1
    assert replayed == [dispatcher._config_sub]
    return built


@pytest.mark.asyncio
async def test_a_sender_revoked_during_the_import_is_not_authorized(slow_lark, monkeypatch) -> None:
    """An allow-list edit with no transport to apply to is in force at wiring."""
    (transport,) = await _start_with_edit_during_import(slow_lark, monkeypatch, _live_feishu([]))
    assert transport.configured_targets() == []


@pytest.mark.asyncio
async def test_a_deferred_replay_leaves_nobody_authorized(slow_lark, monkeypatch) -> None:
    """A degraded section never restores the boot allow-list."""
    (transport,) = await _start_with_edit_during_import(
        slow_lark, monkeypatch, _live_feishu(["ou_abc123"], degraded=True), deferred=True
    )
    assert transport.configured_targets() == []


@pytest.mark.asyncio
async def test_a_revocation_then_a_malformed_section_keeps_the_sender_out(
    slow_lark, monkeypatch
) -> None:
    """Revoked, then the section goes degraded: the replay defers, nobody passes."""
    (transport,) = await _start_with_edit_during_import(
        slow_lark, monkeypatch, _live_feishu([], degraded=True), deferred=True
    )
    assert transport.configured_targets() == []
    assert not transport.may_send_to("msg1", f"{SPOOL_DM_ROUTE_PREFIX}ou_abc123")


@pytest.mark.asyncio
async def test_a_replayed_section_is_adopted(slow_lark, monkeypatch) -> None:
    (transport,) = await _start_with_edit_during_import(
        slow_lark, monkeypatch, _live_feishu(["ou_new"])
    )
    assert [t.target_id for t in transport.configured_targets()] == ["user:ou_new"]


@pytest.mark.asyncio
async def test_an_unedited_start_audits_no_roster_change(slow_lark, monkeypatch, caplog) -> None:
    """Replaying the section the transport was seeded from records no grant."""
    audit = MagicMock()
    unchanged = _live_feishu(["ou_abc123"], allow_group=True, group_ids=["oc_g"])
    with caplog.at_level(logging.WARNING, logger="kiro_crew.feishu.transport"):
        (transport,) = await _start_with_edit_during_import(
            slow_lark, monkeypatch, unchanged, audit=audit
        )
    assert not [r for r in caplog.records if "flipped" in r.getMessage()]
    assert [t.target_id for t in transport.configured_targets()] == [
        "user:ou_abc123",
        "group:oc_g",
    ]
    audit.log_api_access.assert_not_called()


@pytest.mark.asyncio
async def test_a_real_reload_after_the_start_audits_exactly_its_diff(
    slow_lark, monkeypatch
) -> None:
    """After a quiet start, a real allow-list edit is audited by its own counts."""
    audit = MagicMock()
    seeded = _live_feishu(["ou_abc123"], allow_group=True, group_ids=["oc_g"])
    (transport,) = await _start_with_edit_during_import(slow_lark, monkeypatch, seeded, audit=audit)
    audit.log_api_access.assert_not_called()

    transport.reconfigure(
        _live_feishu(["ou_abc123", "ou_new"], allow_group=True, group_ids=["oc_g"]).feishu
    )

    (call,) = audit.log_api_access.call_args_list
    assert call.kwargs["outcome"] == "allow_list_changed"
    assert call.kwargs["resources"] == "added=1 removed=0 size=2"
