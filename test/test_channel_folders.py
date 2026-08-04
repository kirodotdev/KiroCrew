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
        assert asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord")) == "f1"

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

    def test_prefers_the_channel_stamped_folder_on_a_name_tie(
        self, dashboard_state: Any
    ) -> None:
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

        assert asyncio.run(channel_folders.lookup_channel_folder(dashboard_state, "discord")) == "f1"
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
        assert json.loads(
            (config_dir() / dashboard_state._FOLDERS_FILE).read_text(encoding="utf-8")
        )[0]["id"] == fid

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
        assert asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "")) == ""
        assert dashboard_state._folders == []

    def test_adopts_and_unhides_an_existing_folder(self, dashboard_state: Any) -> None:
        dashboard_state._folders.append(
            {"id": "f1", "name": "Discord", "order": 0, "parent_id": "", "hidden": True}
        )
        assert (
            asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord"))
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
            asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord"))
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
            asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord"))
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
            asyncio.run(channel_folders.ensure_channel_folder(dashboard_state, "discord", "Discord"))
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


class _FakeLog:
    """Minimal ConversationLog stand-in: one channel session, one message.

    Since #1366 the tab and the channel share ONE record, so *meta* is keyed by
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

    def test_one_conversations_folder_is_not_applied_to_another(
        self, dashboard_state: Any
    ) -> None:
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
        assert not log.get_metadata(key_b).get("folder_id"), (
            "the re-filing was persisted, so the user's placement is lost for good"
        )

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
        assert stored.get("folder_id") == fid, (
            "the folder placement was not persisted; a restart would lose it"
        )
        assert stored.get("channel_folder_filed") is True, (
            "filing was not recorded on disk; the next pass would file this "
            "conversation a second time and undo a manual move"
        )

    def test_a_save_cannot_erase_a_marker_it_never_loaded(
        self, dashboard_state: Any
    ) -> None:
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

        assert dashboard_state.conversation_log.get_metadata(key).get(
            "channel_folder_filed"
        ) is True, (
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

        stored = dashboard_state.conversation_log.get_metadata(
            effective_session_key(slot)
        )
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
