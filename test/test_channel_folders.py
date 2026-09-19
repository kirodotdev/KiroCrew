"""Tests for per-channel session filing (``<channel>.session_folder``).

Covers the config read, folder resolution (create / adopt / unhide / off), the
request validator every channel save endpoint shares, and the filing decision
made when a channel conversation is surfaced as a dashboard slot.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
import threading
from typing import Any

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import config_path
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard import channel_folders, channel_slots, chat_folders
from kiro_crew.dashboard.chat_utils import effective_session_key


@pytest.fixture
def dashboard_state(tmp_path: Any) -> Any:
    return _make_state(tmp_path)


def _write_config(section: str, folder: str) -> None:
    """Point ``<section>.session_folder`` at *folder* in the test's config.json."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({section: {"session_folder": folder}}), encoding="utf-8")


class TestConfiguredFolderName:
    def test_off_by_default(self) -> None:
        """No config at all means every channel is off — the shipped default."""
        for ns in channel_folders.CHANNEL_CONFIG_SECTIONS:
            assert channel_folders.configured_folder_name(ns) == ""

    def test_reads_the_channel_section(self) -> None:
        _write_config("discord", "Discord")
        assert channel_folders.configured_folder_name("discord") == "Discord"
        # A sibling channel is unaffected: the setting is per channel.
        assert channel_folders.configured_folder_name("slack") == ""

    def test_namespaces_without_a_config_section_are_always_off(self) -> None:
        """``unified`` spans channels and ``whatsapp`` has no section — both off."""
        assert channel_folders.configured_folder_name("unified") == ""
        assert channel_folders.configured_folder_name("whatsapp") == ""
        assert channel_folders.configured_folder_name("") == ""

    def test_an_overlong_configured_name_disables_filing(self) -> None:
        """A hand-edited over-long name is refused, NOT truncated.

        Truncating would file conversations into a real folder whose name nobody
        chose; leaving them unfiled is the pre-feature behaviour.
        """
        _write_config("discord", "x" * (channel_folders.SESSION_FOLDER_NAME_MAX + 1))
        assert channel_folders.configured_folder_name("discord") == ""

    def test_a_configured_name_with_a_path_separator_disables_filing(self) -> None:
        _write_config("discord", "Work/Discord")
        assert channel_folders.configured_folder_name("discord") == ""

    def test_config_read_failure_is_off_not_an_error(self, monkeypatch: Any) -> None:
        """A broken config read leaves sessions unfiled rather than raising."""

        def boom() -> Any:
            raise OSError("config unreadable")

        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(boom))
        assert channel_folders.configured_folder_name("discord") == ""


class TestLookupChannelFolder:
    """The reconcile path: read-only, never writes the folder store."""

    def test_returns_empty_when_off(self, dashboard_state: Any) -> None:
        assert asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord")) == ""
        assert dashboard_state._folders == []

    def test_finds_the_configured_folder(self, dashboard_state: Any) -> None:
        _write_config("discord", "Discord")
        dashboard_state._folders.append(
            {"id": "f1", "name": "Discord", "order": 0, "parent_id": "", "channel": "discord"}
        )
        assert (
            asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord")) == "f1"
        )

    def test_creates_nothing_when_the_folder_is_missing(self, dashboard_state: Any) -> None:
        """Configured but absent (hand-edited config, or the user deleted it).

        The conversation stays unfiled rather than the reconcile path writing the
        folder store: that write would block the event loop on fsync and, being
        reachable twice concurrently, could drop a parallel folder edit. Creation
        happens on the settings save instead.
        """
        _write_config("discord", "Discord")
        assert asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord")) == ""
        assert dashboard_state._folders == []

    def test_adopts_an_existing_folder_by_name(self, dashboard_state: Any) -> None:
        """Pointing a channel at a folder the user already made reuses it."""
        _write_config("discord", "chats")
        dashboard_state._folders.append(
            {"id": "user1", "name": "Chats", "order": 0, "parent_id": ""}
        )
        assert (
            asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord"))
            == "user1"
        )
        assert len(dashboard_state._folders) == 1

    def test_prefers_the_channel_stamped_folder_on_a_name_tie(self, dashboard_state: Any) -> None:
        _write_config("discord", "Discord")
        dashboard_state._folders.extend(
            [
                {"id": "other", "name": "discord", "order": 0, "parent_id": ""},
                {
                    "id": "mine",
                    "name": "Discord",
                    "order": 1,
                    "parent_id": "",
                    "channel": "discord",
                },
            ]
        )
        assert (
            asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord")) == "mine"
        )

    def test_returns_a_hidden_folder_without_writing(self, dashboard_state: Any) -> None:
        """No unhide write is needed on this path.

        ``folderIsHidden`` in the sidebar is ``hidden && !hasActiveSession``, so a
        hidden folder that receives a session shows up on its own.
        """
        _write_config("discord", "Discord")
        dashboard_state._folders.append(
            {"id": "f1", "name": "Discord", "order": 0, "parent_id": "", "hidden": True}
        )
        writes: list[Any] = []
        dashboard_state.save_folders = lambda: writes.append(1)  # type: ignore[method-assign]

        assert (
            asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord")) == "f1"
        )
        assert not writes, "the reconcile path must not write the folder store"
        assert dashboard_state._folders[0]["hidden"] is True

    def test_the_config_read_runs_off_the_event_loop(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """Reads cannot clobber, so the config read is safe to offload."""
        _write_config("discord", "Discord")
        dashboard_state._folders.append({"id": "f1", "name": "Discord", "order": 0})
        loop_thread = threading.get_ident()
        read_threads: list[int] = []
        real = channel_folders.configured_folder_name

        def recording_read(namespace: str) -> str:
            read_threads.append(threading.get_ident())
            return real(namespace)

        monkeypatch.setattr(channel_folders, "configured_folder_name", recording_read)

        assert asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord"))
        assert read_threads and all(t != loop_thread for t in read_threads)

    def test_the_reconcile_path_has_no_folder_store_write(self) -> None:
        """Structural guard: no ``save_folders`` call anywhere in the lookup.

        The whole point of splitting creation out is that this path cannot write.
        Asserted on the AST rather than by observation so a future edit that
        reintroduces a write fails here instead of in production, where the
        symptom is a stalled event loop or a dropped folder edit.
        """
        source = inspect.getsource(channel_folders.lookup_channel_folder)
        tree = ast.parse(textwrap.dedent(source))
        writers = [
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"save_folders", "_atomic_write_json"}
        ]
        assert not writers, (
            f"lookup_channel_folder writes the folder store ({writers}); the "
            "reconcile path must stay read-only — creation belongs to "
            "ensure_channel_folder, called from the config save endpoints."
        )


