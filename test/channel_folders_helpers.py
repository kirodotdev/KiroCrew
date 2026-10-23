"""The state fixture, config writer and conversation-log fake the channel folder tests share.

``test_channel_folders.py`` and its ``_backfill`` sibling import these by name.
``dashboard_state`` is declared ``name="dashboard_state"`` on ``dashboard_state_fixture``
so no test parameter shadows the import.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import config_path
from kiro_crew.history_projection import METADATA_LINE_READABLE


@pytest.fixture(name="dashboard_state")
def dashboard_state_fixture(tmp_path: Any) -> Any:
    return _make_state(tmp_path)


def _write_config(section: str, folder: str) -> None:
    """Point ``<section>.session_folder`` at *folder* in the test's config.json."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({section: {"session_folder": folder}}), encoding="utf-8")


class _FakeLog:
    """Minimal ConversationLog stand-in: one channel session, one message.

    The tab and the channel share ONE record, so *meta* is keyed by
    the session key itself. ``update_metadata`` merges like the real thing, which
    is what lets a test assert that the filing marker was actually persisted.
    """

    def __init__(self, keys: list[str], meta: dict[str, Any] | None = None) -> None:
        self._keys = keys
        self._meta: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (meta or {}).items()}

    def list_sessions(self) -> list[dict[str, Any]]:
        return [{"key": k, "title": "", "modified": 0.0} for k in self._keys]

    def list_sessions_status(self) -> tuple[list[dict[str, Any]], bool]:
        # Routed through list_sessions so the subclass override below still
        # drives the reconciler's listing.
        return self.list_sessions(), True

    def get_metadata(self, key: str) -> dict[str, Any]:
        return dict(self._meta.get(key, {}))

    def get_metadata_status(self, key: str) -> tuple[dict[str, Any], bool]:
        return self.get_metadata(key), True

    def metadata_line_state(self, key: str) -> str:
        return METADATA_LINE_READABLE

    def update_metadata(self, key: str, fields: dict[str, Any]) -> None:
        self._meta.setdefault(key, {}).update(fields)

    def update_metadata_if(
        self,
        key: str,
        fields: dict[str, Any],
        guard: Any,
        *,
        require_existing: bool = False,
    ) -> bool:
        """Merge only if *guard* still accepts the stored record.

        The real method evaluates the guard inside the cross-process lock, so a
        write that landed while the caller queued IS visible to it. Mirroring that
        here — guard first, against the current record — is what lets a test prove
        the reconcile pass yields to a placement made mid-pass.

        ``require_existing`` is modelled on ``self._keys``, which is this fake's
        whole notion of a session existing: ``list_sessions`` and
        ``read_messages`` both read it. Checked BEFORE the guard, matching the
        real order, because the real store's metadata read reports a deleted
        session as an empty-but-readable record, so a guard that accepts an
        empty record cannot be the thing that refuses a deletion.
        """
        if require_existing and key not in self._keys:
            return False
        if not guard(self.get_metadata(key)):
            return False
        self.update_metadata(key, fields)
        return True

    def read_messages(self, key: str) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "hello"}] if key in self._keys else []
