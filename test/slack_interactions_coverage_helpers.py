"""Fixtures and builders shared by the :mod:`kiro_crew.slack.interactions` coverage tests.

``test_slack_interactions_coverage.py`` imports these by name. The autouse fixtures stub
``aiohttp.ClientSession`` and restore the process-global handler state each test
mutates; the fixtures tests request by parameter are declared with ``name=`` and
imported under their function names, so a parameter never shadows an import.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import config_path
from kiro_crew.slack import handler as sh
from kiro_crew.slack import interactions as ix


@pytest.fixture(name="_mock_aiohttp", autouse=True)
def _mock_aiohttp_fixture():
    """Stub ``aiohttp.ClientSession`` so ``response_url`` posts never hit the network.

    Handlers post to Slack's ``response_url`` via ``async with
    aiohttp.ClientSession()``; without this the tests would attempt a real
    outbound connection.
    """
    sess = AsyncMock()
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=None)
    resp = MagicMock()
    resp.status = 200
    sess.post = AsyncMock(return_value=resp)
    with patch("aiohttp.ClientSession", return_value=sess):
        yield sess


@pytest.fixture(autouse=True)
def _restore_module_globals():
    """Snapshot/restore the process-global state these handlers mutate.

    ``VIEW_REGISTRY``, ``_resume_locks`` and ``handler._vc`` all outlive a
    single test, so without this a test that registers a handler or flips a
    voice setting would leak into whatever runs next (order dependence).
    """
    saved_registry = dict(ix.VIEW_REGISTRY)
    saved_locks = dict(ix._resume_locks)
    saved_vc = {
        f: getattr(sh._vc, f)
        for f in (
            "global_enabled",
            "auto_speak",
            "default_voice",
            "default_engine",
            "default_rate",
            "default_pitch",
            "aws_profile",
            "region",
        )
    }
    saved_allowed = sh._allowed_users
    saved_tracking = sh._tracking_channels
    yield
    ix.VIEW_REGISTRY.clear()
    ix.VIEW_REGISTRY.update(saved_registry)
    ix._resume_locks.clear()
    ix._resume_locks.update(saved_locks)
    for field, value in saved_vc.items():
        setattr(sh._vc, field, value)
    sh.set_allowed_users(saved_allowed)
    sh.set_tracking_channels(saved_tracking)


def _make_orch() -> MagicMock:
    """A minimal orchestrator double with async Slack ops."""
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock(return_value="ts1")
    orch.slack.post_blocks = AsyncMock(return_value="ts1")
    orch.slack.post_ephemeral = AsyncMock()
    orch.slack.update_message = AsyncMock()
    orch.slack.delete_message = AsyncMock()
    orch.slack.open_dm = AsyncMock(return_value="D1")
    orch.slack.views_update = AsyncMock()
    orch.slack.set_thread_status = AsyncMock()
    orch.slack.is_dm = AsyncMock(return_value=True)
    orch._allowed_users = set()
    orch._tracking_channels = set()
    orch.dashboard_state = None
    return orch


@pytest.fixture(name="orch")
def orch_fixture(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Bind an orchestrator double and allow the caller by default."""
    o = _make_orch()
    monkeypatch.setattr(ix, "_orch", o)
    monkeypatch.setattr(ix, "is_allowed_user", lambda uid: True)
    _set_owner(monkeypatch, True)
    monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=True))
    return o


def _set_owner(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Patch BOTH ``is_owner`` bindings.

    ``interactions`` imported ``is_owner`` at module scope, but several
    handlers re-import it from ``handler`` at call time, so a single patch
    only covers half the call sites.
    """
    monkeypatch.setattr(ix, "is_owner", lambda uid: value)
    monkeypatch.setattr(sh, "is_owner", lambda uid: value)


def _payload(**over: Any) -> dict:
    base: dict[str, Any] = {
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "m1", "blocks": []},
        "response_url": "",
    }
    base.update(over)
    return base


def _action_payload(action_id: str, value: str = "", **over: Any) -> dict:
    p = _payload(**over)
    p["actions"] = [{"action_id": action_id, "value": value}]
    return p


def _read_config() -> dict:
    raw = config_path().read_text(encoding="utf-8")
    parsed: dict = json.loads(raw)
    return parsed


# Every ``interactions_*`` owner module logs under the facade's name.
_IX_LOGGER = "kiro_crew.slack.interactions"


@pytest.fixture(name="ix_logs")
def ix_logs_fixture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger=_IX_LOGGER)
    return caplog


def _ix_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _IX_LOGGER]


@pytest.fixture(name="sel_log")
def sel_log_fixture(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Record the security-event-log calls the handlers make (patched via the facade)."""
    rec = MagicMock()
    monkeypatch.setattr(ix, "sel", lambda: rec)
    return rec


def _sel_ops(rec: MagicMock) -> list[tuple[str, str]]:
    return [(c.kwargs["operation"], c.kwargs["outcome"]) for c in rec.log_api_access.call_args_list]