class TestEnsureChannelFolder:
    """The settings-save path: creates or adopts, verifies, never on reconcile."""

    def test_creates_the_folder_stamped_with_the_channel(self, dashboard_state: Any) -> None:
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        assert fid
        (folder,) = dashboard_state._folders
        assert folder["id"] == fid
        assert folder["name"] == "Discord"
        # The stamp is what makes the sidebar draw the brand mark on it.
        assert folder["channel"] == "discord"
        # No emoji icon: the brand mark is this folder's icon.
        assert "icon" not in folder
        assert (
            json.loads((config_dir() / dashboard_state._FOLDERS_FILE).read_text(encoding="utf-8"))[
                0
            ]["id"]
            == fid
        )

    def test_is_idempotent(self, dashboard_state: Any) -> None:
        first = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        second = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        assert first == second
        assert len(dashboard_state._folders) == 1

    def test_empty_name_creates_nothing(self, dashboard_state: Any) -> None:
        assert (
            asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "")) == ""
        )
        assert dashboard_state._folders == []

    def test_adopts_and_unhides_an_existing_folder(self, dashboard_state: Any) -> None:
        dashboard_state._folders.append(
            {"id": "f1", "name": "Discord", "order": 0, "parent_id": "", "hidden": True}
        )
        assert (
            asyncio.run(
                channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
            )
            == "f1"
        )
        assert dashboard_state._folders[0]["hidden"] is False
        assert len(dashboard_state._folders) == 1

    def test_unpersistable_folder_is_not_kept_in_memory(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """A folder whose write RAISES is dropped, not left to duplicate."""

        def boom(path: Any, data: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(dashboard_state, "_atomic_write_json", boom)
        assert (
            asyncio.run(
                channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
            )
            == ""
        )
        assert dashboard_state._folders == []

    def test_a_silently_failed_write_is_not_treated_as_persisted(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """The real failure mode: the write helper logs and swallows the error.

        ``DashboardState._atomic_write_json`` never raises, so a read-only or
        full disk returns normally. Handing a session that folder id anyway would
        leave it pointing at a folder that is gone after the next restart, so
        persistence is verified by reading the store back.
        """
        monkeypatch.setattr(
            dashboard_state, "_atomic_write_json", lambda path, data: None  # writes nothing
        )
        assert (
            asyncio.run(
                channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
            )
            == ""
        )
        assert dashboard_state._folders == []

    def test_a_write_that_lands_stale_content_is_not_treated_as_persisted(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """A store that parses but lacks the folder is not proof either.

        Distinct from the swallowed-error case above, where nothing is written at
        all: here the write "succeeds" and leaves a perfectly valid folder store
        that simply does not contain the new folder (a partial write that still
        parses, or a store another writer clobbered). Reading it back is only
        evidence if the ids are actually compared.
        """
        path = config_dir() / dashboard_state._FOLDERS_FILE

        def stale_write(p: Any, data: Any) -> None:
            path.write_text("[]", encoding="utf-8")  # valid, but not what we asked for

        monkeypatch.setattr(dashboard_state, "_atomic_write_json", stale_write)
        assert (
            asyncio.run(
                channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
            )
            == ""
        )
        assert dashboard_state._folders == []

    def test_a_lookup_cannot_observe_an_uncommitted_create(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """A concurrent lookup must not see a folder whose write then fails.

        ``ensure_channel_folder`` mutates the in-memory list and only then
        persists, so between those two steps the folder is visible to anything
        reading the store WITHOUT the lock. If the write then fails and the
        folder is rolled back, a lookup that read in that window has already
        handed a session a ``folder_id`` that dangles — and because slot-side
        folder metadata marks a session as already filed, no later save corrects
        it.

        The window is opened deliberately rather than raced for: the write is
        PARKED inside its worker thread, the loop is pumped so the lookup runs as
        far as it can, and only then does the write fail.
        """
        monkeypatch.setattr(channel_folders, "configured_folder_name", lambda ns: "Discord")
        in_write = threading.Event()
        release = threading.Event()

        def failing_write(path: Any, data: Any) -> None:
            in_write.set()
            release.wait(timeout=5)
            raise OSError("disk full")

        monkeypatch.setattr(dashboard_state, "_atomic_write_json", failing_write)

        async def _run() -> str:
            ensure = asyncio.create_task(
                channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
            )
            await asyncio.to_thread(in_write.wait, 5)
            # The folder IS in memory now; only the persist is outstanding.
            assert any(f["name"] == "Discord" for f in dashboard_state._folders)
            lookup = asyncio.create_task(
                channel_folders.lookup_channel_folder(dashboard_state, "discord")
            )
            for _ in range(50):  # pump: let the lookup get as far as it can
                await asyncio.sleep(0)
            release.set()
            found, created = await asyncio.gather(lookup, ensure)
            assert created == ""
            return str(found)

        assert asyncio.run(_run()) == "", (
            "a lookup returned a folder id while its write was still unconfirmed; "
            "the reconcile path must read the store under the lock"
        )
        assert dashboard_state._folders == []

    def test_the_create_runs_inside_one_store_transaction(self) -> None:
        """Structural guard: find + create + persist happen in ONE transaction.

        Two callers can reach this at once (two browser tabs saving two channels'
        settings), and a duplicate folder or a dropped folder edit is the failure
        mode. Atomicity comes from ``DashboardState.mutate_folders`` holding the
        store lock across the whole read-modify-write — so the guard is that the
        folder work goes through that primitive and never writes the store
        directly.

        Asserted structurally rather than by racing two tasks: the interleaving
        depends on when the thread pool hands each coroutine back, so a timing
        test passes even when the invariant is broken (verified in an earlier
        round — injecting a yield did not fail such a test). The AST cannot be
        fooled that way.
        """
        source = inspect.getsource(channel_folders.ensure_channel_folder)
        tree = ast.parse(textwrap.dedent(source))

        called = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "mutate_folders" in called, (
            "ensure_channel_folder must persist through state.mutate_folders so the "
            "find and the create are atomic against a concurrent folder edit."
        )
        forbidden = called & {"save_folders", "_atomic_write_json"}
        assert not forbidden, (
            f"ensure_channel_folder writes the folder store directly ({forbidden}); "
            "every write must go through mutate_folders, which serializes the "
            "read-modify-write and moves the fsync off the event loop."
        )

    def test_no_folder_module_writes_the_store_directly(self) -> None:
        """The whole point of option 3: one writer, six callers, no bypass.

        A direct ``save_folders()`` anywhere in the folder-write path reintroduces
        both defects at once — an fsync on the event loop, and a write that is
        not serialized against the others.
        """
        offenders: list[str] = []
        for mod in (channel_folders, chat_folders):
            tree = ast.parse(inspect.getsource(mod))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"save_folders", "_atomic_write_json"}
                ):
                    offenders.append(f"{mod.__name__}:{node.lineno} {node.func.attr}")
        assert not offenders, (
            f"direct folder-store writes found ({offenders}); route them through "
            "DashboardState.mutate_folders."
        )


class TestStoredFolderName:
    """The save endpoints read session_folder back from the RAW config dict.

    That path bypasses the loader's coercion, so it needs its own fail-closed
    reader: a hand-edited non-string must not become a folder name when an
    unrelated field in the same section is saved.
    """

    def test_a_real_name_passes_through(self) -> None:
        assert channel_folders.stored_folder_name("  Discord  ") == "Discord"

    def test_absent_or_empty_is_off(self) -> None:
        assert channel_folders.stored_folder_name(None) == ""
        assert channel_folders.stored_folder_name("") == ""
        assert channel_folders.stored_folder_name("   ") == ""

    def test_a_hand_edited_non_string_fails_closed(self) -> None:
        """`"session_folder": 123` must not create a folder named "123".

        Coercing with str() would: the value reaches ensure_channel_folder on the
        next save of any other field in that section, creating a real folder whose
        name nobody chose.
        """
        for raw in (123, 12.5, True, ["Discord"], {"name": "Discord"}, object()):
            assert channel_folders.stored_folder_name(raw) == "", raw

    def test_unusable_names_fail_closed(self) -> None:
        assert channel_folders.stored_folder_name("a/b") == ""
        assert channel_folders.stored_folder_name("a\\b") == ""
        assert channel_folders.stored_folder_name("a\nb") == ""
        assert channel_folders.stored_folder_name("x" * 500) == ""


class TestCleanSessionFolder:
    def test_accepts_and_trims_a_name(self) -> None:
        assert channel_folders.clean_session_folder("  Discord  ") == "Discord"

    def test_empty_means_off(self) -> None:
        assert channel_folders.clean_session_folder("") == ""

    @pytest.mark.parametrize(
        "raw",
        [42, None, True, ["Discord"], "a/b", "a\\b", "a\nb", "x" * 101],
    )
    def test_rejects_unusable_values(self, raw: Any) -> None:
        with pytest.raises(ValueError):
            channel_folders.clean_session_folder(raw)


class TestFilingOnSurface:
    def _info(self, key: str = "discord:kirocrew:direct:U1") -> dict[str, Any]:
        return {"key": key, "title": "", "modified": 0.0}

    @pytest.fixture(autouse=True)
    def _quiet_push(self, dashboard_state: Any) -> None:
        # Surfacing pushes a slots update, which serializes the whole state.
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

    def test_files_a_newly_surfaced_session(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state, self._info(), {}, [], folder_id="f1"
        )
        assert slot is not None
        assert slot.folder_id == "f1"
        # Filing is a one-time action, so it records that it happened.
        assert slot._channel_folder_filed is True

    def test_unfiled_when_the_channel_is_off(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(dashboard_state, self._info(), {}, [])
        assert slot is not None
        assert slot.folder_id == ""
        assert slot._channel_folder_filed is False

    def test_the_sessions_own_folder_wins(self, dashboard_state: Any) -> None:
        """A conversation the user already filed keeps where they put it."""
        slot = channel_slots.surface_channel_session(
            dashboard_state, self._info(), {"folder_id": "user-choice"}, [], folder_id="f1"
        )
        assert slot is not None
        assert slot.folder_id == "user-choice"

    def test_the_marker_is_restored_from_metadata(self, dashboard_state: Any) -> None:
        """A conversation filed in an earlier run stays marked as filed.

        This is what stops the reconciler filing it a second time after the user
        has moved it somewhere else — including to the top level, where
        ``folder_id`` is absent from the metadata line entirely.
        """
        slot = channel_slots.surface_channel_session(
            dashboard_state, self._info(), {"channel_folder_filed": True}, []
        )
        assert slot is not None
        assert slot.folder_id == ""
        assert slot._channel_folder_filed is True

    def test_first_filing_inherits_the_folders_tags(self, dashboard_state: Any) -> None:
        """A channel chat BORN into a tagged folder inherits like a dashboard chat.

        Inheritance is creation-only across the whole feature; the
        channel default-filing branch is a birth, so the caller-resolved tags
        are copied by value here and nowhere else.
        """
        slot = channel_slots.surface_channel_session(
            dashboard_state, self._info(), {}, [], folder_id="f1", folder_tags=["t1", "t2"]
        )
        assert slot is not None
        assert slot.folder_id == "f1"
        assert sorted(slot.tags) == ["t1", "t2"]

    def test_first_filing_rotates_the_tags_revision(self, dashboard_state: Any) -> None:
        """Inherited tags ship under a revision distinct from the slot's birth one."""
        from unittest.mock import patch

        from kiro_crew.dashboard.state import _ChatSlot

        birth_revisions: list[str] = []
        original_init = _ChatSlot.__init__

        def _recording_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            original_init(self, *args, **kwargs)
            birth_revisions.append(self.tags_revision)

        with patch.object(_ChatSlot, "__init__", _recording_init):
            slot = channel_slots.surface_channel_session(
                dashboard_state, self._info(), {}, [], folder_id="f1", folder_tags=["t1"]
            )
        assert slot is not None and slot.tags == ["t1"]
        assert birth_revisions and slot.tags_revision not in birth_revisions
        assert slot.tags_revision > max(birth_revisions)

    def test_restoring_a_filed_session_never_re_tags(self, dashboard_state: Any) -> None:
        """The restore branch (persisted folder_id) is not a birth — no tags.

        Even when the caller supplies folder_tags, a conversation whose
        metadata already carries a folder was filed long ago; re-stamping it
        would be the retro-tagging the creation-only rule forbids.
        """
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            self._info(),
            {"folder_id": "user-choice"},
            [],
            folder_id="f1",
            folder_tags=["t1"],
        )
        assert slot is not None
        assert slot.folder_id == "user-choice"
        assert slot.tags == []

    def test_persisted_tags_are_recovered_after_a_crash(self, dashboard_state: Any) -> None:
        """Tags stored with the filing marker are applied on restore.

        The reconcile pass persists the inherited tags in the SAME metadata
        write as the filing marker. If the process crashes before the slot's
        first save, the marker exists but the slot line does not — this read
        is what recovers the inheritance instead of losing it forever (the
        marker blocks re-inheritance by design).
        """
        dashboard_state._tags = [{"id": "t1", "name": "alpha", "color": "#123456"}]
        dashboard_state._tags_authoritative = True
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            self._info(),
            {"channel_folder_filed": True, "folder_id": "f1", "tags": ["t1"]},
            [],
        )
        assert slot is not None
        assert slot.folder_id == "f1"
        assert slot.tags == ["t1"]
        # Recovery, not re-inheritance: the filed marker is still honored.
        assert slot._channel_folder_filed is True

    def test_persisted_tags_are_validated_against_the_vocabulary(
        self, dashboard_state: Any
    ) -> None:
        """Meta tags are hardened like every other restore path.

        The metadata line is on-disk state: a non-string entry must not crash
        the surface, and an id deleted from the vocabulary since the filing
        must be dropped, not resurrected.
        """
        dashboard_state._tags = [{"id": "t1", "name": "alpha", "color": "#123456"}]
        dashboard_state._tags_authoritative = True
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            self._info(),
            {"channel_folder_filed": True, "tags": ["t1", {"bad": 1}, "ghost"]},
            [],
        )
        assert slot is not None
        assert slot.tags == ["t1"]

    def test_restore_fails_open_when_the_vocabulary_is_not_authoritative(
        self, dashboard_state: Any
    ) -> None:
        """An unreadable tags.json must not erase a filed chat's tags.

        The three sibling restore paths gate their prune on
        ``_tags_authoritative``; this path must match. Intersecting with an
        unknown (empty) vocabulary would drop every persisted id, the next
        save would write the empty list to disk, and the sticky filing marker
        would block re-inheritance forever — silent, permanent loss from one
        bad boot.
        """
        dashboard_state._tags = []
        dashboard_state._tags_authoritative = False
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            self._info(),
            {"channel_folder_filed": True, "tags": ["t1", "t2"]},
            [],
        )
        assert slot is not None
        assert slot.tags == ["t1", "t2"]


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

    def get_metadata(self, key: str) -> dict[str, Any]:
        return dict(self._meta.get(key, {}))

    def update_metadata(self, key: str, fields: dict[str, Any]) -> None:
        self._meta.setdefault(key, {}).update(fields)

    def update_metadata_if(self, key: str, fields: dict[str, Any], guard: Any) -> bool:
        """Merge only if *guard* still accepts the stored record.

        The real method evaluates the guard inside the cross-process lock, so a
        write that landed while the caller queued IS visible to it. Mirroring that
        here — guard first, against the current record — is what lets a test prove
        the reconcile pass yields to a placement made mid-pass.
        """
        if not guard(self.get_metadata(key)):
            return False
        self.update_metadata(key, fields)
        return True

    def read_messages(self, key: str) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "hello"}] if key in self._keys else []


