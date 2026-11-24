"""The account's shared model picker, read from KAS for the chat picker.

Crew declares the ``sharedModelPicker`` client capability, reads the account's
hidden models off the engine's connection view (once after ``initialize``, then on
each pushed view), records them beside the KAS catalog under the vault identity,
and the bundle-route picker marks each such model's row ``hidden_by_account``.
Nothing is written back and the account's order is not read.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import model_registry
from kiro_crew.acp import kas_shared_model_picker as picker
from kiro_crew.acp import runtime_reader_loop, runtime_spawn_launch
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.agent_sdk import kas_model_catalog as catalog
from kiro_crew.dashboard.handlers import models_kas_bundle as bundle

_ID = "fingerprint-of-account-a"
_OTHER_ID = "fingerprint-of-account-b"
_HIDDEN_ROW = {"krn": picker.MODEL_PICKER_HIDDEN_KRN, "value": ["claude-haiku-4.5", "glm-5"]}
_OTHER_ROW = {"krn": "krn:::setting/modelPickerOrder", "value": ["glm-5"]}


@pytest.fixture(autouse=True)
def _cold(_floor_monkeypatch):
    """The catalog recording itself is reset per test by ``conftest_resets``; only the registry here."""
    _floor_monkeypatch.setattr(model_registry, "_CATALOG_RATES", {})
    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    _floor_monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


_SERVING = {
    "_meta": {"kiro": {"sharedModelPicker": True, "extensionMethods": [picker.GET_STATE_METHOD]}}
}


class TestCapabilityAndParsing:
    def test_the_client_capability_is_declared_under_meta_kiro_and_keeps_the_rest(self):
        caps = picker.with_shared_model_picker(
            {"fs": {}, "_meta": {"kiro": {"telemetryEnabled": False}}}
        )
        assert caps["_meta"]["kiro"] == {
            "telemetryEnabled": False,
            "sharedModelPicker": True,
            "configurationState": True,
        }
        assert caps["fs"] == {}

    @pytest.mark.parametrize(
        "agent_caps, served",
        [
            (_SERVING, True),
            ({"_meta": {"kiro": {"sharedModelPicker": True, "extensionMethods": []}}}, False),
            ({"_meta": {"kiro": {"extensionMethods": [picker.GET_STATE_METHOD]}}}, False),
            (
                {
                    "_meta": {
                        "kiro": {
                            "sharedModelPicker": False,
                            "extensionMethods": [picker.GET_STATE_METHOD],
                        }
                    }
                },
                False,
            ),
            # Both are read under ``_meta.kiro``, never at the top level.
            (
                {
                    "_meta": {"kiro": {"sharedModelPicker": True}},
                    "extensionMethods": [picker.GET_STATE_METHOD],
                },
                False,
            ),
            ({}, False),
            ("not a dict", False),
        ],
    )
    def test_an_engine_serves_the_picker_only_with_the_flag_and_the_read_method(
        self, agent_caps, served
    ):
        assert picker.serves_shared_picker(agent_caps) is served

    def test_the_hidden_row_is_read_and_a_missing_row_is_none(self):
        assert picker.hidden_models_from_settings([_OTHER_ROW, _HIDDEN_ROW]) == [
            "claude-haiku-4.5",
            "glm-5",
        ]
        assert picker.hidden_models_from_settings([_OTHER_ROW]) is None
        assert picker.hidden_models_from_settings([]) is None
        assert picker.hidden_models_from_settings("settings") is None
        # An empty row is an account that hid nothing: not None.
        assert picker.hidden_models_from_settings([{**_HIDDEN_ROW, "value": []}]) == []

    def test_malformed_ids_are_dropped_and_duplicates_collapse(self):
        row = {**_HIDDEN_ROW, "value": [" glm-5 ", "", 7, "glm-5", "x" * 300, None]}
        assert picker.hidden_models_from_settings([row]) == ["glm-5"]

    def test_a_pushed_view_is_read_only_for_the_connection_observer(self):
        pushed = {"view": {"observer": "connection"}, "settings": [_HIDDEN_ROW]}
        assert picker.hidden_models_from_state(pushed) == ["claude-haiku-4.5", "glm-5"]
        assert picker.hidden_models_from_state({**pushed, "view": {"observer": "session"}}) is None
        assert picker.hidden_models_from_state({"settings": [_HIDDEN_ROW]}) is None
        assert picker.hidden_models_from_state(None) is None


class TestRecording:
    def test_the_row_is_recorded_for_its_account_only(self):
        assert catalog.record_hidden_models(["glm-5"], identity=_ID) is True
        assert catalog.recorded_hidden_models_snapshot(_ID) == ["glm-5"]
        assert catalog.recorded_hidden_models_snapshot("another-account") is None
        assert catalog.recorded_hidden_models_snapshot("") is None

    def test_no_row_and_no_identity_record_nothing(self):
        assert catalog.record_hidden_models(None, identity=_ID) is False
        assert catalog.record_hidden_models(["glm-5"], identity="") is False
        assert catalog.record_hidden_models(["glm-5"], identity=None) is False
        assert catalog.recorded_hidden_models_snapshot(_ID) is None

    def test_a_changed_recording_tells_the_registered_listener_once(self):
        told: list[int] = []
        catalog.on_hidden_models_changed(lambda: told.append(1))
        assert catalog.record_hidden_models(["glm-5"], identity=_ID) is True
        assert catalog.record_hidden_models(["glm-5"], identity=_ID) is True
        assert catalog.record_hidden_models(["glm-5", "kimi-k3"], identity=_ID) is True
        assert catalog.record_hidden_models(None, identity=_ID) is False
        assert catalog.record_hidden_models(["glm-5", "kimi-k3"], identity="") is False
        assert len(told) == 2

    def test_a_failing_listener_does_not_unrecord_the_row(self):
        def _boom() -> None:
            raise RuntimeError("consumer")

        catalog.on_hidden_models_changed(_boom)
        assert catalog.record_hidden_models(["glm-5"], identity=_ID) is True
        assert catalog.recorded_hidden_models_snapshot(_ID) == ["glm-5"]

    def test_an_empty_row_is_recorded_as_nothing_hidden(self):
        catalog.record_hidden_models(["glm-5"], identity=_ID)
        assert catalog.record_hidden_models([], identity=_ID) is True
        assert catalog.recorded_hidden_models_snapshot(_ID) == []


def _runtime(
    agent_caps: dict, response: object = None, *, fail: Exception | None = None
) -> MagicMock:
    runtime = MagicMock()
    runtime._agent_capabilities = agent_caps
    runtime._harness = SimpleNamespace(spawn_machine_auth=None)
    runtime._send_and_await = (
        AsyncMock(side_effect=fail) if fail else AsyncMock(return_value=response)
    )
    runtime._answer_tasks = set()
    return runtime


async def _start_and_settle(runtime: MagicMock) -> None:
    runtime_spawn_launch._start_shared_model_picker_read(runtime)
    for task in list(runtime._answer_tasks):
        await task


@pytest.mark.asyncio
class TestReadAfterInitialize:
    async def test_a_serving_engine_is_asked_for_the_connection_view_and_the_row_is_recorded(
        self, monkeypatch
    ):
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        runtime = _runtime(_SERVING, {"settings": [_OTHER_ROW, _HIDDEN_ROW]})

        await _start_and_settle(runtime)

        runtime._send_and_await.assert_awaited_once()
        method, params = runtime._send_and_await.await_args.args[:2]
        assert method == picker.GET_STATE_METHOD
        assert params == {"observer": "connection"}
        assert catalog.recorded_hidden_models_snapshot(_ID) == ["claude-haiku-4.5", "glm-5"]

    async def test_an_older_engine_is_not_asked(self, monkeypatch):
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        runtime = _runtime(
            {"_meta": {"kiro": {"extensionMethods": []}}}, {"settings": [_HIDDEN_ROW]}
        )

        await _start_and_settle(runtime)

        assert runtime._answer_tasks == set()
        runtime._send_and_await.assert_not_awaited()
        assert catalog.recorded_hidden_models_snapshot(_ID) is None

    async def test_a_failed_read_records_nothing_and_does_not_fail_the_start(self, monkeypatch):
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        runtime = _runtime(_SERVING, fail=RuntimeError("no answer"))

        await _start_and_settle(runtime)

        assert catalog.recorded_hidden_models_snapshot(_ID) is None

    async def test_a_view_without_the_row_keeps_the_earlier_recording(self, monkeypatch):
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        catalog.record_hidden_models(["glm-5"], identity=_ID)
        runtime = _runtime(_SERVING, {"settings": [_OTHER_ROW]})

        await _start_and_settle(runtime)

        assert catalog.recorded_hidden_models_snapshot(_ID) == ["glm-5"]

    async def test_the_read_does_not_hold_the_start(self, monkeypatch):
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        gate = asyncio.Event()

        async def _slow(*args, **kwargs):
            await gate.wait()
            return {"settings": [_HIDDEN_ROW]}

        runtime = _runtime(_SERVING)
        runtime._send_and_await = AsyncMock(side_effect=_slow)

        runtime_spawn_launch._start_shared_model_picker_read(runtime)

        assert len(runtime._answer_tasks) == 1
        assert catalog.recorded_hidden_models_snapshot(_ID) is None
        gate.set()
        await asyncio.gather(*runtime._answer_tasks)
        assert catalog.recorded_hidden_models_snapshot(_ID) == ["claude-haiku-4.5", "glm-5"]
        assert runtime._answer_tasks == set()

    async def test_the_row_is_stamped_with_the_account_the_read_went_out_under(self, monkeypatch):
        identities = iter([_ID, _OTHER_ID])
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: next(identities))
        runtime = _runtime(_SERVING, {"settings": [_HIDDEN_ROW]})

        await _start_and_settle(runtime)

        assert catalog.recorded_hidden_models_snapshot(_ID) == ["claude-haiku-4.5", "glm-5"]
        assert catalog.recorded_hidden_models_snapshot(_OTHER_ID) is None


@pytest.mark.asyncio
class TestPushedView:
    async def test_a_pushed_connection_view_replaces_the_recording(self, monkeypatch):
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        catalog.record_hidden_models(["glm-5"], identity=_ID)
        runtime = _runtime(_SERVING)

        await runtime_reader_loop._follow_shared_model_picker(
            runtime,
            {
                "view": {"observer": "connection"},
                "settings": [{**_HIDDEN_ROW, "value": ["kimi-k3"]}],
            },
        )

        assert catalog.recorded_hidden_models_snapshot(_ID) == ["kimi-k3"]

    async def test_views_land_in_receive_order_when_their_vault_reads_finish_out_of_order(
        self, monkeypatch
    ):
        """Hide then show on another client: the first push's vault read is slow and
        finishes after the second's. The older view must not overwrite the newer."""
        import threading

        first_read = threading.Event()
        reads = 0

        def _identity(machine=None):
            nonlocal reads
            reads += 1
            if reads == 1:
                # The first read blocks until the second recording has landed.
                first_read.wait(timeout=5)
            return _ID

        monkeypatch.setattr(catalog, "vault_identity", _identity)
        runtime = SimpleNamespace(_harness=SimpleNamespace(spawn_machine_auth=None))
        hide = asyncio.ensure_future(
            runtime_reader_loop._follow_shared_model_picker(
                runtime, {"view": {"observer": "connection"}, "settings": [_HIDDEN_ROW]}
            )
        )
        await asyncio.sleep(0)
        show = asyncio.ensure_future(
            runtime_reader_loop._follow_shared_model_picker(
                runtime,
                {
                    "view": {"observer": "connection"},
                    "settings": [{"krn": picker.MODEL_PICKER_HIDDEN_KRN, "value": []}],
                },
            )
        )
        await show
        assert catalog.recorded_hidden_models_snapshot(_ID) == []
        first_read.set()
        await hide
        assert catalog.recorded_hidden_models_snapshot(_ID) == []

    async def test_a_view_pushed_during_the_first_read_outranks_its_answer(self, monkeypatch):
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        runtime = _runtime(_SERVING)
        answer = asyncio.Event()

        async def _slow(*args, **kwargs):
            await answer.wait()
            return {"settings": [_HIDDEN_ROW]}

        runtime._send_and_await = AsyncMock(side_effect=_slow)
        runtime_spawn_launch._start_shared_model_picker_read(runtime)
        await asyncio.sleep(0)
        await runtime_reader_loop._follow_shared_model_picker(
            runtime,
            {
                "view": {"observer": "connection"},
                "settings": [{"krn": picker.MODEL_PICKER_HIDDEN_KRN, "value": ["kimi-k3"]}],
            },
        )
        answer.set()
        await asyncio.gather(*runtime._answer_tasks)
        assert catalog.recorded_hidden_models_snapshot(_ID) == ["kimi-k3"]

    async def test_another_view_or_no_row_changes_nothing(self, monkeypatch):
        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        catalog.record_hidden_models(["glm-5"], identity=_ID)
        runtime = _runtime(_SERVING)

        await runtime_reader_loop._follow_shared_model_picker(
            runtime, {"view": {"observer": "session"}, "settings": [_HIDDEN_ROW]}
        )
        await runtime_reader_loop._follow_shared_model_picker(
            runtime, {"view": {"observer": "connection"}, "settings": [_OTHER_ROW]}
        )

        assert catalog.recorded_hidden_models_snapshot(_ID) == ["glm-5"]


