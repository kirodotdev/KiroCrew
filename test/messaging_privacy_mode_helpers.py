"""Keys, doubles and fixtures the privacy-mode test modules share.

A Telegram session key, the autouse tracker reset, a real
``SessionMap`` rooted in ``tmp_path`` (``session_map``) and the ``log_api_access``
capture (``audits``) -- fixtures, declared ``@pytest.fixture(name=...)`` and imported
under their function names -- plus a ``SessionManager`` stand-in, the helper that
lands a map's pending state on disk, and the notice recorder.
``test_messaging_privacy_mode.py`` and ``test_messaging_privacy_mode_reservations.py``
import them.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from kiro_crew.messaging import privacy_mode
from kiro_crew.session_map import SessionMap

#: A key in a namespace that is NOT Slack. The whole point of the hoist.
_TG_KEY = "telegram:kirocrew:direct:4242"


@pytest.fixture(autouse=True)
def _isolate_trackers():
    """The trackers are process globals; make every test hermetic."""
    privacy_mode.reset()
    yield
    privacy_mode.reset()


@pytest.fixture(name="session_map")
def session_map_fixture(tmp_path, monkeypatch):
    """A real ``SessionMap`` rooted in *tmp_path*, plus a factory for a fresh one.

    A second instance reading the same directory IS the restart: it shares no
    in-memory state with the first, only the file on disk.
    """
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    return SessionMap


class _Sessions:
    """Minimal ``SessionManager`` stand-in exposing a real ``SessionMap``."""

    def __init__(self, sm: object = None) -> None:
        self._session_map = sm


async def _land_on_disk(sm: SessionMap) -> None:
    """Make *sm*'s pending state durable and leave no task behind.

    ``set_flag`` inside a running loop schedules a DEBOUNCED write, which the
    test's loop would outlive ("Task was destroyed but it is pending"). Retiring
    the task re-owes the dirty mark, so the synchronous flush after it is what
    actually lands the bytes the restart then reads.
    """
    task = getattr(sm, "_flush_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            # flake-ok: pre-existing await with no timeout of the flush task cancelled just above, moved verbatim; tracked for a de-flake pass
            await task
    sm.flush()


class _Recorder:
    """Collects the notices and hook calls a channel would have delivered."""

    def __init__(self) -> None:
        self.notices: list[str] = []
        self.hooks: list[str] = []
        self.restricted_at_notice: list[bool] = []

    async def notify(self, message: str) -> None:
        self.notices.append(message)

    async def on_applied(self, mode: str) -> None:
        self.hooks.append(mode)


@pytest.fixture(name="audits")
def audits_fixture(monkeypatch):
    """Capture every ``log_api_access`` call the module makes."""
    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(privacy_mode, "sel", lambda: fake)
    return events