class TestReconcilePassFiling:
    def test_reconcile_files_pending_channel_sessions(self, dashboard_state: Any) -> None:
        """The folder already exists — the settings save created it."""
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        dashboard_state.conversation_log = _FakeLog([key])
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        surfaced = asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0))
        assert surfaced == 1
        slot = dashboard_state._slots[channel_slots.channel_slot_name(key)]
        (folder,) = dashboard_state._folders
        assert folder["name"] == "Discord"
        assert slot.folder_id == fid

    def test_reconcile_leaves_the_session_unfiled_when_the_folder_is_gone(
        self, dashboard_state: Any
    ) -> None:
        """Configured but no such folder: surface unfiled, write nothing.

        Reachable by hand-editing config.json or deleting the folder after
        turning the setting on. The reconcile path never creates it — that would
        put an fsync on the event loop and race a concurrent folder edit.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        dashboard_state.conversation_log = _FakeLog([key])
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        writes: list[Any] = []
        dashboard_state.save_folders = lambda: writes.append(1)  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        assert dashboard_state._slots[channel_slots.channel_slot_name(key)].folder_id == ""
        assert dashboard_state._folders == []
        assert not writes

    def test_reconcile_creates_no_folder_when_every_channel_is_off(
        self, dashboard_state: Any
    ) -> None:
        key = "discord:kirocrew:direct:U1"
        dashboard_state.conversation_log = _FakeLog([key])
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        assert dashboard_state._folders == []
        assert dashboard_state._slots[channel_slots.channel_slot_name(key)].folder_id == ""

    def test_one_conversations_folder_is_not_applied_to_another(self, dashboard_state: Any) -> None:
        """The namespace-wide folder must not reach an already-filed conversation.

        The folder is resolved once per CHANNEL, so the same value is available to
        every pending conversation of that channel. If it were applied without
        re-testing per session, this happens: conversation A has never been filed,
        so the pass resolves folder F for "discord"; conversation B was filed
        before and the user moved it to the top level (marker set, no folder_id).
        B would then be handed F — silently returning it to the folder and
        OVERWRITING the user's placement on disk, which is the exact guarantee
        this feature claims to keep.
        """
        _write_config("discord", "Discord")
        key_a = "discord:kirocrew:direct:UA"
        key_b = "discord:kirocrew:direct:UB"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        log = _FakeLog(
            [key_a, key_b],
            # B: filed once, then moved to the top level by hand.
            {key_b: {"memory_mode": "persistent", "channel_folder_filed": True}},
        )
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 2
        slot_a = dashboard_state._slots[channel_slots.channel_slot_name(key_a)]
        slot_b = dashboard_state._slots[channel_slots.channel_slot_name(key_b)]

        assert slot_a.folder_id == fid, "the never-filed conversation should be filed"
        assert slot_b.folder_id == "", (
            "an already-filed conversation was handed another conversation's "
            "folder; the user's move to the top level was undone"
        )
        # And nothing was written back for B that would make it permanent.
        assert not log.get_metadata(key_b).get(
            "folder_id"
        ), "the re-filing was persisted, so the user's placement is lost for good"

    def test_the_placement_is_on_disk_before_the_slot_is_visible(
        self, dashboard_state: Any
    ) -> None:
        """Filing must be durable BEFORE the conversation becomes visible.

        ``get_or_create_slot`` pushes a slots update, so the instant a session is
        surfaced the user can see it and drag it elsewhere — and that move saves
        immediately. If the filing write landed after that, it would overwrite the
        move with the default folder and the next restart would put the session
        back, losing a user action. So the ordering is the guarantee: by the time
        the slot is published, the record already says where it was filed.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        log = _FakeLog([key])
        dashboard_state.conversation_log = log

        at_publish: list[dict[str, Any]] = []

        def _capture() -> None:
            at_publish.append(log.get_metadata(key))

        dashboard_state.push_slots_update = _capture  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        assert at_publish, "the slot was never published, so the ordering is untested"
        assert at_publish[0].get("folder_id") == fid, (
            "the conversation became visible before its placement was durable; a "
            "move made in that window would be overwritten by the filing write"
        )

    def test_a_placement_that_cannot_be_persisted_is_not_applied(
        self, dashboard_state: Any
    ) -> None:
        """If the record cannot be written, do not file in memory either.

        An in-memory-only placement is worse than none: it is lost on restart and
        the next pass files the conversation again, so the user sees it move on its
        own. Leave it unfiled and let a later pass retry.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord"))
        log = _FakeLog([key])

        def boom(k: str, fields: dict[str, Any]) -> None:
            raise OSError("read-only history")

        log.update_metadata = boom  # type: ignore[method-assign]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        slot = dashboard_state._slots[channel_slots.channel_slot_name(key)]
        assert slot.folder_id == "", (
            "the conversation was filed in memory only; the placement dies on "
            "restart and the next pass files it again"
        )

    def test_a_session_resumed_mid_pass_is_not_filed_over(self, dashboard_state: Any) -> None:
        """A conversation surfaced DURING the pass must not be filed by that pass.

        The pass snapshots metadata and decides what to file, then awaits a
        transcript read before writing the placement. In that window the user can
        resume the conversation from History and move it; a slot existing by the
        time the write happens is the evidence that happened, and writing anyway
        would restore the default folder after the next restart.

        The slot has to appear DURING the pass, not before it: a session whose slot
        already exists is never in ``pending``, so pre-creating it would make this
        test pass without ever reaching the re-check.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord"))
        log = _FakeLog([key])
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        # Surface the conversation from inside the transcript read — the await
        # that sits between "decide to file" and "write the placement".
        real_read = log.read_messages

        def read_then_resume(k: str) -> list[dict[str, Any]]:
            dashboard_state.get_or_create_slot(channel_slots.channel_slot_name(key))
            return real_read(k)

        log.read_messages = read_then_resume  # type: ignore[method-assign]

        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0))
        assert not log.get_metadata(key).get("folder_id"), (
            "the pass filed a conversation that was surfaced while it ran; a move "
            "made in that window is overwritten after the next restart"
        )
        assert not log.get_metadata(key).get("channel_folder_filed")

    def test_a_session_surfaced_before_filing_was_on_is_not_filed_later(
        self, dashboard_state: Any
    ) -> None:
        """Turning the feature ON must not re-place conversations already surfaced.

        The gap this closes: a conversation that first surfaced while filing was
        OFF gets neither ``folder_id`` nor ``channel_folder_filed``. The user then
        moves it into a folder and back out to the top level (which clears
        ``folder_id`` and omits the key entirely), and later enables filing. On the
        next surface after a restart, a check that only looked at those two keys
        would read "never filed" and overwrite the deliberate top-level placement.

        ``channel_origin`` is the evidence that distinguishes them: the slot saver
        persists it once the conversation has been surfaced as a tab, so its
        presence means first surface has already happened.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        log = _FakeLog(
            [key],
            # Surfaced and saved while filing was off, then moved to the top
            # level: provenance recorded, no folder, no filing marker.
            {key: {"memory_mode": "persistent", "channel_origin": True}},
        )
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        slot = dashboard_state._slots[channel_slots.channel_slot_name(key)]
        assert fid and slot.folder_id == "", (
            "a conversation surfaced before filing was enabled got filed anyway; "
            "the user's top-level placement was overwritten"
        )
        assert not log.get_metadata(key).get(
            "folder_id"
        ), "the re-filing was persisted, so the placement is lost for good"

    def test_filing_is_persisted_so_it_never_runs_twice(self, dashboard_state: Any) -> None:
        """The pass that files a conversation must record it ON DISK.

        A freshly surfaced slot is deliberately not dirty — its window came
        straight off the file — so the periodic flush skips it and neither the
        placement nor the marker would reach the metadata line on their own. If
        they do not, the next pass after a restart files the conversation again,
        undoing wherever the user moved it. Asserted against the store, not a
        method call.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        log = _FakeLog([key])
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        slot = dashboard_state._slots[channel_slots.channel_slot_name(key)]
        assert slot.folder_id == fid

        stored = log.get_metadata(key)
        assert (
            stored.get("folder_id") == fid
        ), "the folder placement was not persisted; a restart would lose it"
        assert stored.get("channel_folder_filed") is True, (
            "filing was not recorded on disk; the next pass would file this "
            "conversation a second time and undo a manual move"
        )

    def test_inherited_tags_ride_the_same_filing_write(self, dashboard_state: Any) -> None:
        """Marker and tags are persisted atomically, or a crash loses the tags.

        The filing marker is what tells every later pass (and a post-crash
        restore) that inheritance already ran. Persisting it WITHOUT the tags
        opens a window: crash after the metadata write but before the slot's
        first save, and the restored conversation has the marker, no tags, and
        no way to ever get them — the marker blocks re-inheritance by design.
        One write closes the window.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        dashboard_state._tags = [{"id": "t1", "name": "alpha", "color": "#123456"}]
        dashboard_state._tags_authoritative = True
        for f in dashboard_state._folders:
            if f["id"] == fid:
                f["tags"] = ["t1"]
        log = _FakeLog([key])
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        slot = dashboard_state._slots[channel_slots.channel_slot_name(key)]
        assert slot.tags == ["t1"]

        stored = log.get_metadata(key)
        assert stored.get("channel_folder_filed") is True
        assert stored.get("tags") == ["t1"], (
            "inherited tags were not persisted with the filing marker; a crash "
            "before the slot's first save would lose them permanently"
        )

    def test_a_tag_deleted_before_the_filing_write_is_not_resurrected(
        self, dashboard_state: Any
    ) -> None:
        """Validation runs at the write boundary, not resolve time.

        The reconcile pass reads folder tags early, then awaits transcript and
        config reads before filing. A tag deleted in that window must not be
        written onto the freshly filed chat — the filing write validates the
        ids against the vocabulary as it is at write time. Modeled here by a
        folder carrying an id the vocabulary does not contain.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        # The folder still references "deleted"; the vocabulary does not have it.
        dashboard_state._tags = [{"id": "t1", "name": "alpha", "color": "#123456"}]
        dashboard_state._tags_authoritative = True
        for f in dashboard_state._folders:
            if f["id"] == fid:
                f["tags"] = ["deleted", "t1"]
        log = _FakeLog([key])
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        slot = dashboard_state._slots[channel_slots.channel_slot_name(key)]
        assert slot.tags == ["t1"]
        assert log.get_metadata(key).get("tags") == [
            "t1"
        ], "a deleted tag id was resurrected into the filing write"

    def test_filing_validate_and_write_hold_the_tags_write_lock(self, dashboard_state: Any) -> None:
        """The vocabulary intersection and the filing write are ONE critical
        section under ``tags_write_lock``, mirroring ``api_chat_slot_tags``: a
        tag deletion committing between the intersection and the write would
        resurrect the deleted id onto the filed chat, and the sticky filing
        marker means no later pass ever re-validates it."""
        from kiro_crew.dashboard.chat_tags import tags_write_lock

        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        dashboard_state._tags = [{"id": "t1", "name": "alpha", "color": "#123456"}]
        dashboard_state._tags_authoritative = True
        for f in dashboard_state._folders:
            if f["id"] == fid:
                f["tags"] = ["t1"]
        log = _FakeLog([key])
        held_at_write: list[bool] = []
        orig = log.update_metadata_if

        def _spy(*args: Any, **kwargs: Any) -> Any:
            held_at_write.append(tags_write_lock(dashboard_state).locked())
            return orig(*args, **kwargs)

        log.update_metadata_if = _spy  # type: ignore[method-assign]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        assert held_at_write == [True], (
            "the filing write ran outside tags_write_lock; a concurrent tag "
            "deletion could resurrect a deleted id onto the filed chat"
        )

    def test_a_save_cannot_erase_a_marker_it_never_loaded(self, dashboard_state: Any) -> None:
        """An on-disk marker survives a save by a slot that never restored it.

        There are four paths that rebuild a slot from history, and any one of them
        omitting the marker would be enough to lose it: the save rebuilds the
        metadata line from scratch, so a slot whose in-memory flag is False writes
        a line WITHOUT the marker, and the conversation gets re-filed on the next
        pass. Carrying the on-disk value forward makes that whole class of
        omission harmless rather than relying on every restore path being correct.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        slot = dashboard_state.get_or_create_slot("chan2")
        slot.append("user", "hello")
        slot.drain()
        _save_slot_to_history(dashboard_state, slot, force=True)
        key = effective_session_key(slot)
        dashboard_state.conversation_log.update_metadata(key, {"channel_folder_filed": True})

        # A slot that did NOT pick the marker up in memory saves again.
        assert slot._channel_folder_filed is False
        slot.append("user", "another turn")
        slot.drain()
        _save_slot_to_history(dashboard_state, slot, force=True)

        assert (
            dashboard_state.conversation_log.get_metadata(key).get("channel_folder_filed") is True
        ), (
            "a save erased the on-disk filing marker; the conversation would be "
            "re-filed and the user's placement undone"
        )

    def test_the_filing_marker_survives_a_later_slot_save(self, dashboard_state: Any) -> None:
        """Moving the session must not erase the record that filing happened.

        ``_save_slot_to_history`` rebuilds the metadata line from scratch,
        preserving only an explicit allowlist, so any key it does not write is
        DROPPED. The user moving a filed conversation to the top level triggers
        exactly such a save with ``folder_id`` now empty — if the marker were not
        written back there, that save would erase it and the next reconcile after
        a restart would file the conversation straight back into the folder,
        undoing the move. This is the same defect as
        ``test_a_move_to_the_top_level_survives_a_restart``, reached through the
        save path rather than the reconcile path.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        slot = dashboard_state.get_or_create_slot("chan")
        slot.append("user", "hello")
        slot.drain()
        # The state after filing, then the user dragging it to the top level.
        slot._channel_folder_filed = True
        slot.folder_id = ""
        _save_slot_to_history(dashboard_state, slot, force=True)

        stored = dashboard_state.conversation_log.get_metadata(effective_session_key(slot))
        assert stored.get("folder_id") in (None, ""), "precondition: the move cleared the folder"
        assert stored.get("channel_folder_filed") is True, (
            "a slot save erased the filing marker; the conversation would be "
            "re-filed after a restart, undoing the user's move"
        )

    def test_a_move_to_the_top_level_survives_a_restart(self, dashboard_state: Any) -> None:
        """An already-filed conversation is never filed again.

        Moving a session to the top level clears its ``folder_id``, and the
        metadata line OMITS that key when it is empty — so a ``folder_id`` check
        alone cannot tell "the user moved it out" from "never filed", and the
        next pass after a restart would file it straight back in. The persisted
        ``channel_folder_filed`` marker is what distinguishes them.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        dashboard_state.conversation_log = _FakeLog(
            [key],
            # The state after filing, then a drag to the top level: the marker
            # persists, folder_id does not.
            {key: {"memory_mode": "persistent", "model": "m", "channel_folder_filed": True}},
        )
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0)) == 1
        slot = dashboard_state._slots[channel_slots.channel_slot_name(key)]
        # The folder exists and is configured, yet this conversation is left
        # where the user put it.
        assert fid and slot.folder_id == ""

    def test_a_placement_made_mid_write_is_not_overwritten(self, dashboard_state: Any) -> None:
        """Issuing our write first does not mean it lands first.

        The filing write waits on the cross-process history lock, so the user's
        move can acquire that lock ahead of it. The decision therefore has to be
        re-made under the lock: if the record has since gained a placement of its
        own, our merge must be skipped and the conversation surfaced unfiled.
        """
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord"))
        log = _FakeLog([key])
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        # Stand in for the user's move winning the lock: the record acquires a
        # placement between this pass deciding to file and its write being applied.
        real_guard_call = log.update_metadata_if

        def _racing(key_: str, fields: dict[str, Any], guard: Any) -> bool:
            log.update_metadata(key_, {"folder_id": "user-picked"})
            return real_guard_call(key_, fields, guard)

        log.update_metadata_if = _racing  # type: ignore[assignment]

        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 0))

        # The user's placement stands, and ours was not merged over it.
        assert log.get_metadata(key)["folder_id"] == "user-picked"
        assert "channel_folder_filed" not in log.get_metadata(key)
        # The surfaced slot is unfiled: this pass declined to apply a placement it
        # could not justify, and the record — not this slot — is what the
        # next restart restores from.
        slot = dashboard_state._slots[channel_slots.channel_slot_name(key)]
        assert slot.folder_id == ""


class TestStampIsTheIdentity:
    """The channel's folder is found by its stamp, not by its configured name."""

    def test_a_renamed_folder_is_relabelled_not_duplicated(self, dashboard_state: Any) -> None:
        """A sidebar rename must not cost the user a duplicate folder.

        Name-based lookup could not see the renamed folder, so the next settings
        save created a second branded one next to it and filing split across the
        two.
        """
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(
                dashboard_state, "discord", "Discord", relabel=True
            )
        )
        # The user renames it in the sidebar.
        (folder,) = dashboard_state._folders
        folder["name"] = "Team chat"

        # A save that carried the folder field again: the configured name is the
        # newer intent, so it relabels rather than building a second folder.
        again = asyncio.run(
            channel_folders.ensure_channel_folder(
                dashboard_state, "discord", "Discord", relabel=True
            )
        )

        assert again == fid, "the same folder must be reused, not replaced"
        assert len(dashboard_state._folders) == 1, "a second folder was created"
        assert dashboard_state._folders[0]["name"] == "Discord"

    def test_renaming_preserves_the_folder_id_so_filed_sessions_stay_put(
        self, dashboard_state: Any
    ) -> None:
        """Relabelling must not orphan what is already filed.

        Sessions reference the folder by id, so reusing the id is what keeps them
        inside it across a name change.
        """
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(
                dashboard_state, "discord", "Discord", relabel=True
            )
        )
        renamed = asyncio.run(
            channel_folders.ensure_channel_folder(
                dashboard_state, "discord", "Team chat", relabel=True
            )
        )
        assert renamed == fid
        assert dashboard_state._folders[0]["name"] == "Team chat"

    def test_an_unstamped_user_folder_of_the_same_name_is_not_rebranded(
        self, dashboard_state: Any
    ) -> None:
        """Adopting a folder the user made for themselves must not brand it."""
        asyncio.run(
            dashboard_state.mutate_folders(
                lambda fs: (
                    fs.append({"id": "mine", "name": "Notes", "order": 0, "collapsed": False}),
                    (True, None),
                )[1]
            )
        )
        got = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Notes")
        )
        assert got == "mine"
        assert "channel" not in dashboard_state._folders[0]


