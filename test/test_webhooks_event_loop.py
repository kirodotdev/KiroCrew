"""Webhook store I/O must never run on the asyncio event loop.

The webhook stores persist with ``flock`` + read-modify-write + ``fsync``. The
inbound route is externally callable, so if any of that ran inline, one caller —
or a dashboard token edit holding the same lock — would stall every task on the
single gateway loop: other sessions, WebSocket pushes and the liveness
heartbeat. AUTOSDE ``no-blocking-call-on-event-loop`` makes that blocking.

These tests assert the property structurally rather than by timing, so they
cannot go green on a fast disk while the defect is still present: the store
functions raise if they are ever entered while a loop is running in this thread,
which is exactly what an un-offloaded call site would do.
"""

from __future__ import annotations

import asyncio
import functools
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as hooks_handlers


class _LoopGuard:
    """Wrap a callable so it fails when invoked on a running event loop.

    Implements ``__get__`` because it stands in for an unbound method: a plain
    callable patched onto a class is not a descriptor, so ``self`` would never
    be passed through.
    """

    def __init__(self, fn, label: str):
        self._fn = fn
        self._label = label
        self.calls = 0

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return functools.partial(self.__call__, obj)

    def __call__(self, *args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # worker thread — the whole point
        else:
            raise AssertionError(
                f"{self._label} ran on the event loop; offload it with "
                "asyncio.to_thread (AUTOSDE no-blocking-call-on-event-loop)"
            )
        self.calls += 1
        return self._fn(*args, **kwargs)


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(webhooks, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_handlers, "_HOOK_STORE_PATH", tmp_path / "hooks.json")
    # Reset on BOTH sides: the replay set and throttle buckets are
    # process-global, so a signature left behind here would follow the worker
    # into another module's tests.
    webhooks._reset_signature_replay()
    webhooks._reset_auth_throttle()
    yield tmp_path
    webhooks._reset_signature_replay()
    webhooks._reset_auth_throttle()


def _request(headers=None, body=b"{}", remote="10.1.2.3"):
    req = MagicMock()
    req.headers = headers or {}
    req.remote = remote
    req.content_length = len(body)
    req.can_read_body = False

    async def _read():
        return body

    req.read = _read
    req.app = {"state": SimpleNamespace(_background_tasks=set())}
    return req


class TestInboundRouteOffloadsStoreIo:
    """Each rejection path of the external route must stay off the loop."""

    def test_kill_switch_path(self, store_dir):
        webhooks.WebhookTokenStore(store_dir).set_switch(False)
        switch = _LoopGuard(webhooks.WebhookTokenStore.is_switch_on, "is_switch_on")
        record = _LoopGuard(webhooks.WebhookRunStore.record, "run_store.record")

        with patch.object(webhooks.WebhookTokenStore, "is_switch_on", switch), \
                patch.object(webhooks.WebhookRunStore, "record", record), \
                patch.object(hooks_handlers, "_sel", MagicMock()):
            resp = asyncio.run(hooks_handlers.api_hooks_agent(_request()))

        assert resp.status == 503
        assert switch.calls == 1
        assert record.calls == 1

    def test_unauthorized_path(self, store_dir):
        verify = _LoopGuard(webhooks.WebhookTokenStore.verify, "token_store.verify")
        record = _LoopGuard(webhooks.WebhookRunStore.record, "run_store.record")

        with patch.object(webhooks.WebhookTokenStore, "verify", verify), \
                patch.object(webhooks.WebhookRunStore, "record", record), \
                patch.object(hooks_handlers, "_sel", MagicMock()):
            resp = asyncio.run(
                hooks_handlers.api_hooks_agent(
                    _request(headers={"Authorization": "Bearer nope"})
                )
            )

        assert resp.status == 401
        assert verify.calls == 1
        assert record.calls == 1

    def test_signed_success_path_stamps_off_loop(self, store_dir):
        raw, secret, entry = webhooks.WebhookTokenStore(store_dir).create("ci")
        body = json.dumps({"message": "hi", "sessionKey": "hook:x"}).encode()
        ts = 1_700_000_000
        headers = {
            "Authorization": f"Bearer {raw}",
            webhooks.TIMESTAMP_HEADER: str(ts),
            webhooks.SIGNATURE_HEADER: webhooks.sign_payload(secret, ts, body),
        }
        stamp = _LoopGuard(webhooks.WebhookTokenStore.stamp_used, "stamp_used")

        async def _noop_run(*args, **kwargs):
            return None

        with patch.object(webhooks.WebhookTokenStore, "stamp_used", stamp), \
                patch.object(hooks_handlers, "_sel", MagicMock()), \
                patch.object(hooks_handlers, "_run_hook_agent", _noop_run), \
                patch.object(webhooks.time, "time", lambda: float(ts)):
            resp = asyncio.run(
                hooks_handlers.api_hooks_agent(_request(headers=headers, body=body))
            )

        assert resp.status == 200
        assert stamp.calls == 1
        assert entry["id"]


class TestManagementRoutesOffloadStoreIo:
    """The dashboard routes share the same lock, so they must offload too."""

    def test_token_create_and_delete(self, store_dir):
        create = _LoopGuard(webhooks.WebhookTokenStore.create, "token_store.create")
        delete = _LoopGuard(webhooks.WebhookTokenStore.delete, "token_store.delete")

        req = MagicMock()

        async def _json():
            return {"label": "ci"}

        req.json = _json
        req.get = lambda *a, **k: "dashboard"
        req.match_info = {}

        with patch.object(webhooks.WebhookTokenStore, "create", create), \
                patch.object(hooks_handlers, "_sel", MagicMock()):
            resp = asyncio.run(hooks_handlers.api_webhook_token_create(req))
        assert resp.status == 201
        assert create.calls == 1

        token_id = json.loads(resp.text)["entry"]["id"]
        req.match_info = {"token_id": token_id}
        with patch.object(webhooks.WebhookTokenStore, "delete", delete), \
                patch.object(hooks_handlers, "_sel", MagicMock()):
            resp = asyncio.run(hooks_handlers.api_webhook_token_delete(req))
        assert resp.status == 200
        assert delete.calls == 1

    def test_switch_write(self, store_dir):
        switch = _LoopGuard(webhooks.WebhookTokenStore.set_switch, "set_switch")

        req = MagicMock()

        async def _json():
            return {"enabled": False}

        req.json = _json
        req.get = lambda *a, **k: "dashboard"

        with patch.object(webhooks.WebhookTokenStore, "set_switch", switch), \
                patch.object(hooks_handlers, "_sel", MagicMock()):
            resp = asyncio.run(hooks_handlers.api_webhooks_switch(req))

        assert resp.status == 200
        assert switch.calls == 1

    def test_read_endpoint_snapshot(self, store_dir):
        entries = _LoopGuard(
            webhooks.WebhookTokenStore.public_entries, "public_entries"
        )
        runs = _LoopGuard(webhooks.WebhookRunStore.list_runs, "run_store.list_runs")

        req = MagicMock()
        req.get = lambda *a, **k: "dashboard"
        req.app = {"port": 6776}
        req.url = SimpleNamespace(port=6776)

        with patch.object(webhooks.WebhookTokenStore, "public_entries", entries), \
                patch.object(webhooks.WebhookRunStore, "list_runs", runs), \
                patch.object(
                    hooks_handlers,
                    "_webhook_endpoint_url",
                    lambda _req: "http://127.0.0.1:6776/api/hooks/agent",
                ), \
                patch.object(hooks_handlers, "_sel", MagicMock()):
            resp = asyncio.run(hooks_handlers.api_webhooks(req))

        assert resp.status == 200
        assert entries.calls == 1
        assert runs.calls == 1
