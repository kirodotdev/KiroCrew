"""A run the sweep cannot reclaim yet is not evaluated in full on every sweep.

A delivered run whose transcript belongs to a provider this build has no cleanup
route for stays on disk until its retry window ends. Evaluating it again each
minute reads the same files and reaches the same verdict. The sweep remembers the
verdict with what it was read from, and these tests pin both halves: an unchanged
run is skipped, and any change to what the verdict rests on evaluates it again.

Every count is a call count on the product's own seam, never wall time.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

import kiro_crew.subagent_persistence as sp
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import wait_until

NO_ROUTE = "kas"
ROUTED = "acp"
DAY = 86400


def _wait_for_a_later_timestamp(path) -> None:
    """Return once a write made now is stamped later than *path* was.

    The kernel stamps files on a coarse clock, so an edit made in the same tick as
    the file's last write leaves every timestamp equal. The tests below edit a file
    right after writing it and need that edit to show, so they wait on the signal
    itself: a probe beside the file is rewritten until its modification time passes the
    file's own.
    """
    held = path.stat().st_mtime_ns
    probe = path.with_name("clock-probe")

    def stamped_later() -> bool:
        probe.write_bytes(b"")
        return probe.stat().st_mtime_ns > held

    wait_until(stamped_later)
    probe.unlink()


def _seed_delivered(agent_id: str, provider: str, *, age_secs: float = 2 * DAY) -> None:
    """A delivered run past its window, with one protected cleanup generation."""
    sp.create_agent_folder(agent_id, task=agent_id)
    sp.update_state(agent_id, session_id=f"sid-{agent_id}", provider=provider, keep=False)
    sp.remember_live_cleanup_identity(
        agent_id, session_id=f"sid-{agent_id}", provider=provider, keep=False
    )
    sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
    ts_path = sp._agent_dir(agent_id) / "tombstone.json"
    tombstone = json.loads(ts_path.read_text())
    # flake-ok: back-dated on the live clock, well inside the 90-day retry window
    tombstone["died"] = time.time() - age_secs
    ts_path.write_text(json.dumps(tombstone))
    # A restarted gateway: only the protected record remains.
    with sp._CLEANUP_IDENTITY_LOCK:
        sp._LIVE_CLEANUP_IDENTITIES.clear()


@pytest.fixture
def cleanup_calls():
    """Each ``(session id, provider)`` the sweep tried to clean, through the real function."""
    calls: list[tuple[str, str]] = []
    real = sp._cleanup_session_files_sync

    def recording(session_id, provider=sp.PROVIDER_LABEL_DEFAULT, *, cwd=""):
        calls.append((session_id, provider))
        return real(session_id, provider, cwd=cwd)

    with patch.object(sp, "_cleanup_session_files_sync", recording):
        yield calls


def _sweep() -> int:
    # Both windows at zero, as the sweep-read tests use: every seeded run is due.
    return sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0)


def test_the_no_route_answer_is_what_the_cleanup_itself_gives():
    assert sp._provider_cleanup_has_route(NO_ROUTE) is False
    assert sp._cleanup_session_files_sync("sid-x", NO_ROUTE) is False
    assert sp._provider_cleanup_has_route(sp.PROVIDER_LABEL_DEFAULT) is True


def test_the_cleanup_refuses_a_provider_the_route_answer_rejects(monkeypatch):
    """One statement of which providers have a route: the cleanup asks it first.

    If the cleanup could delete for a provider the answer calls route-less, the
    sweep would remember "cannot reclaim" for a run whose cleanup might succeed.
    """
    monkeypatch.setattr(sp, "_provider_cleanup_has_route", lambda provider: False)

    with patch.object(sp, "kiro_sessions_dir") as sessions_dir:
        assert sp._cleanup_session_files_sync("sid-x", ROUTED) is False

    sessions_dir.assert_not_called()


def test_an_unchanged_run_with_no_cleanup_route_is_evaluated_once(cleanup_calls):
    _seed_delivered("no-route", NO_ROUTE)

    assert _sweep() == 0
    assert cleanup_calls == [("sid-no-route", NO_ROUTE)]
    assert _sweep() == 0
    assert _sweep() == 0

    assert cleanup_calls == [("sid-no-route", NO_ROUTE)], "the run was evaluated again"
    assert sp._agent_dir("no-route").exists()


def test_a_run_whose_cleanup_failed_on_a_routed_provider_is_tried_every_sweep():
    """CONTROL: a failure that may pass must not be remembered."""
    _seed_delivered("routed", ROUTED)

    with patch.object(sp, "_cleanup_session_files_sync", return_value=False) as cleanup:
        assert _sweep() == 0
        assert _sweep() == 0

    assert cleanup.call_count == 2
    assert sp._agent_dir("routed").exists()


def test_a_new_protected_record_is_acted_on_at_the_next_sweep(cleanup_calls):
    _seed_delivered("re-recorded", NO_ROUTE)
    assert _sweep() == 0
    record = sp._cleanup_identities_path("re-recorded")
    payload = json.loads(record.read_text())
    payload["identities"][0]["provider"] = sp.PROVIDER_LABEL_DEFAULT
    # This test edits the file in place and to the same length, which only its
    # modification time shows, so the edit has to land in a later clock tick. The
    # product replaces the file, and a new inode shows at once.
    _wait_for_a_later_timestamp(record)
    record.write_text(json.dumps(payload))

    assert _sweep() == 1

    assert cleanup_calls[-1] == ("sid-re-recorded", sp.PROVIDER_LABEL_DEFAULT)
    assert not sp._agent_dir("re-recorded").exists()


def test_an_identity_published_in_memory_only_is_acted_on_at_the_next_sweep(cleanup_calls):
    """The gateway publishes in memory before the record reaches disk."""
    _seed_delivered("published", NO_ROUTE)
    assert _sweep() == 0
    before = len(cleanup_calls)

    with sp._CLEANUP_IDENTITY_LOCK:
        sp._LIVE_CLEANUP_IDENTITIES["published"] = [
            {"session_id": "sid-published-2", "provider": NO_ROUTE, "keep": False}
        ]
    assert _sweep() == 0

    assert len(cleanup_calls) > before, "a newly published identity was not looked at"


def test_a_state_change_is_evaluated_at_the_next_sweep(cleanup_calls):
    _seed_delivered("restated", NO_ROUTE)
    assert _sweep() == 0
    before = len(cleanup_calls)

    sp.update_state("restated", note="changed after the sweep")
    assert _sweep() == 0

    assert len(cleanup_calls) > before, "a rewritten state was not looked at"


def test_the_run_is_removed_once_its_retry_window_ends(cleanup_calls, monkeypatch):
    _seed_delivered("expired", NO_ROUTE)
    assert _sweep() == 0
    later = time.time() + sp._UNRECLAIMABLE_LOOKUP_MAX_AGE_SECS + DAY

    ManualClock(later).install(monkeypatch, sp)
    assert _sweep() == 1

    assert not sp._agent_dir("expired").exists()


def test_a_run_that_left_by_another_route_is_forgotten(cleanup_calls):
    import shutil

    _seed_delivered("gone", NO_ROUTE)
    assert _sweep() == 0
    assert "gone" in sp._UNRECLAIMABLE_MEMO

    shutil.rmtree(sp._agent_dir("gone"))
    assert _sweep() == 0

    assert "gone" not in sp._UNRECLAIMABLE_MEMO


def test_a_codex_identity_has_a_route_only_with_its_recorded_store():
    """The answer the sweep remembers is the one the cleanup itself gives."""
    from kiro_crew.acp.types import PROVIDER_LABEL_CODEX

    assert sp._identity_cleanup_has_route(PROVIDER_LABEL_CODEX, "") is False
    assert sp._cleanup_session_files_sync("sid-x", PROVIDER_LABEL_CODEX) is False
    assert sp._identity_cleanup_has_route(PROVIDER_LABEL_CODEX, "/recorded/codex-home") is True


def test_an_unchanged_codex_run_with_no_recorded_store_is_evaluated_once(cleanup_calls):
    """A Codex run recorded before its store was recorded cannot be cleaned by a retry."""
    from kiro_crew.acp.types import PROVIDER_LABEL_CODEX

    _seed_delivered("codex-no-store", PROVIDER_LABEL_CODEX)

    assert _sweep() == 0
    assert _sweep() == 0

    assert cleanup_calls == [
        ("sid-codex-no-store", PROVIDER_LABEL_CODEX)
    ], "the run was evaluated again"
    assert sp._agent_dir("codex-no-store").exists()