class TestRelabelOnlyOnFolderIntent:
    """Relabelling is for the save that set the name, not for every save."""

    def test_relabel_false_keeps_a_sidebar_rename(self, dashboard_state: Any) -> None:
        """An unrelated save must not revert the user's rename.

        These endpoints run on every section save, so without this gate a
        token-only save renamed the folder back to the stored config value.
        """
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(
                dashboard_state, "discord", "Discord", relabel=True
            )
        )
        dashboard_state._folders[0]["name"] = "Team chat"

        again = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        assert again == fid
        assert dashboard_state._folders[0]["name"] == "Team chat"

    def test_relabel_true_applies_the_new_name(self, dashboard_state: Any) -> None:
        fid = asyncio.run(
            channel_folders.ensure_channel_folder(
                dashboard_state, "discord", "Discord", relabel=True
            )
        )
        again = asyncio.run(
            channel_folders.ensure_channel_folder(
                dashboard_state, "discord", "Team chat", relabel=True
            )
        )
        assert again == fid
        assert dashboard_state._folders[0]["name"] == "Team chat"

    def test_a_missing_folder_is_still_recreated_without_relabel(
        self, dashboard_state: Any
    ) -> None:
        """Ensure-exists is unconditional; only the RENAME is gated.

        A folder the user deleted still comes back on the next save, which is what
        the setting's help text promises.
        """
        asyncio.run(
            channel_folders.ensure_channel_folder(
                dashboard_state, "discord", "Discord", relabel=True
            )
        )
        asyncio.run(dashboard_state.mutate_folders(lambda fs: (True, fs.clear())))

        made = asyncio.run(
            channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord")
        )
        assert made
        assert dashboard_state._folders[0]["name"] == "Discord"