def _record_catalog(*ids: str) -> None:
    options = [{"value": i, "name": i, "description": ""} for i in ids]
    assert catalog.record_catalog(
        {"configOptions": [{"id": "model", "type": "select", "options": options}]},
        ACP_BACKEND_KAS,
        identity=_ID,
    )


@pytest.mark.asyncio
class TestReaderLoopRouting:
    """The push branch of the reader loop: gated, filtered and capped before a task runs."""

    def _state_frame(self, observer: str, settings: list) -> dict:
        return {
            "jsonrpc": "2.0",
            "method": picker.STATE_NOTIFICATION,
            "params": {"view": {"observer": observer}, "settings": settings},
        }

    async def _run(self, rt, reader, frames) -> dict:
        """Feed *frames*, settle the tasks, and return the drops counted before the reader exits."""
        from acp_runtime_helpers import _drain, _feed, _start_reader, _stop_reader

        task = await _start_reader(rt)
        try:
            for frame in frames:
                _feed(reader, frame)
            await _drain(reader)
            for pending in list(rt._answer_tasks):
                await pending
            return dict(rt._dropped_frames)
        finally:
            await _stop_reader(task)

    async def test_a_connection_view_from_a_serving_engine_is_recorded(self, monkeypatch):
        from acp_runtime_helpers import _make_runtime

        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        rt, reader, _ = _make_runtime()
        rt._agent_capabilities = _SERVING
        drops = await self._run(rt, reader, [self._state_frame("connection", [_HIDDEN_ROW])])
        assert catalog.recorded_hidden_models_snapshot(_ID) == ["claude-haiku-4.5", "glm-5"]
        assert drops == {}

    async def test_a_session_view_a_rowless_view_and_a_non_serving_engine_are_counted_drops(
        self, monkeypatch
    ):
        from acp_runtime_helpers import _make_runtime

        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        rt, reader, _ = _make_runtime()
        rt._agent_capabilities = _SERVING
        drops = await self._run(
            rt,
            reader,
            [
                self._state_frame("session", [_HIDDEN_ROW]),
                self._state_frame("connection", [_OTHER_ROW]),
            ],
        )
        assert catalog.recorded_hidden_models_snapshot(_ID) is None
        assert drops == {("-", picker.STATE_NOTIFICATION): 2}

        rt2, reader2, _ = _make_runtime()
        rt2._agent_capabilities = {"_meta": {"kiro": {"extensionMethods": []}}}
        drops = await self._run(rt2, reader2, [self._state_frame("connection", [_HIDDEN_ROW])])
        assert catalog.recorded_hidden_models_snapshot(_ID) is None
        assert drops == {("-", picker.STATE_NOTIFICATION): 1}

    async def test_a_push_past_the_answer_task_cap_is_a_counted_drop(self, monkeypatch):
        from acp_runtime_helpers import _make_runtime

        monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)
        rt, reader, _ = _make_runtime()
        rt._agent_capabilities = _SERVING
        rt._max_answer_tasks = 0
        drops = await self._run(rt, reader, [self._state_frame("connection", [_HIDDEN_ROW])])
        assert catalog.recorded_hidden_models_snapshot(_ID) is None
        assert drops == {("-", picker.STATE_NOTIFICATION): 1}


