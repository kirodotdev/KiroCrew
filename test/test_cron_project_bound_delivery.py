"""Owner gates for project-bound cron output on dashboard WebSockets."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


class TestProjectBoundOutputWebSocketGate:
    @staticmethod
    def _ws() -> MagicMock:
        ws = MagicMock(closed=False)
        ws.get.side_effect = lambda key, default=None: {
            "_is_dashboard_user": True,
        }.get(key, default)
        return ws

    @pytest.mark.parametrize(
        "event,payload",
        [
            ("notification", {"kind": "cron", "body": "private", "project_bound": True}),
            (
                "chat_message",
                {"slot": "cron-j1", "content": "private", "meta": {"project_bound": True}},
            ),
        ],
    )
    def test_non_owner_is_denied_project_bound_output(
        self, state: DashboardState, event: str, payload: dict
    ) -> None:
        ws = self._ws()
        state.register_ws(ws)
        assert state._ws_client_allowed(ws, event, payload) is False

    @pytest.mark.parametrize("bad", ["false", 0, None, [], {}])
    def test_malformed_provenance_is_denied(self, state: DashboardState, bad: object) -> None:
        ws = self._ws()
        state.register_ws(ws)
        assert (
            state._ws_client_allowed(
                ws,
                "notification",
                {"kind": "cron", "body": "private", "project_bound": bad},
            )
            is False
        )

    def test_owner_still_receives_project_bound_output(self, state: DashboardState) -> None:
        ws = self._ws()
        state.register_ws(ws, owner=True)
        assert (
            state._ws_client_allowed(
                ws,
                "notification",
                {"kind": "cron", "body": "private", "project_bound": True},
            )
            is True
        )

    def test_explicitly_unbound_output_is_unchanged(self, state: DashboardState) -> None:
        ws = self._ws()
        state.register_ws(ws)
        assert (
            state._ws_client_allowed(
                ws,
                "notification",
                {"kind": "cron", "body": "safe", "project_bound": False},
            )
            is True
        )
