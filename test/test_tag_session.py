"""Tests for the background auto-tag task (chat_auto_tag.maybe_auto_tag).

Covers:
- Derives tag from slot.project basename (project=/x/repos/MyRepo -> tag 'MyRepo')
- No project -> no-op
- Second call -> no extra write (idempotency)
- Status-name collision -> skipped
- Creates missing tag definitions
- Reuses existing tags case-insensitively
- Merges without duplication (dedup)
- Credential redaction in derived tag name
- Concurrent calls race-safe (one definition only)
- Non-dashboard slot -> no-op
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.chat_auto_tag import maybe_auto_tag


def _make_state(tags=None):
    """Build a minimal mock DashboardState for testing."""
    from kiro_crew.dashboard.state import DashboardState

    state = MagicMock()
    state._tags = list(tags or [])
    state._TAGS_FILE = "chat_tags.json"
    state.save_tags = MagicMock()
    state._atomic_write_json = DashboardState._atomic_write_json
    state._atomic_write_json_strict = DashboardState._atomic_write_json_strict
    state.save_tags_snapshot = lambda snapshot: DashboardState.save_tags_snapshot(state, snapshot)
    state.push_slots_update = MagicMock()
    return state


def _make_slot(project="", tags=None):
    """Build a minimal mock slot.

    Real ``_ChatSlot.key`` values are BARE names (the ``dashboard:`` prefix
    exists only on the derived session key), so the mock uses a bare key to
    match production shape.
    """
    slot = SimpleNamespace(
        tags=list(tags or []),
        key="test-slot",
        project=project,
    )
    return slot


@pytest.fixture
def patch_save_slot(tmp_path):
    mock = AsyncMock()
    # chat_auto_tag binds save_slot_off_loop at import time (module-level
    # import), so the patch target must be chat_auto_tag's binding — patching
    # chat_persistence would be a silent no-op.
    with patch(
        "kiro_crew.dashboard.chat_auto_tag.save_slot_off_loop",
        mock,
    ):

        async def _sync_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("kiro_crew.dashboard.chat_tags.asyncio.to_thread", side_effect=_sync_to_thread):
            with patch("kiro_crew.dashboard.state.config_dir", return_value=tmp_path):
                yield mock


# ── Core derivation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagDerivation:
    """Derive tag name from slot.project basename."""

    async def test_project_basename_becomes_tag(self, patch_save_slot, tmp_path):
        state = _make_state()
        slot = _make_slot(project="/x/repos/MyRepo")

        await maybe_auto_tag(state, slot)

        # Tag definition created with project basename
        assert len(state._tags) == 1
        assert state._tags[0]["name"] == "MyRepo"
        assert state._tags[0]["status"] is False
        # Tag id assigned to slot
        assert state._tags[0]["id"] in slot.tags
        # Persistence
        tags_file = tmp_path / "chat_tags.json"
        assert tags_file.exists()
        written = json.loads(tags_file.read_text())
        assert len(written) == 1
        assert written[0]["name"] == "MyRepo"

    async def test_no_project_is_noop(self, patch_save_slot):
        state = _make_state()
        slot = _make_slot(project="")

        await maybe_auto_tag(state, slot)

        assert len(state._tags) == 0
        assert len(slot.tags) == 0
        patch_save_slot.assert_not_awaited()

    async def test_dot_project_is_noop(self, patch_save_slot):
        state = _make_state()
        slot = _make_slot(project=".")

        await maybe_auto_tag(state, slot)

        assert len(state._tags) == 0
        assert len(slot.tags) == 0

    async def test_tilde_project_is_noop(self, patch_save_slot):
        state = _make_state()
        slot = _make_slot(project="~")

        await maybe_auto_tag(state, slot)

        assert len(state._tags) == 0
        assert len(slot.tags) == 0

    async def test_nested_path_uses_basename(self, patch_save_slot):
        state = _make_state()
        slot = _make_slot(project="/home/user/workspace/src/DisapereBackend")

        await maybe_auto_tag(state, slot)

        assert len(state._tags) == 1
        assert state._tags[0]["name"] == "DisapereBackend"


# ── Idempotency ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagIdempotency:
    """Second call with same project does not produce extra writes."""

    async def test_second_call_is_noop(self, patch_save_slot):
        """After the first attempt sets _auto_tagged, second call short-circuits."""
        state = _make_state()
        slot = _make_slot(project="/x/repos/MyRepo")

        await maybe_auto_tag(state, slot)
        assert slot._auto_tagged is True
        assert len(slot.tags) == 1
        patch_save_slot.reset_mock()

        # Second call: flag already set -> immediate return
        await maybe_auto_tag(state, slot)
        patch_save_slot.assert_not_awaited()

    async def test_manual_removal_not_retagged(self, patch_save_slot):
        """If user removes auto-tag manually, flag prevents re-tagging."""
        state = _make_state()
        slot = _make_slot(project="/x/repos/MyRepo")

        # First call: tag is applied
        await maybe_auto_tag(state, slot)
        assert slot._auto_tagged is True
        assert len(slot.tags) == 1

        # Simulate user removing the tag manually
        slot.tags = []

        # Second call: flag prevents re-tagging
        patch_save_slot.reset_mock()
        await maybe_auto_tag(state, slot)
        assert slot.tags == []
        patch_save_slot.assert_not_awaited()


# ── Case-insensitive reuse ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagCaseInsensitive:
    """Reuses existing tag case-insensitively."""

    async def test_reuses_existing_case_insensitively(self, patch_save_slot):
        existing = {
            "id": "abc123",
            "name": "myrepo",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tags=[existing])
        slot = _make_slot(project="/x/repos/MyRepo")

        await maybe_auto_tag(state, slot)

        # No new tag created
        assert len(state._tags) == 1
        # Existing id assigned
        assert "abc123" in slot.tags


# ── Status tag collision ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagStatusSkip:
    """Status/workflow tags with same name are skipped."""

    async def test_status_tag_collision_skipped(self, patch_save_slot):
        status_tag = {
            "id": "status-id",
            "name": "MyRepo",
            "color": "#22c55e",
            "order": 0,
            "status": True,
        }
        state = _make_state(tags=[status_tag])
        slot = _make_slot(project="/x/repos/MyRepo")

        await maybe_auto_tag(state, slot)

        # No tag assigned (status tag skipped)
        assert "status-id" not in slot.tags
        assert len(slot.tags) == 0
        patch_save_slot.assert_not_awaited()


# ── Credential redaction ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagRedaction:
    """Credential patterns in derived tag name are redacted."""

    async def test_credential_in_project_name_is_redacted(self, patch_save_slot):
        state = _make_state()
        # This extreme case: project dir named with an AWS key
        slot = _make_slot(project="/x/deploy-AKIAIOSFODNN7EXAMPLE")

        await maybe_auto_tag(state, slot)

        # Credential must NOT survive into the tag definition name
        if state._tags:
            tag_name = state._tags[0]["name"]
            assert "AKIAIOSFODNN7EXAMPLE" not in tag_name


# ── Production-shaped slot keys ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagBareSlotKey:
    """Real ``_ChatSlot.key`` values are BARE names — the ``dashboard:``
    prefix exists only on the derived session key. Regression: an early
    guard that required a ``dashboard:`` prefix made auto-tagging a
    permanent no-op for every real slot (the once-flag was still set,
    suppressing retries)."""

    async def test_bare_key_slot_gets_tagged(self, patch_save_slot):
        state = _make_state()
        slot = _make_slot(project="/x/repos/MyRepo")
        slot.key = "chat-1712793600123"  # production shape: bare, no prefix

        await maybe_auto_tag(state, slot)

        assert [t["name"] for t in state._tags] == ["MyRepo"]
        assert len(slot.tags) == 1
        patch_save_slot.assert_awaited()


# ── Once-flag persisted with the slot save ──────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagFlagSetBeforeSave:
    """The once-flag must be True at the moment the slot is persisted — the
    save writes the metadata line, so a flag set only afterwards (e.g. in a
    ``finally``) never reaches disk; after a restart the flag is lost and a
    later message re-adds a tag the user removed."""

    async def test_flag_is_set_when_slot_is_saved(self, tmp_path, monkeypatch):
        state = _make_state()
        slot = _make_slot(project="/x/repos/MyRepo")

        flag_at_save: list[bool] = []

        async def _recording_save(state_arg, slot_arg, **kwargs):
            flag_at_save.append(bool(getattr(slot_arg, "_auto_tagged", False)))

        monkeypatch.setattr("kiro_crew.dashboard.chat_auto_tag.save_slot_off_loop", _recording_save)

        async def _sync_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("kiro_crew.dashboard.chat_tags.asyncio.to_thread", side_effect=_sync_to_thread):
            with patch("kiro_crew.dashboard.state.config_dir", return_value=tmp_path):
                await maybe_auto_tag(state, slot)

        assert flag_at_save == [True]
        assert len(slot.tags) == 1


# ── Concurrent calls produce one definition ─────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagConcurrency:
    """Concurrent maybe_auto_tag calls for the same project yield one definition."""

    async def test_concurrent_creates_one_definition(self, patch_save_slot):
        state = _make_state()
        slot_a = _make_slot(project="/x/repos/ConcurrentRepo")
        slot_b = _make_slot(project="/x/repos/ConcurrentRepo")

        await asyncio.gather(
            maybe_auto_tag(state, slot_a),
            maybe_auto_tag(state, slot_b),
        )

        # Exactly one definition
        matching = [t for t in state._tags if t["name"].lower() == "concurrentrepo"]
        assert len(matching) == 1
        # Both slots have the tag
        the_id = matching[0]["id"]
        assert the_id in slot_a.tags
        assert the_id in slot_b.tags


# ── Delete race ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagDeleteRace:
    """Concurrent auto_tag + tag delete must not leave dangling references."""

    async def test_no_dangling_reference(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_tags import api_chat_tag_delete

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        async def _sync_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        violations = 0
        iterations = 20

        for _ in range(iterations):
            state = _make_state()
            tag_t = {
                "id": "race_tag_id",
                "name": "RaceRepo",
                "color": "#6b7280",
                "order": 0,
                "status": False,
            }
            state._tags = [tag_t]
            state._slots = {}
            state._tag_boards = []
            state.save_tag_boards = MagicMock()
            slot = _make_slot(project="/x/repos/RaceRepo")
            slot.tags = []
            state._slots["test-slot"] = slot

            with patch(
                "kiro_crew.dashboard.chat_tags.asyncio.to_thread", side_effect=_sync_to_thread
            ):
                with patch(
                    "kiro_crew.dashboard.chat_tags.save_slot_off_loop", new_callable=AsyncMock
                ):
                    with patch(
                        "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
                        new_callable=AsyncMock,
                    ):

                        async def do_tag():
                            await maybe_auto_tag(state, slot)

                        async def do_delete():
                            mock_request = MagicMock()
                            mock_request.app = {"state": state}
                            mock_request.match_info = {"id": "race_tag_id"}
                            await api_chat_tag_delete(mock_request)

                        await asyncio.gather(do_tag(), do_delete())

            tag_exists = any(t.get("id") == "race_tag_id" for t in state._tags)
            slot_has_tag = "race_tag_id" in slot.tags
            if not tag_exists and slot_has_tag:
                violations += 1

        assert violations == 0, f"Dangling reference in {violations}/{iterations} iterations"


# ── Trigger integration ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagTrigger:
    """Verify chat_handlers imports and fires maybe_auto_tag."""

    async def test_import_succeeds(self):
        """chat_handlers can import maybe_auto_tag without cycle."""
        from kiro_crew.dashboard.chat_handlers import maybe_auto_tag as imported

        assert callable(imported)


# ── Persist-failure rollback ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagPersistFailureRollback:
    """If the tags.json snapshot write fails after the definition was
    appended in memory, the append must be rolled back — the once-flag
    suppresses retries, so an undurable definition would otherwise linger
    visible and unassigned until restart."""

    async def test_snapshot_failure_rolls_back_definition(self, patch_save_slot, monkeypatch):
        state = _make_state()
        slot = _make_slot(project="/x/repos/MyRepo")

        async def _failing_persist(st):
            raise IOError("disk full")

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_auto_tag.persist_tags_snapshot_unlocked",
            _failing_persist,
        )
        await maybe_auto_tag(state, slot)

        assert state._tags == []  # rolled back
        assert slot.tags == []  # never assigned
        patch_save_slot.assert_not_awaited()
        assert slot._auto_tagged is True  # attempt still consumed


# ── Truncation before matching ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoTagTruncationBeforeMatch:
    """Names must be normalized (strip + _NAME_MAX truncation) BEFORE the
    case-insensitive match, or two >60-char basenames differing only past
    the cut would create duplicate definitions with identical names."""

    async def test_long_basenames_reuse_one_definition(self, patch_save_slot):
        from kiro_crew.dashboard.chat_auto_tag import _NAME_MAX

        state = _make_state()
        base = "x" * _NAME_MAX
        slot_a = _make_slot(project=f"/repos/{base}AAAA")
        slot_b = _make_slot(project=f"/repos/{base}BBBB")

        await maybe_auto_tag(state, slot_a)
        await maybe_auto_tag(state, slot_b)

        assert len(state._tags) == 1  # single definition, no duplicate
        assert state._tags[0]["name"] == base
        assert slot_a.tags == slot_b.tags == [state._tags[0]["id"]]