class _ModifiedLog(_FakeLog):
    """:class:`_FakeLog` whose sessions carry distinct mtimes and memory modes.

    The base fake reports every session as ``modified: 0.0``, which cannot
    distinguish "newest first" from "in whatever order the store listed them" --
    the two agree on a tie. It also has no ``memory_mode``, which the ephemeral
    skip reads.
    """

    def __init__(
        self,
        keys: list[str],
        meta: dict[str, Any] | None = None,
        modified: dict[str, float] | None = None,
        modes: dict[str, str] | None = None,
    ) -> None:
        super().__init__(keys, meta)
        self._modified = dict(modified or {})
        self._modes = dict(modes or {})

    def list_sessions(self) -> list[dict[str, Any]]:
        out = []
        for k in self._keys:
            row: dict[str, Any] = {
                "key": k,
                "title": "",
                "modified": self._modified.get(k, 0.0),
            }
            if k in self._modes:
                row["memory_mode"] = self._modes[k]
            out.append(row)
        return out


class TestNeedsBackfillFiling:
    """The guard, which is the whole of the behaviour change.

    Its one difference from :func:`needs_default_filing` is that it ignores
    ``channel_origin``, and that difference is what makes an existing
    conversation reachable at all.
    """

    def test_a_conversation_that_only_predates_the_setting_is_eligible(self) -> None:
        # The target population: surfaced and saved while filing was off, so it
        # carries the provenance flag and nothing else. Automatic filing refuses
        # this record; an explicit click must not.
        assert channel_slots.needs_backfill_filing({"channel_origin": True}) is True
        assert channel_slots.needs_default_filing({"channel_origin": True}) is False

    def test_a_never_surfaced_conversation_is_eligible(self) -> None:
        assert channel_slots.needs_backfill_filing({}) is True

    def test_a_conversation_in_a_folder_is_refused(self) -> None:
        assert channel_slots.needs_backfill_filing({"folder_id": "user-choice"}) is False

    def test_a_conversation_filed_then_moved_to_the_top_level_is_refused(self) -> None:
        """The marker with no folder beside it IS the record of a user move."""
        assert (
            channel_slots.needs_backfill_filing(
                {"channel_folder_filed": True, "channel_origin": True}
            )
            is False
        )


