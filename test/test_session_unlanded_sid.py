"""A fresh native session that never received its history must not survive a restart.

After a discard recovery the next turn claims a fresh native session whose
sid is mapped at creation, while the "history still owed" debt
(``first_turn_history_owed``) lives only in memory. kiro-cli is safe because
``SessionMap.get`` prunes a sid whose ``.jsonl`` holds no turn. Every other
backend's sid has no transcript this process can stat, so after a restart a
successful ``session/load`` on the unused session would run the next turn with
no history. The debt is mirrored onto the map as the sid's unlanded marker,
which ``get`` honours for those backends.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_KAS, PROVIDER_LABEL_DEFAULT
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session_map import UNLANDED_SID_FIELD, SessionMap, canonical_key

KEY = "dashboard:chat-1-abc"


@pytest.fixture()
def map_dir(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield tmp_path


def _restart(sm: SessionMap) -> SessionMap:
    """Persist *sm* and load a fresh map from the same file, as a restart does."""
    sm.flush()
    return SessionMap()


def _kas_label() -> str:
    from kiro_crew.providers.acp import AcpProvider, provider_label

    p = MagicMock(spec=AcpProvider)
    p.client = MagicMock()
    p.client.backend = ACP_BACKEND_KAS
    label = provider_label(p)
    assert label != PROVIDER_LABEL_DEFAULT
    return label


class TestSessionMapUnlandedSid:
    def test_marked_sid_is_refused_after_restart(self, map_dir):
        sm = SessionMap()
        sm.set(KEY, "sid-fresh", provider=_kas_label())
        assert sm.mark_sid_unlanded(KEY, "sid-fresh") is True

        sm2 = _restart(sm)
        assert sm2.get(KEY) is None
        # Refused, not repaired: the entry and its marker stay, so a later
        # write-back of the same sid is still refused.
        entry = sm2._data.get(canonical_key(KEY)) or {}
        assert entry.get("sid") == "sid-fresh"
        assert entry.get(UNLANDED_SID_FIELD) == "sid-fresh"
        assert sm2.get(KEY) is None

    def test_unmarked_sid_still_resumes(self, map_dir):
        sm = SessionMap()
        sm.set(KEY, "sid-landed", provider=_kas_label())
        assert _restart(sm).get(KEY) == "sid-landed"

    def test_cleared_marker_resumes(self, map_dir):
        sm = SessionMap()
        sm.set(KEY, "sid-fresh", provider=_kas_label())
        sm.mark_sid_unlanded(KEY, "sid-fresh")
        assert sm.clear_sid_unlanded(KEY) is True
        assert _restart(sm).get(KEY) == "sid-fresh"

    def test_mark_refuses_a_sid_the_entry_does_not_hold(self, map_dir):
        sm = SessionMap()
        sm.set(KEY, "sid-current", provider=_kas_label())
        assert sm.mark_sid_unlanded(KEY, "sid-other") is False
        assert sm.mark_sid_unlanded("dashboard:missing", "sid-current") is False
        assert sm.get(KEY) == "sid-current"

    def test_a_new_sid_does_not_inherit_the_marker(self, map_dir):
        sm = SessionMap()
        label = _kas_label()
        sm.set(KEY, "sid-old", provider=label)
        sm.mark_sid_unlanded(KEY, "sid-old")
        sm.set(KEY, "sid-new", provider=label)
        assert UNLANDED_SID_FIELD not in sm._data[canonical_key(KEY)]
        assert _restart(sm).get(KEY) == "sid-new"

    def test_rewriting_the_same_sid_keeps_the_marker(self, map_dir):
        """``close_all`` writes every live sid back at shutdown; that write must
        not erase the marker for the sid it rewrites."""
        sm = SessionMap()
        label = _kas_label()
        sm.set(KEY, "sid-fresh", provider=label)
        sm.mark_sid_unlanded(KEY, "sid-fresh")
        sm.set(KEY, "sid-fresh", provider=label, cwd="/tmp/ws")
        assert _restart(sm).get(KEY) is None

    @pytest.mark.asyncio
    async def test_marker_is_on_disk_once_aflush_returns_on_the_loop(self, map_dir):
        """On the event loop ``_save`` only schedules a debounced flush; the turn
        awaits ``aflush`` after arming. Read the file with a fresh map, as a
        crash-restart would, once that await returns."""
        sm = SessionMap()
        sm.set(KEY, "sid-fresh", provider=_kas_label())
        await sm.aflush()  # the sid is already on disk, as for a prewarmed session
        assert SessionMap().get(KEY) == "sid-fresh"
        assert sm.mark_sid_unlanded(KEY, "sid-fresh") is True
        await sm.aflush()
        assert SessionMap().get(KEY) is None


def _acp_provider(sid: str, backend: str):
    from kiro_crew.providers.acp import AcpProvider

    p = MagicMock(spec=AcpProvider)
    p.shutdown = AsyncMock()
    p.context_usage_pct = MagicMock(return_value=0.0)
    p.cwd = "/tmp/test"
    p.client = MagicMock()
    p.client._session_id = sid
    p.client.backend = backend
    return p


class TestFirstTurnDebtSurvivesRestart:
    @pytest.mark.asyncio
    async def test_owed_history_refuses_the_fresh_sid_after_close_all(self, map_dir):
        from kiro_crew.session import SessionManager, _Session

        mgr = SessionManager(KiroCrewConfig(), provider_factory=MagicMock())
        provider = _acp_provider("sid-fresh", ACP_BACKEND_KAS)
        mgr._sessions[KEY] = _Session(provider=provider)
        mgr._session_map.set(KEY, "sid-fresh", provider=_kas_label())

        assert mgr.mark_first_turn_history_owed(KEY) is True
        await mgr.close_all()  # writes the live sid back, as a graceful restart does

        assert _restart(mgr._session_map).get(KEY) is None

    @pytest.mark.asyncio
    async def test_a_live_lookup_does_not_erase_the_marker(self, map_dir):
        """A ``get`` while the debt is owed (an export resolving the sid mid-turn)
        must not drop the marker, or ``close_all``'s write-back of the same sid
        would make it resumable after the restart."""
        from kiro_crew.session import SessionManager, _Session

        mgr = SessionManager(KiroCrewConfig(), provider_factory=MagicMock())
        provider = _acp_provider("sid-fresh", ACP_BACKEND_KAS)
        mgr._sessions[KEY] = _Session(provider=provider)
        mgr._session_map.set(KEY, "sid-fresh", provider=_kas_label())

        mgr.mark_first_turn_history_owed(KEY)
        assert mgr._session_map.get(KEY) is None
        await mgr.close_all()

        assert _restart(mgr._session_map).get(KEY) is None

    @pytest.mark.asyncio
    async def test_settled_history_resumes_the_sid_after_close_all(self, map_dir):
        from kiro_crew.session import SessionManager, _Session

        mgr = SessionManager(KiroCrewConfig(), provider_factory=MagicMock())
        provider = _acp_provider("sid-fresh", ACP_BACKEND_KAS)
        mgr._sessions[KEY] = _Session(provider=provider)
        mgr._session_map.set(KEY, "sid-fresh", provider=_kas_label())

        mgr.mark_first_turn_history_owed(KEY)
        assert mgr.consume_first_turn_history_owed(KEY) is True
        await mgr.close_all()

        assert _restart(mgr._session_map).get(KEY) == "sid-fresh"
