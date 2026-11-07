"""How often one stale-tombstone prune sweep reads and locks down cleanup records.

Every count here is a call count on the product's own seam, never wall time.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.subagent_persistence as sp
import kiro_crew.subagent_persistence_layout as layout

_AGENTS = 200


def _seed_prunable(agent_id: str, *, died: float = 1) -> None:
    """A delivered run past its window, with one protected cleanup generation."""
    sp.create_agent_folder(agent_id, task=agent_id)
    sp.update_state(agent_id, session_id=f"sid-{agent_id}", provider="acp", keep=False)
    sp.remember_live_cleanup_identity(
        agent_id, session_id=f"sid-{agent_id}", provider="acp", keep=False
    )
    sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
    ts_path = sp._agent_dir(agent_id) / "tombstone.json"
    tombstone = json.loads(ts_path.read_text())
    tombstone["died"] = died
    ts_path.write_text(json.dumps(tombstone))


def _forget_live_identities() -> None:
    # A restarted gateway: only the protected records remain.
    with sp._CLEANUP_IDENTITY_LOCK:
        sp._LIVE_CLEANUP_IDENTITIES.clear()


def _shared_trust_dirs() -> tuple[Path, Path]:
    records_root = sp._subagents_dir().parent / "trust" / sp._CLEANUP_IDENTITIES_TRUST_DIR
    return records_root.parent, records_root


class TestPruneSweepReadCounts:
    def test_one_sweep_reads_each_record_once_and_locks_shared_parents_once(
        self, tmp_path: Path
    ) -> None:
        agent_ids = [f"sweep-{n:03d}" for n in range(_AGENTS)]
        for agent_id in agent_ids:
            _seed_prunable(agent_id)
        _forget_live_identities()

        parsed: Counter[str] = Counter()
        real_parse = layout._parse_cleanup_identities_file

        def counting_parse(path: Path) -> list[dict[str, object]]:
            parsed[path.parent.name] += 1
            return real_parse(path)

        restricted: Counter[Path] = Counter()
        real_restrict = sp.platform_compat.restrict_dir_to_owner

        def counting_restrict(path: Path) -> None:
            restricted[Path(path)] += 1
            real_restrict(path)

        with patch.object(sp.platform_compat, "restrict_dir_to_owner", counting_restrict):
            # ``make_owner_only_dir`` makes its own best-effort call, so one
            # lockdown is measured rather than assumed to be one call.
            probe = tmp_path / "one-lockdown"
            layout._lock_down_cleanup_dir(probe)
            per_lockdown = restricted.pop(probe)
            with (
                patch.object(sp, "_parse_cleanup_identities_file", counting_parse),
                patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup,
            ):
                pruned = sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0)

        assert pruned == _AGENTS
        assert cleanup.call_count == _AGENTS
        assert set(parsed) == set(agent_ids)
        assert max(parsed.values()) == 1
        trust_root, records_root = _shared_trust_dirs()
        assert per_lockdown >= 1
        assert restricted[trust_root] == per_lockdown
        assert restricted[records_root] == per_lockdown
        # Each agent's own record directory is still locked down before its read.
        per_agent = {p.name: n for p, n in restricted.items() if p.parent == records_root}
        assert per_agent == {agent_id: per_lockdown for agent_id in agent_ids}
        assert sum(restricted.values()) == (_AGENTS + 2) * per_lockdown

    def test_a_second_sweep_reads_the_records_again(self) -> None:
        # Nothing carries over between sweeps: a corrupted record seen by the
        # second sweep must stop that sweep, whatever the first one read.
        # flake-ok: back-dated a second on the live clock, inside the retry window
        _seed_prunable("kept-across-sweeps", died=time.time() - 1)
        _forget_live_identities()
        with patch.object(sp, "_cleanup_session_files_sync", return_value=False):
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
        record = sp._cleanup_identities_path("kept-across-sweeps")
        record.write_text(json.dumps({"identities": [{"session_id": ""}]}))

        with patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0

        cleanup.assert_not_called()
        assert sp._agent_dir("kept-across-sweeps").exists()


class TestPruneSweepFailsClosed:
    @pytest.mark.parametrize(
        "payload",
        [
            "{malformed",
            json.dumps({"identities": "not-a-list"}),
            json.dumps({"identities": [{"session_id": ""}]}),
            json.dumps({"identities": [{"session_id": "sid-x", "keep": "yes"}]}),
        ],
    )
    def test_invalid_record_keeps_that_run_and_prunes_the_rest(self, payload: str) -> None:
        bad_id, good_id = "a-invalid-record", "z-valid-record"
        for agent_id in (bad_id, good_id):
            _seed_prunable(agent_id)
        _forget_live_identities()
        bad_record = sp._cleanup_identities_path(bad_id)
        bad_record.write_text(payload)

        with patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        cleanup.assert_called_once_with(f"sid-{good_id}", "acp", cwd="")
        assert sp._agent_dir(bad_id).exists()
        assert bad_record.read_text() == payload
        assert not sp._agent_dir(good_id).exists()

    def test_failed_shared_lockdown_reads_nothing_and_is_retried(self) -> None:
        first_id, second_id = "a-lockdown-refused", "z-lockdown-retried"
        for agent_id in (first_id, second_id):
            _seed_prunable(agent_id)
        _forget_live_identities()
        trust_root, _ = _shared_trust_dirs()
        real_lock_down = layout._lock_down_cleanup_dir
        trust_root_attempts = 0

        def refuse_first(path: Path) -> None:
            nonlocal trust_root_attempts
            if path == trust_root:
                trust_root_attempts += 1
                if trust_root_attempts == 1:
                    raise OSError("DACL refused")
            real_lock_down(path)

        parsed: list[str] = []
        real_parse = layout._parse_cleanup_identities_file

        def recording_parse(path: Path) -> list[dict[str, object]]:
            parsed.append(path.parent.name)
            return real_parse(path)

        with (
            patch.object(sp, "_lock_down_cleanup_dir", refuse_first),
            patch.object(sp, "_parse_cleanup_identities_file", recording_parse),
            patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup,
        ):
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        # The refused run's record was never read and the run is untouched. The
        # next run retried the shared lockdown instead of trusting a sweep-wide
        # flag that the failure never set.
        assert trust_root_attempts == 2
        assert parsed == [second_id]
        cleanup.assert_called_once_with(f"sid-{second_id}", "acp", cwd="")
        assert sp._agent_dir(first_id).exists()
        assert sp._cleanup_identities_path(first_id).exists()
        assert not sp._agent_dir(second_id).exists()

    def test_generation_published_after_the_read_is_still_cleaned(self) -> None:
        agent_id = "late-generation"
        _seed_prunable(agent_id)
        _forget_live_identities()
        real_read = layout._CleanupIdentitySweepReader.read

        def read_then_publish(
            reader: layout._CleanupIdentitySweepReader, run_id: str
        ) -> list[dict[str, object]]:
            records = real_read(reader, run_id)
            # A generation lands in the live map before its durable write, so
            # one published after the sweep's only read must still be cleaned.
            sp.publish_live_cleanup_identity(run_id, session_id="sid-late", provider="acp")
            return records

        with (
            patch.object(layout._CleanupIdentitySweepReader, "read", read_then_publish),
            patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup,
        ):
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        cleaned = [c.args[0] for c in cleanup.call_args_list]
        assert cleaned == [f"sid-{agent_id}", "sid-late"]

    @pytest.mark.parametrize("promoted_after_read", [False, True])
    def test_live_promotion_after_the_read_outranks_the_stale_record(
        self, promoted_after_read: bool
    ) -> None:
        # Unreadable state leaves the protected record's ``keep`` as the only
        # retention signal. The durable record says False; a live True
        # published after the sweep's read must still keep the run.
        agent_id = "promoted-after-read"
        _seed_prunable(agent_id)
        _forget_live_identities()
        (sp._agent_dir(agent_id) / "state.json").write_text("{corrupt")
        real_read = layout._CleanupIdentitySweepReader.read

        def read_then_promote(
            reader: layout._CleanupIdentitySweepReader, run_id: str
        ) -> list[dict[str, object]]:
            records = real_read(reader, run_id)
            if promoted_after_read:
                sp.publish_live_cleanup_identity(
                    run_id, session_id=f"sid-{run_id}", provider="acp", keep=True
                )
            return records

        with (
            patch.object(layout._CleanupIdentitySweepReader, "read", read_then_promote),
            patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup,
        ):
            pruned = sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0)

        if promoted_after_read:
            assert pruned == 0
            cleanup.assert_not_called()
            assert sp._agent_dir(agent_id).exists()
        else:
            assert pruned == 1
            cleanup.assert_called_once_with(f"sid-{agent_id}", "acp", cwd="")
            assert not sp._agent_dir(agent_id).exists()

    def test_folder_deleted_after_the_read_is_not_cleaned_from_the_snapshot(self) -> None:
        agent_id = "deleted-after-read"
        _seed_prunable(agent_id)
        _forget_live_identities()
        real_read = layout._CleanupIdentitySweepReader.read

        def read_then_delete(
            reader: layout._CleanupIdentitySweepReader, run_id: str
        ) -> list[dict[str, object]]:
            records = real_read(reader, run_id)
            sp.delete_agent_folder(run_id)
            return records

        with (
            patch.object(layout._CleanupIdentitySweepReader, "read", read_then_delete),
            patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup,
        ):
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0

        cleanup.assert_not_called()
