"""Code-channel workspace: the watcher's working status for a channel's workers is
cleared even when a clear fails or the feature goes off mid-run.

The harness, fixtures and fakes are ``test_slack_code_channel_workspace``'s,
imported by name so its tests are not collected twice. The status goes through
the real driver over a fake Slack web client.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import test_slack_code_channel_workspace as _ws

from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import status

# Fixtures, by name: pytest finds a fixture through the module's namespace.
_no_sandbox = _ws._no_sandbox
_feature_on = _ws._feature_on
audits = _ws.audits
repo = _ws.repo


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"ok": False, "error": code}


class _StatusSlack:
    """The two attributes the status driver uses on a Slack client. The next
    ``active`` write raises ``fail`` when it is set."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail: Exception | None = None
        self._web = SimpleNamespace(api_call=self._api_call)

    async def _api_call(self, method: str, **kwargs: Any) -> Any:
        sent = kwargs["params"]["status"]
        self.sent.append(sent)
        if sent == "active" and self.fail is not None:
            error, self.fail = self.fail, None
            raise error
        return {"ok": True}

    def _inject_team(self, channel: str, params: dict[str, Any]) -> None:
        return None


@pytest.fixture
def slack():
    fake = _StatusSlack()
    status.set_slack_provider(lambda: fake)
    yield fake
    status.set_slack_provider(None)


def _channel_with_a_running_worker(tmp_path, repo, monkeypatch):
    h = _ws.Harness(tmp_path)
    dash = _ws.FakeDashboard()

    async def create_folder(state, parent_id, name, project_dir):
        return state.add_folder(f"F{len(state._folders)}", name, parent_id, project_dir)

    h.svc._create_folder = create_folder
    folder_id, _wt = h.run(
        h.svc.plan_session(
            dash,
            channel_id="C1",
            team_id="T1",
            title="t",
            channel_name="c",
            project_dir=str(repo),
            parent_folder_id="F0",
        )
    )
    refreshes: list[Any] = []
    monkeypatch.setattr(h.svc, "schedule_refresh", refreshes.append)
    return h, dash, dash.add_slot("chat-worker", folder_id, running=True), refreshes


@pytest.mark.parametrize(
    "error, sent",
    [
        (ConnectionError("connection reset"), ["processing", "active", "active"]),
        (_Refused("channel_not_found"), ["processing", "active"]),
    ],
    ids=["transient", "refused-for-good"],
)
def test_a_failed_worker_status_clear_is_retried_until_taken_or_refused_for_good(
    tmp_path, repo, audits, monkeypatch, slack, error, sent
):
    h, dash, worker, _refreshes = _channel_with_a_running_worker(tmp_path, repo, monkeypatch)
    h.run(h.svc.watch_tick(dash))
    assert slack.sent == ["processing"]
    worker.turn_running = False
    slack.fail = error
    for _ in range(3):
        h.run(h.svc.watch_tick(dash))
    assert slack.sent == sent


def test_turning_code_channels_off_mid_run_clears_the_worker_status(
    tmp_path, repo, audits, monkeypatch, slack
):
    h, dash, worker, refreshes = _channel_with_a_running_worker(tmp_path, repo, monkeypatch)
    h.run(h.svc.watch_tick(dash))
    assert slack.sent == ["processing"]
    monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: _ws._cfg(False))
    h.run(h.svc.watch_tick(dash))  # the worker still runs; the feature is off
    worker.turn_running = False
    h.run(h.svc.watch_tick(dash))  # its turn ended: no refresh, no views
    assert slack.sent == ["processing", "active"]
    assert refreshes == []