@pytest.mark.asyncio
class TestBundlePickerRows:
    @pytest.fixture(autouse=True)
    def _ready(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(bundle, "_bundle_installed", lambda: True)
        _floor_monkeypatch.setattr(bundle, "_vault_signed_in", lambda: True)
        _floor_monkeypatch.setattr(catalog, "vault_identity", lambda machine=None: _ID)

    async def test_hidden_models_stay_listed_with_the_flag(self):
        _record_catalog("auto", "claude-haiku-4.5", "glm-5", "kimi-k3")
        catalog.record_hidden_models(["claude-haiku-4.5", "glm-5"], identity=_ID)
        request = MagicMock()
        request.app = {}

        rows = json.loads((await bundle.api_models_kas_bundle(request)).body)

        assert [r["model_name"] for r in rows] == ["auto", "claude-haiku-4.5", "glm-5", "kimi-k3"]
        assert [r.get("hidden_by_account") for r in rows] == [None, True, True, None]

    async def test_no_recorded_row_marks_nothing(self):
        _record_catalog("auto", "glm-5")
        request = MagicMock()
        request.app = {}

        rows = json.loads((await bundle.api_models_kas_bundle(request)).body)

        assert all("hidden_by_account" not in r for r in rows)

    async def test_another_accounts_row_marks_nothing(self):
        _record_catalog("auto", "glm-5")
        catalog.record_hidden_models(["glm-5"], identity="another-account")
        request = MagicMock()
        request.app = {}

        rows = json.loads((await bundle.api_models_kas_bundle(request)).body)

        assert all("hidden_by_account" not in r for r in rows)