class TestBackfillChannelFolder:
    @pytest.fixture(autouse=True)
    def _quiet_push(self, dashboard_state: Any) -> None:
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

    def _folder(self, state: Any, ns: str = "discord", name: str = "Discord") -> str:
        _write_config(ns, name)
        return asyncio.run(channel_folders.ensure_channel_folder(state, ns, name))

    def test_files_a_conversation_that_predates_the_setting(self, dashboard_state: Any) -> None:
        """The defect this fixes: switching the setting on left these behind."""
        fid = self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True, "title": "Standup"}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["reason"] == ""
        assert [m["key"] for m in report["moved"]] == [key]
        # Reported by name, because there is no bulk undo and the list is the
        # only record of what moved.
        assert report["moved"][0]["title"] == "Standup"
        # Persisted, not just reported: the marker is what stops the background
        # pass filing it a second time after the user moves it.
        assert log.get_metadata(key)["folder_id"] == fid
        assert log.get_metadata(key)["channel_folder_filed"] is True

    def test_leaves_a_conversation_the_user_filed_where_it_is(self, dashboard_state: Any) -> None:
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"folder_id": "user-choice"}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert log.get_metadata(key)["folder_id"] == "user-choice"

    def test_leaves_a_conversation_moved_to_the_top_level_at_the_top_level(
        self, dashboard_state: Any
    ) -> None:
        """Filed once, then dragged out. The click must not drag it back."""
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_folder_filed": True}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert "folder_id" not in log.get_metadata(key)

    def test_an_ephemeral_conversation_is_never_given_a_folder(self, dashboard_state: Any) -> None:
        """Incognito asked for no trace; a durable placement contradicts that."""
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {}}, modes={key: "incognito"})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert "folder_id" not in log.get_metadata(key)

    def test_another_channels_conversations_are_untouched(self, dashboard_state: Any) -> None:
        self._folder(dashboard_state)
        mine = "discord:kirocrew:direct:U1"
        theirs = "telegram:kirocrew:direct:U2"
        log = _ModifiedLog([mine, theirs], {mine: {}, theirs: {}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [mine]
        assert "folder_id" not in log.get_metadata(theirs)

    def test_the_setting_being_off_is_reported_as_such(self, dashboard_state: Any) -> None:
        """Distinguishable from "nothing to do", which the panel words differently."""
        key = "discord:kirocrew:direct:U1"
        dashboard_state.conversation_log = _ModifiedLog([key], {key: {}})

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["reason"] == "not_configured"
        assert report["moved"] == []

    def test_a_configured_but_absent_folder_is_reported_as_such(self, dashboard_state: Any) -> None:
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        dashboard_state.conversation_log = _ModifiedLog([key], {key: {}})

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["reason"] == "folder_missing"
        assert report["folder_name"] == "Discord"
        # Nothing is created from this path: that would put an fsync on the loop.
        assert dashboard_state._folders == []

    def test_an_unknown_namespace_files_nothing(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = _ModifiedLog([])
        report = asyncio.run(
            channel_slots.backfill_channel_folder(dashboard_state, "not-a-channel")
        )
        assert report["reason"] == "not_configured"

    def test_one_click_is_bounded_and_says_how_many_remain(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """The cap is reportable rather than silent, so a second click continues."""
        monkeypatch.setattr(channel_slots, "BACKFILL_MOVE_LIMIT", 2)
        self._folder(dashboard_state)
        keys = [f"discord:kirocrew:direct:U{i}" for i in range(5)]
        log = _ModifiedLog(keys, {k: {} for k in keys})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert len(report["moved"]) == 2
        assert report["remaining"] == 3
        # A capped run failed at nothing. This is the half that makes `failed`
        # worth reporting: without it, "3 still unfiled" reads the same here as
        # it does on a pass whose every write blew up.
        assert report["failed"] == 0
        # Idempotent, so the next click picks up exactly what this one did not.
        again = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))
        assert len(again["moved"]) == 2
        assert again["remaining"] == 1
        assert again["failed"] == 0

    def test_a_capped_click_takes_the_newest_conversations_first(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """A capped run must file what the user is most likely looking for."""
        monkeypatch.setattr(channel_slots, "BACKFILL_MOVE_LIMIT", 1)
        self._folder(dashboard_state)
        old = "discord:kirocrew:direct:OLD"
        new = "discord:kirocrew:direct:NEW"
        log = _ModifiedLog([old, new], {old: {}, new: {}}, modified={old: 100.0, new: 900.0})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [new]

    def test_an_open_tab_is_re_placed_without_a_restart(self, dashboard_state: Any) -> None:
        """Otherwise the button looks inert for the conversations on screen."""
        fid = self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        slot = channel_slots.surface_channel_session(
            dashboard_state, {"key": key, "title": "", "modified": 0.0}, {}, []
        )
        assert slot is not None and slot.folder_id == ""
        pushes: list[int] = []
        dashboard_state.push_slots_update = lambda: pushes.append(1)  # type: ignore[method-assign]

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert slot.folder_id == fid
        assert slot._channel_folder_filed is True
        assert pushes, "the sidebar is not told the tab moved"

    def test_a_folder_the_user_just_dragged_a_tab_into_wins(self, dashboard_state: Any) -> None:
        """The in-memory placement can be AHEAD of disk.

        A drag sets the slot immediately and saves asynchronously, so the record
        the guard reads still says unfiled. Reading only the record would move a
        conversation the user placed a second earlier.
        """
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        slot = channel_slots.surface_channel_session(
            dashboard_state, {"key": key, "title": "", "modified": 0.0}, {}, []
        )
        assert slot is not None
        slot.folder_id = "dragged-here"

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert slot.folder_id == "dragged-here"
        assert "folder_id" not in log.get_metadata(key)

    def test_a_placement_landing_mid_pass_is_not_overwritten(self, dashboard_state: Any) -> None:
        """The guard re-decides under the store's lock, not at scan time."""
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        original = log.update_metadata_if

        def _place_first(k: str, fields: dict[str, Any], guard: Any) -> bool:
            # Stand in for the user's own move committing while this pass queued
            # behind the cross-process lock.
            log.update_metadata(k, {"folder_id": "user-choice"})
            return original(k, fields, guard)

        log.update_metadata_if = _place_first  # type: ignore[method-assign]

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert log.get_metadata(key)["folder_id"] == "user-choice"

    def test_a_filed_conversation_inherits_the_folders_tags(self, dashboard_state: Any) -> None:
        """Same creation-time inheritance the automatic filing path applies."""
        fid = self._folder(dashboard_state)
        asyncio.run(
            dashboard_state.mutate_folders(lambda fs: (True, fs[0].__setitem__("tags", ["t1"])))
        )
        dashboard_state._tags = [{"id": "t1", "label": "t1"}]
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        stored = log.get_metadata(key)
        assert stored["folder_id"] == fid
        assert stored.get("tags") == ["t1"]

    def test_a_reported_title_is_redacted(self, dashboard_state: Any) -> None:
        """Titles are generated from channel content, so they are untrusted here."""
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        secret = "ghp_" + "a" * 36
        log = _ModifiedLog([key], {key: {"channel_origin": True, "title": secret}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert secret not in report["moved"][0]["title"]

    def test_no_conversation_log_is_reported_not_raised(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = None
        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))
        assert report["reason"] == "unavailable"

    def test_one_failing_conversation_does_not_abandon_the_rest(self, dashboard_state: Any) -> None:
        self._folder(dashboard_state)
        bad = "discord:kirocrew:direct:BAD"
        good = "discord:kirocrew:direct:GOOD"
        log = _ModifiedLog([bad, good], {bad: {}, good: {}}, modified={bad: 900.0, good: 100.0})
        dashboard_state.conversation_log = log
        original = log.update_metadata_if

        def _explode(k: str, fields: dict[str, Any], guard: Any) -> bool:
            if k == bad:
                raise OSError("disk went away")
            return original(k, fields, guard)

        log.update_metadata_if = _explode  # type: ignore[method-assign]

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [good]

    def test_a_folder_deleted_mid_pass_strands_nothing(self, dashboard_state: Any) -> None:
        """A dead ``folder_id`` beside the filing marker has NO recovery.

        The marker is what :func:`needs_backfill_filing` refuses on, so a
        conversation stamped with a folder that is gone ends up in no folder AND
        permanently ineligible for a later click. Empty tags cannot carry the
        difference between "this folder has no tags" and "this folder is gone",
        which is why the read reports absence explicitly.
        """
        self._folder(dashboard_state)
        keys = [f"discord:kirocrew:direct:U{i}" for i in range(3)]
        log = _ModifiedLog(keys, {k: {} for k in keys})
        dashboard_state.conversation_log = log
        # Delete the folder the moment the first write is attempted.
        original = log.update_metadata_if
        deleted: list[int] = []

        def _delete_then_write(k: str, fields: dict[str, Any], guard: Any) -> bool:
            if not deleted:
                deleted.append(1)
                dashboard_state._folders.clear()
            return original(k, fields, guard)

        log.update_metadata_if = _delete_then_write  # type: ignore[method-assign]

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        # Whatever it managed before the deletion is reported; nothing after it
        # is stamped with the dead id. Its OWN reason: a folder DELETED mid-pass
        # needs the opposite sentence from one that was never created, and the
        # panel must not have to infer which it had from the counts.
        assert report["reason"] == "folder_gone"
        stranded = [
            k
            for k in keys
            if log.get_metadata(k).get("channel_folder_filed")
            and not any(
                f.get("id") == log.get_metadata(k).get("folder_id")
                for f in dashboard_state._folders
            )
        ]
        assert len(stranded) <= 1, stranded
        assert len(report["moved"]) == len(stranded)

    def test_a_drag_during_the_write_is_not_reverted_in_memory(self, dashboard_state: Any) -> None:
        """The mirror re-reads the slot, so a placement made mid-write survives.

        The write is an await. A drag landing during it sets the slot's folder in
        memory before its own save lands, so mirroring the value read BEFORE that
        await would revert the user's move -- and the guard on the write cannot
        catch it, because it reads the record their save has not reached yet.
        """
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        slot = channel_slots.surface_channel_session(
            dashboard_state, {"key": key, "title": "", "modified": 0.0}, {}, []
        )
        assert slot is not None
        original = log.update_metadata_if

        def _drag_then_write(k: str, fields: dict[str, Any], guard: Any) -> bool:
            # Stand in for the user dragging the open tab while the write runs:
            # in memory now, on disk only after their save.
            slot.folder_id = "dragged-mid-write"
            return original(k, fields, guard)

        log.update_metadata_if = _drag_then_write  # type: ignore[method-assign]

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert slot.folder_id == "dragged-mid-write"

    def test_writes_that_all_fail_are_not_reported_as_nothing_to_do(
        self, dashboard_state: Any
    ) -> None:
        """A swallowed failure must not render as "nothing needed moving"."""
        self._folder(dashboard_state)
        keys = [f"discord:kirocrew:direct:U{i}" for i in range(2)]
        log = _ModifiedLog(keys, {k: {} for k in keys})
        dashboard_state.conversation_log = log

        def _explode(k: str, fields: dict[str, Any], guard: Any) -> bool:
            raise OSError("disk went away")

        log.update_metadata_if = _explode  # type: ignore[method-assign]

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        # Its OWN reason, not `unavailable`. That value means the store could not
        # be READ, and here every read succeeded and every WRITE failed. Sharing
        # one value made the panel state the wrong cause, and because the panel
        # short-circuits on an empty `moved` it also dropped the count below --
        # the only number that tells the user whether clicking again can help.
        assert report["reason"] == "all_failed"
        # Still unfiled, so they belong in the count that invites another click.
        assert report["remaining"] == 2
        # And every one of them is there because a write FAILED, not because the
        # run hit its cap, so the copy can say so instead of inviting a click
        # that will fail identically.
        assert report["failed"] == 2

    def test_the_receipt_names_the_folder_the_sessions_were_actually_filed_into(
        self, dashboard_state: Any
    ) -> None:
        """One config read, so the reported name and the written id cannot disagree.

        ``report["folder_name"]`` is what the panel shows and ``folder_id`` is what
        every write stamps. Both are derived from a SINGLE config read. With two
        independent reads -- one here and one inside ``lookup_channel_folder`` -- a
        settings save moving this channel from folder A to folder B and committing
        between them yields A's name beside B's id, so the user is told A while
        every session lands in B.

        Asserted by COUNTING the config reads rather than by comparing the strings.
        The property is that a second read does not happen; a comparison alone also
        passes whenever the race does not fire, which is almost always.
        """
        self._folder(dashboard_state, name="Alpha")
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {}})
        dashboard_state.conversation_log = log

        reads: list[str] = []
        real = channel_folders.configured_folder_name

        def _counting_read(ns: str) -> str:
            reads.append(ns)
            # A second read would see the reconfigured value, which is exactly the
            # divergence this pins shut.
            if len(reads) > 1:
                return "Beta"
            return real(ns)

        monkey = channel_slots.configured_folder_name
        channel_slots.configured_folder_name = _counting_read  # type: ignore[assignment]
        channel_folders.configured_folder_name = _counting_read  # type: ignore[assignment]
        try:
            report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))
        finally:
            channel_slots.configured_folder_name = monkey  # type: ignore[assignment]
            channel_folders.configured_folder_name = real  # type: ignore[assignment]

        assert reads == ["discord"], reads
        assert report["folder_name"] == "Alpha"
        # And the session really is in Alpha, not in the folder a second read
        # would have named.
        alpha = next(f for f in dashboard_state._folders if f.get("name") == "Alpha")
        assert log.get_metadata(key).get("folder_id") == alpha.get("id")

    def test_a_deleted_folder_and_one_never_created_report_different_reasons(
        self, dashboard_state: Any
    ) -> None:
        """The two situations are named by the SERVER, not inferred by the panel.

        ``lookup_channel_folder`` answering nothing means no folder ever existed,
        and saving the settings creates it. The per-write re-read finding the
        folder gone means it existed a moment ago, and anything already filed into
        it is stranded on a dead id -- recreating mints a fresh one. Opposite
        remedies, so opposite values.

        An earlier revision reported both as ``folder_missing`` and left the panel
        proving "the folder existed" from ``failed > 0``, a claim about this
        function's control flow made a layer away from it.
        """
        # Deleted WHILE the pass runs, with a write failing on the way.
        self._folder(dashboard_state)
        keys = [f"discord:kirocrew:direct:U{i}" for i in range(3)]
        log = _ModifiedLog(keys, {k: {} for k in keys})
        dashboard_state.conversation_log = log

        def _fail_then_delete(k: str, fields: dict[str, Any], guard: Any) -> bool:
            dashboard_state._folders.clear()
            raise OSError("disk went away")

        log.update_metadata_if = _fail_then_delete  # type: ignore[method-assign]

        gone = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert gone["reason"] == "folder_gone"
        assert gone["moved"] == []
        assert gone["failed"] >= 1

        # Never created: configured, but no folder answers to the name.
        _write_config("discord", "Discord")
        dashboard_state._folders.clear()
        fresh = [f"discord:kirocrew:direct:N{i}" for i in range(2)]
        dashboard_state.conversation_log = _ModifiedLog(fresh, {k: {} for k in fresh})

        never = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert never["reason"] == "folder_missing"
        assert never["moved"] == []
        # The two are distinguishable from the report alone, which is the point.
        assert never["reason"] != gone["reason"]

    def test_a_history_only_conversations_own_tags_survive_filing(
        self, dashboard_state: Any
    ) -> None:
        """The user's tags are unioned with the folder's, never replaced.

        The store merges with ``metadata.update(fields)``, so a bare list under
        ``tags`` overwrites the whole key. This path reaches records the automatic
        one never does: a conversation surfaced while filing was off, tagged by the
        user and never filed, with its tab CLOSED. It passes the guard (which reads
        placement, not tags) and has no live slot to union the tags back through,
        so replacing would destroy them with nothing recording what they were.
        """
        fid = self._folder(dashboard_state)
        asyncio.run(
            dashboard_state.mutate_folders(
                lambda fs: (True, fs[0].__setitem__("tags", ["folder-tag"]))
            )
        )
        dashboard_state._tags = [
            {"id": "folder-tag", "label": "folder-tag"},
            {"id": "important", "label": "important"},
        ]
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True, "tags": ["important"]}})
        dashboard_state.conversation_log = log
        # No live slot: History-only is the case with no in-memory union to fall
        # back on, which is what makes the loss unrecoverable.
        assert channel_slots.channel_slot_name(key) not in dashboard_state._slots

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [key]
        stored = log.get_metadata(key)
        assert stored["folder_id"] == fid
        assert stored["tags"] == ["important", "folder-tag"], stored["tags"]

    def test_an_unreadable_record_does_not_get_its_tags_touched(self, dashboard_state: Any) -> None:
        """Fails closed: inheriting is a convenience, losing tags is permanent."""
        self._folder(dashboard_state)
        asyncio.run(
            dashboard_state.mutate_folders(
                lambda fs: (True, fs[0].__setitem__("tags", ["folder-tag"]))
            )
        )
        dashboard_state._tags = [{"id": "folder-tag", "label": "folder-tag"}]
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        calls: list[str] = []
        original = log.get_metadata

        def _explode_on_reread(k: str) -> dict[str, Any]:
            calls.append(k)
            # Exactly the SECOND read: the first is the pre-scan snapshot and the
            # third is the guard's own read inside update_metadata_if. Failing
            # every read after the first would break the write itself and the test
            # would pass for the wrong reason -- it did, before this was scoped.
            if len(calls) == 2:
                raise OSError("record unreadable")
            return original(k)

        log.get_metadata = _explode_on_reread  # type: ignore[method-assign]

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        log.get_metadata = original  # type: ignore[method-assign]
        stored = log.get_metadata(key)
        # Filed, but the tags key was never written.
        assert stored.get("channel_folder_filed") is True
        assert "tags" not in stored, stored

    def test_the_folder_adding_nothing_new_writes_no_tags_key(self, dashboard_state: Any) -> None:
        """No write on `tags` at all when the union would not change it."""
        self._folder(dashboard_state)
        asyncio.run(
            dashboard_state.mutate_folders(lambda fs: (True, fs[0].__setitem__("tags", ["shared"])))
        )
        dashboard_state._tags = [{"id": "shared", "label": "shared"}]
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True, "tags": ["shared"]}})
        dashboard_state.conversation_log = log
        written: list[dict[str, Any]] = []
        original = log.update_metadata_if

        def _record(k: str, fields: dict[str, Any], guard: Any) -> bool:
            written.append(dict(fields))
            return original(k, fields, guard)

        log.update_metadata_if = _record  # type: ignore[method-assign]

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert written and "tags" not in written[0], written
        assert log.get_metadata(key)["tags"] == ["shared"]
