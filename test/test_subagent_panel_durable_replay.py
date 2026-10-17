"""The subagent panel's durable rebuild source.

The panel's live source is gateway memory, so a replacement gateway process has
nothing to replay for the runs it never tracked. These tests pin the persisted
fallback that answers for them, and -- just as importantly -- pin what it must
refuse to invent: a slot-tracked native card, a run whose memory mode is not
persistent, a second copy of a run the live manager still holds, and a failure
for a run the tombstone records as a user stop.

This module and its two siblings share that work. This one pins the frame the replay sends
and the panel's dismissals: the durable record that keeps a dismissed card from coming back
on either reader, the registrations that protect its store, the grant the replay records,
and both arms of the dismiss route. What one run folder rebuilds into, the four refusals
above among it, is pinned in ``test_subagent_panel_durable_replay_records.py`` and who may
read it in ``test_subagent_panel_durable_replay_access.py``; the shared helpers live in
``subagent_panel_durable_replay_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
from types import SimpleNamespace

import pytest
from subagent_panel_durable_replay_helpers import (  # noqa: F401 -- the fixture is requested by name
    DAY,
    agent_root_fixture,
    ids,
    panel,
    write_record,
)

from kiro_crew import subagent_persistence
from kiro_crew.dashboard.handlers import messaging
from kiro_crew.dashboard.state import (
    PERSISTED_SUBAGENT_REPLAY_KEEP,
    PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
)
from kiro_crew.dashboard.ws import build_persisted_subagent_frame
from kiro_crew.subagent_persistence import (
    delete_agent_folder,
    dismissed_panel_ids,
    prune_orphan_panel_dismissals,
    read_panel_records,
    read_state,
    record_panel_dismissal,
)


def messaging_crew_log_emit():
    """The emitter module the dismiss route imports inside its own helper.

    Patched there rather than on ``messaging``: the helper does a local import, so
    a name set on ``messaging`` is never read.
    """
    from kiro_crew.crew_log import emit

    return emit


class TestReplayFrame:
    """The frame shape the panel's reducers consume."""

    def plain(self, text: str) -> str:
        return text

    def test_frame_is_a_terminal_subagent_event(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "abc123",
                "task": "audit the gate",
                "agent": "kirocrew-worker",
                "parent_session": "dashboard:chat-1",
                "started": 100.0,
                "elapsed": 42.0,
                "outcome": "completed",
                "error": "",
                "stopped": False,
            },
            redact=self.plain,
        )
        assert frame["type"] == "subagent_done"
        data = frame["data"]
        assert data["id"] == "abc123"
        assert data["slot"] == "chat-1"
        assert data["elapsed"] == 42.0
        assert data["outcome"] == "completed"
        assert data["task"] == "audit the gate"
        assert data["agent"] == "kirocrew-worker"
        assert data["stopped"] is False

    def test_a_recorded_stop_reaches_the_frame(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "stopped",
                "error": "",
                "stopped": True,
            },
            redact=self.plain,
        )
        assert frame["data"]["stopped"] is True
        assert frame["data"]["outcome"] == "stopped"

    def test_no_error_is_sent_as_null_not_an_empty_string(self):
        frame = build_persisted_subagent_frame(
            {"id": "a", "parent_session": "dashboard:chat-1", "outcome": "completed", "error": ""},
            redact=self.plain,
        )
        assert frame["data"]["error"] is None

    def test_an_error_travels_through_the_callers_redactor(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "failed",
                "error": "Orphaned: gateway_restart",
            },
            redact=lambda text: text.upper(),
        )
        assert frame["data"]["error"] == "ORPHANED: GATEWAY_RESTART"

    def test_task_and_agent_travel_through_the_callers_redactor(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "completed",
                "error": "",
                "task": "secret",
                "agent": "worker",
            },
            redact=lambda text: f"[{text}]",
        )
        assert frame["data"]["task"] == "[secret]"
        assert frame["data"]["agent"] == "[worker]"

    def test_a_record_with_no_parent_yields_an_ownerless_frame(self):
        """Which the replay's own owner filter then drops, as it does for live frames."""
        frame = build_persisted_subagent_frame(
            {"id": "a", "parent_session": "", "outcome": "completed", "error": ""},
            redact=self.plain,
        )
        assert frame["data"]["slot"] == ""


class TestADismissalOutlivesTheManager:
    """A dismissed card must not come back when the panel reads folders.

    The live half of a dismissal is ``settle_before_delete`` popping the run out
    of the manager, and before this it was the WHOLE dismissal: the durable half
    excludes only the ids the live manager still holds, so the next rebuild read
    the folder the delete left behind and put the card back. The dismissal is
    recorded durably instead, and the folder stays where it is -- ``spawn_continue``
    reseeds its session map from that folder's ``state.json``.
    """

    def test_a_dismissed_run_stays_out_of_the_panel(self, agent_root):
        write_record(agent_root, "keep-me")
        write_record(agent_root, "dismiss-me")
        assert sorted(ids(panel(keep=10, max_age_secs=DAY))) == ["dismiss-me", "keep-me"]

        assert record_panel_dismissal("dismiss-me") is True

        assert ids(panel(keep=10, max_age_secs=DAY)) == ["keep-me"]

    def test_the_run_folder_is_left_alone_so_a_conversation_survives(self, agent_root):
        """Removing the folder would hide the card AND break ``spawn_continue``."""
        write_record(agent_root, "dismiss-me")
        record_panel_dismissal("dismiss-me")
        assert (agent_root / "dismiss-me" / "state.json").exists()
        assert read_state("dismiss-me") is not None

    def test_the_record_lives_outside_the_agent_writable_run_folder(self, agent_root):
        """A run must not be able to hide its own card from the operator."""
        write_record(agent_root, "dismiss-me")
        record_panel_dismissal("dismiss-me")
        store = subagent_persistence._panel_dismissals_dir()
        written = [p for p in store.rglob("*") if p.is_file()]
        assert written, "the dismissal left no durable record"
        assert not any(agent_root in p.parents for p in written)
        assert agent_root not in store.parents and store != agent_root

    def test_a_run_with_no_folder_records_nothing(self, agent_root):
        """Nothing durable can resurrect that card, and prune would never reclaim it."""
        assert record_panel_dismissal("never-ran") is False
        assert dismissed_panel_ids() == frozenset()

    def test_an_unreadable_record_shows_the_run_rather_than_hiding_it(
        self, agent_root, monkeypatch
    ):
        """Fail OPEN: a fault may resurrect a card, never hide a run's history."""
        write_record(agent_root, "dismiss-me")
        record_panel_dismissal("dismiss-me")

        def boom(_path):
            raise OSError("registry unreadable")

        monkeypatch.setattr(subagent_persistence.os, "scandir", boom)
        assert dismissed_panel_ids() == frozenset()

    def test_a_dismissed_folder_does_not_spend_a_candidate_slot(self, agent_root):
        """Filtered before the heap, so a dismissal cannot push a visible run out."""
        now = time.time()
        write_record(agent_root, "older", mtime=now - 300)
        for index in range(4):
            write_record(agent_root, f"dismissed-{index}", mtime=now - index)
            record_panel_dismissal(f"dismissed-{index}")
        records = read_panel_records(keep=1, max_age_secs=DAY)
        assert ids(records.records) == ["older"]
        assert records.overflow == 0

    def test_deleting_the_folder_reclaims_the_record(self, agent_root):
        write_record(agent_root, "dismiss-me")
        record_panel_dismissal("dismiss-me")
        assert dismissed_panel_ids() == frozenset({"dismiss-me"})
        delete_agent_folder("dismiss-me")
        assert dismissed_panel_ids() == frozenset()

    def test_settle_before_delete_records_the_dismissal(self, agent_root, monkeypatch):
        """The pop and the durable record are one act, not two the route must pair."""
        import kiro_crew.subagent as subagent_module

        write_record(agent_root, "a1")
        recorded: list[str] = []

        def record(agent_id):
            recorded.append(agent_id)
            return subagent_persistence.DISMISSAL_RECORDED

        monkeypatch.setattr(subagent_module, "record_panel_dismissal_outcome", record)
        manager = SimpleNamespace(
            _agents={"a1": SimpleNamespace(id="a1", _ending_claimed=False, done=True)},
            _tasks={},
            _report_owners={},
        )
        asyncio.run(subagent_module.SubagentManager.settle_before_delete(manager, "a1"))
        assert recorded == ["a1"]
        assert "a1" not in manager._agents

    def test_the_record_is_written_off_the_event_loop(self, agent_root):
        """It is a synchronous file write, and its caller is a coroutine."""
        import inspect

        import kiro_crew.subagent as subagent_module

        source = inspect.getsource(subagent_module.SubagentManager.settle_before_delete)
        assert "asyncio.to_thread(record_panel_dismissal" in source

    def test_the_sweep_removes_a_record_whose_folder_is_gone(self, agent_root):
        import shutil

        write_record(agent_root, "swept")
        record_panel_dismissal("swept")
        shutil.rmtree(agent_root / "swept")
        assert prune_orphan_panel_dismissals() == 1
        assert dismissed_panel_ids() == frozenset()

    def test_the_sweep_keeps_a_record_whose_folder_remains(self, agent_root):
        write_record(agent_root, "kept")
        record_panel_dismissal("kept")
        assert prune_orphan_panel_dismissals() == 0
        assert dismissed_panel_ids() == frozenset({"kept"})

    def test_the_sweep_counts_only_what_it_removed(self, agent_root):
        """A name no run could carry is removed once, not counted every cycle."""
        write_record(agent_root, "kept")
        record_panel_dismissal("kept")
        stray = subagent_persistence._panel_dismissals_dir() / "not..an..id"
        stray.write_text("{}", encoding="utf-8")
        assert prune_orphan_panel_dismissals() == 1
        assert prune_orphan_panel_dismissals() == 0
        assert dismissed_panel_ids() == frozenset({"kept"})


class TestAFailedDismissalWriteIsNotPublished:
    """The pop is the publish, so it must not happen before the record is stored.

    With the write second and its result discarded, an unwritable store still
    popped the run and still answered ``"delivered"`` -- which the DELETE route
    reports as success -- and the card came back on the next reconnect with
    nothing to explain it. The user is told the dismissal worked and watches it
    undo itself.

    The two falsy cases are NOT interchangeable. A run with no folder has nothing
    durable to bring its card back, so its dismissal stands and the pop proceeds;
    only a FAILED write holds it. Collapsing them would make every folderless run
    permanently undismissable, which is the over-correction this pins against.
    """

    def manager(self):
        return SimpleNamespace(
            _agents={"a1": SimpleNamespace(id="a1", _ending_claimed=False, done=True)},
            _tasks={"a1": object()},
            _report_owners={},
        )

    def settle(self, monkeypatch, outcome: str):
        import kiro_crew.subagent as subagent_module

        monkeypatch.setattr(
            subagent_module, "record_panel_dismissal_outcome", lambda agent_id: outcome
        )
        manager = self.manager()
        result = asyncio.run(subagent_module.SubagentManager.settle_before_delete(manager, "a1"))
        return result, manager

    def test_a_failed_write_keeps_the_run_and_asks_for_a_retry(self, agent_root, monkeypatch):
        result, manager = self.settle(monkeypatch, subagent_persistence.DISMISSAL_FAILED)
        assert result == "pending", "a dismissal that was not stored must not report delivered"
        assert "a1" in manager._agents, (
            "the run was popped although its dismissal was never stored, so the card "
            "returns on the next rebuild and the route reported success"
        )
        assert "a1" in manager._tasks

    def test_a_stored_write_publishes_the_dismissal(self, agent_root, monkeypatch):
        result, manager = self.settle(monkeypatch, subagent_persistence.DISMISSAL_RECORDED)
        assert result == "delivered"
        assert "a1" not in manager._agents
        assert "a1" not in manager._tasks

    def test_a_run_with_no_folder_is_still_dismissable(self, agent_root, monkeypatch):
        """Control: the fix must not make a folderless run undismissable forever."""
        result, manager = self.settle(monkeypatch, subagent_persistence.DISMISSAL_NO_FOLDER)
        assert result == "delivered"
        assert "a1" not in manager._agents

    def test_the_outcome_separates_a_missing_folder_from_a_failed_write(self, agent_root):
        """Read off the real function, so the three answers are not just constants."""
        write_record(agent_root, "real11")
        assert (
            subagent_persistence.record_panel_dismissal_outcome("real11")
            == subagent_persistence.DISMISSAL_RECORDED
        )
        assert (
            subagent_persistence.record_panel_dismissal_outcome("never-ran")
            == subagent_persistence.DISMISSAL_NO_FOLDER
        )

    def test_an_unwritable_store_reports_failed_not_missing(self, agent_root, monkeypatch):
        write_record(agent_root, "real22")

        def boom(*_a, **_kw):
            raise OSError("store is read-only")

        monkeypatch.setattr(subagent_persistence, "atomic_write", boom)
        assert (
            subagent_persistence.record_panel_dismissal_outcome("real22")
            == subagent_persistence.DISMISSAL_FAILED
        )
        # The legacy bool keeps its meaning for callers that only need "stored".
        assert subagent_persistence.record_panel_dismissal("real22") is False


class TestTheDismissalCheckRetainsNothing:
    """The reader's working set is bounded, so the dismissal check must be too.

    ``read_panel_records`` caps its candidate heap on purpose. Answering "is this
    dismissed" by enumerating every record into a set put one entry per dismissal
    back inside that bound, so a registry with many retained dismissals set the
    memory the cap exists to fix. The question is per candidate, and so is the
    answer now.
    """

    def test_many_dismissals_do_not_change_what_the_reader_returns(self, agent_root):
        """Behavioural control: the cheaper check still filters every one of them."""
        write_record(agent_root, "visible")
        for index in range(40):
            name = f"gone{index:03d}"
            write_record(agent_root, name)
            assert record_panel_dismissal(name) is True
        assert ids(panel(keep=10, max_age_secs=DAY)) == ["visible"]

    def test_the_probe_answers_per_run(self, agent_root):
        write_record(agent_root, "one")
        write_record(agent_root, "two")
        record_panel_dismissal("one")
        assert subagent_persistence.panel_dismissal_recorded("one") is True
        assert subagent_persistence.panel_dismissal_recorded("two") is False

    def test_the_probe_fails_open(self, agent_root, monkeypatch):
        """A fault resurrects a card; it must never hide an undismissed run."""
        write_record(agent_root, "one")
        record_panel_dismissal("one")
        assert subagent_persistence.panel_dismissal_recorded("one") is True

        def boom(_self):
            raise OSError("registry unreadable")

        monkeypatch.setattr(pathlib.Path, "is_file", boom)
        assert subagent_persistence.panel_dismissal_recorded("one") is False


def _messaging_handler_files(root: pathlib.Path, *holders: str) -> list[pathlib.Path]:
    """``handlers/messaging.py`` and the ``messaging_api`` owners it composes routes from.

    The facade's routes run from those owners, so a source ratchet on the facade
    reads them all; each name in *holders* must live in one of the returned files.
    """
    import inspect

    dashboard = root / "src/kiro_crew/dashboard"
    owners = sorted((dashboard / "messaging_api").glob("[!_]*.py"))
    assert owners, "the messaging_api owners were not found"
    files = [dashboard / "handlers" / "messaging.py", *owners]
    for name in holders:
        held = pathlib.Path(inspect.unwrap(getattr(messaging, name)).__code__.co_filename)
        assert held.parts[-2:] in {path.parts[-2:] for path in files}, (name, held)
    return files


class TestADismissalHoldsOnBothReaders:
    """A dismissal has to hold on every durable reader, not just one.

    There were two folder readers and they were reported separately: the WS
    reconnect replay and the REST listing. Each excluded only the ids the LIVE
    manager held, so a delete that left the folder behind came back on the next
    reconnect. The filter's position -- inside the shared helper rather than at
    either call site -- is what let one store answer for both, and that position
    is pinned structurally below.

    One of those two readers is gone: the replay folds the crew log, where a
    dismissal is an ENTRY rather than a record in a store beside it. That is not a
    softening of this class's rule but the same rule applied one level up -- the
    folder store could only be keyed on the folder, so a dismissal in it did not
    outlive the thing it was suppressing. What remains here is the listing, which
    reads folders because it serves the run's own output text, and the ratchet that
    keeps the replay from quietly becoming a second folder reader again.
    """

    def request(self, state, caller: str, app: str = "", *, internal: bool = True):
        headers = {"X-Session-Key": caller}
        values: dict[str, object] = {"internal_auth": internal}
        if app:
            values["app"] = app
        return SimpleNamespace(
            app={"state": state},
            headers=headers,
            query={},
            get=lambda key, default=None: values.get(key, default),
        )

    def state(self, slot_app: str = ""):
        manager = SimpleNamespace(all_agents=[])
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(subagents=manager, _slots=slots, get_slot=slots.get)

    def run_listing(self, monkeypatch, caller: str = "dashboard:chat-1"):
        """Drive ``GET /api/spawn`` end to end and return its agent ids."""

        async def fake_scope(request, op):
            return object(), None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(self.state(), caller))
        )
        body = json.loads(payload.text or "{}")
        return [entry["id"] for entry in body["agents"]]

    def replay_records(self, seen: set[str] | None = None):
        """The LISTING's query with a durable rebuild's own bounds.

        The WS replay asks a different source entirely: it folds the crew log,
        where a dismissal is an entry rather than a record in a store beside it.
        These bounds are kept because the listing applies them too, and because
        the keep/age pair is what makes the question a durable rebuild's rather
        than a generic read.
        """
        return read_panel_records(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
            exclude_ids=seen or set(),
        ).records

    def test_a_dismissed_run_is_absent_from_the_rest_listing(self, agent_root, monkeypatch):
        write_record(agent_root, "keep11", parent_session="dashboard:chat-1")
        write_record(agent_root, "gone11", parent_session="dashboard:chat-1")
        # Control: both are listed before the dismissal, so absence below is the
        # dismissal doing it rather than the record never having been admitted.
        assert sorted(self.run_listing(monkeypatch)) == ["gone11", "keep11"]

        assert record_panel_dismissal("gone11") is True

        assert self.run_listing(monkeypatch) == ["keep11"]

    def test_a_dismissed_run_is_absent_from_the_durable_folder_read(self, agent_root):
        """The reader that actually resurrected the card, asked its own question.

        The WS replay's half of this moved with the reader: it folds the crew log
        now, and ``test_subagent_panel_fold_replay.py`` pins a dismissal there --
        including the case this store cannot answer at all, a run whose folder was
        already reclaimed.
        """
        write_record(agent_root, "keep22", parent_session="dashboard:chat-1")
        write_record(agent_root, "gone22", parent_session="dashboard:chat-1")
        assert sorted(ids(self.replay_records())) == ["gone22", "keep22"]

        assert record_panel_dismissal("gone22") is True

        surviving = self.replay_records()
        assert ids(surviving) == ["keep22"]
        # And nothing downstream can put it back: the frames the listing's records
        # would build are built from these alone.
        frames = [
            build_persisted_subagent_frame(record, redact=lambda text: text) for record in surviving
        ]
        assert [frame["data"]["id"] for frame in frames] == ["keep22"]

    def test_the_folder_read_still_honours_its_live_exclusions(self, agent_root):
        """Control on the shape: the dismissal filter is added to that set, not swapped in."""
        write_record(agent_root, "live33", parent_session="dashboard:chat-1")
        write_record(agent_root, "gone33", parent_session="dashboard:chat-1")
        write_record(agent_root, "keep33", parent_session="dashboard:chat-1")
        record_panel_dismissal("gone33")
        assert ids(self.replay_records(seen={"live33"})) == ["keep33"]

    def test_the_filter_lives_inside_the_shared_reader_not_at_a_call_site(self):
        """Why one store answers for both readers -- pinned, not assumed.

        Filtering at the two call sites would read the same on the day it is
        written and would leave the next reader uncovered. The filter is inside
        :func:`read_panel_records`, so a third reader inherits it by construction.
        """
        import ast

        root = pathlib.Path(__file__).resolve().parents[1]
        tree = ast.parse(
            (root / "src/kiro_crew/subagent_persistence.py").read_text(encoding="utf-8")
        )
        reader = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "read_panel_records"
        )
        called = {
            getattr(call.func, "id", "") or getattr(call.func, "attr", "")
            for call in ast.walk(reader)
            if isinstance(call, ast.Call)
        }
        assert "panel_dismissal_recorded" in called, (
            "read_panel_records does not consult the dismissal store, so each reader "
            "would have to filter for itself and the next one added would not"
        )
        # And it consults it per CANDIDATE, never by materialising the whole set:
        # this function's contract is a bounded working set, and enumerating every
        # record would let the registry's size decide its memory.
        assert "dismissed_panel_ids" not in called, (
            "read_panel_records materialises every dismissal id, so its retained set "
            "grows with the registry inside the one function bounded on purpose"
        )

    def test_each_durable_source_is_reached_through_the_reader_that_filters_it(self):
        """Neither reader may assemble records itself and skip the dismissal filter.

        Matched on a word boundary, because ``_panel_record`` is a SUBSTRING of
        ``read_panel_records`` -- a plain ``in`` test reports a correct reader as an
        offender, and the failure reads exactly like a real bypass.

        Both files read folders, and both must do it through the shared helper that
        consults the dismissal registry. The WS replay reads them on ONE path only,
        the one taken when the crew log is switched off -- where there is no fold,
        so the registry is the only dismissal record there is. With the log on it
        folds instead, and the dismissal is an entry in that same log; the gate
        between those two is pinned below, because a folder read reached with the
        log ON would honour a registry entry while ignoring the log entry that
        supersedes it.
        """
        import re

        builder = re.compile(r"(?<![A-Za-z0-9_])_panel_record(?![A-Za-z0-9_])")
        root = pathlib.Path(__file__).resolve().parents[1]
        for relative in (
            "src/kiro_crew/dashboard/handlers/messaging.py",
            "src/kiro_crew/dashboard/ws.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            if relative.endswith("handlers/messaging.py"):
                # The listing reader runs from a messaging_api owner, so this reader
                # is the facade and every owner it composes, read together.
                source = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in _messaging_handler_files(root, "api_spawn_list")
                )
            # Control: this reader really is one, so the assertion below cannot pass
            # by matching a file that stopped reading records altogether.
            assert "read_panel_records" in source, f"{relative} no longer reads persisted records"
            assert not builder.search(source), (
                f"{relative} reaches the per-folder record builder directly, which "
                "skips the dismissal filter that read_panel_records applies"
            )
        # Control on the needle itself: it must match the real thing somewhere, or
        # the assertions above pass because the pattern is broken.
        owner = (root / "src/kiro_crew/subagent_persistence.py").read_text(encoding="utf-8")
        assert builder.search(owner), "the _panel_record needle matches nothing anywhere"

    def test_the_replay_folds_the_log_when_it_is_on_and_reads_folders_when_it_is_not(self):
        """The gate that keeps one dismissal record authoritative at a time.

        Asserted on the source because the alternative is a live socket: the branch
        sits inside the reconnect handler, which needs a real aiohttp WebSocket, and
        the two readers it chooses between are each pinned on their own elsewhere.

        What is pinned is which reader each branch REACHES, not how it is called.
        A reader handed to :func:`asyncio.to_thread` is NAMED rather than called,
        and the log-on branch names its own worker there, so a pattern expecting
        the reader as the thread's first argument describes one spelling of the
        call rather than the gate.
        """
        import ast

        root = pathlib.Path(__file__).resolve().parents[1]
        source = (root / "src/kiro_crew/dashboard/ws.py").read_text(encoding="utf-8")

        def _names(body: list[ast.stmt]) -> set[str]:
            """Every name this code reaches: bare, called, handed over, or attribute.

            Attributes count because a value read off a returned record -- the
            folder scan's own ``overflow_is_lower_bound`` -- is reached by
            attribute and not by name.
            """
            module = ast.Module(body=body, type_ignores=[])
            found = set()
            for sub in ast.walk(module):
                if isinstance(sub, ast.Name):
                    found.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    found.add(sub.attr)
            return found

        tree = ast.parse(source)
        gates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Name)
            and node.test.func.id == "crew_log_enabled"
            and node.orelse
            and "read_panel_records" in _names(node.orelse)
        ]
        assert len(gates) == 1, (
            "the replay's folder read is no longer the crew-log-off branch of one "
            "crew_log_enabled() gate, so a folder record could be replayed while "
            "the log holds the dismissal"
        )
        gate = gates[0]
        on_branch = _names(gate.body)
        off_branch = _names(gate.orelse)
        assert (
            "read_fold_subagent_records" in on_branch
        ), "the replay no longer folds the crew log behind crew_log_enabled()"
        # Each branch reaches ONE reader. A branch reaching both would draw a card
        # from the folders while the log holds its dismissal, which is the whole
        # point of choosing between them.
        assert "read_panel_records" not in on_branch
        assert "read_fold_subagent_records" not in off_branch
        # The log-on branch corrects the legacy registry's dismissals into the log
        # before it folds. Without that a card dismissed before the log held
        # dismissals comes back, which is the failure this reader exists to end.
        assert "backfill_legacy_panel_dismissals" in on_branch
        # And what it corrected is excluded from THIS read too. The append is
        # queued, so the fold that follows it may not carry it yet -- a branch that
        # passed only the live ids would draw the cleared card once, on the very
        # reconnect that corrected it. Pinned through the NAME the backfill's
        # result is bound to, so dropping the union is caught however it is spelt.
        bound = [
            target.id
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.Assign)
            and "backfill_legacy_panel_dismissals" in _names([ast.Expr(value=node.value)])
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        assert len(bound) == 1, "the backfill's result is no longer bound to one name"
        excluded = [
            node
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.keyword)
            and node.arg == "exclude_ids"
            and bound[0] in _names([ast.Expr(value=node.value)])
        ]
        assert len(excluded) == 1, (
            "the fold read's exclude_ids no longer carries what the backfill "
            f"corrected ({bound[0]}), so a corrected card is drawn once"
        )

        # Every blocking read sits INSIDE the worker handed to asyncio.to_thread.
        # Each of these lists the crew-log root, stats it, and on a cold cache
        # opens a header file per unit, so one left in the branch body is a
        # filesystem walk per slot on the loop that carries chat and heartbeats.
        blocking = {
            "crew_log_panel_units",
            "backfill_legacy_panel_dismissals",
            "read_fold_subagent_records",
        }
        workers = [
            node
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.FunctionDef)
        ]
        assert len(workers) == 1, "the log-on branch no longer has exactly one worker"
        inside = _names(workers[0].body)
        assert blocking <= inside, f"not every blocking read is in the worker: {blocking - inside}"
        handed = [
            node
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_thread"
            and workers[0].name in _names([ast.Expr(value=arg) for arg in node.args])
        ]
        assert len(handed) == 1, "the worker is no longer the one thing handed to to_thread"
        # And none of them is ALSO reached from the branch body outside it, which
        # is how the walk creeps back onto the loop while the worker still exists.
        on_loop = _names([stmt for stmt in gate.body if not isinstance(stmt, ast.FunctionDef)])
        assert not (blocking & on_loop), f"a blocking read runs on the loop: {blocking & on_loop}"

        # The folder scan has its own candidate window, and a window that FILLED
        # means admissible runs past it were never inspected. The count cannot
        # describe them -- a window full of records this socket then rejected
        # reports an overflow of zero while older eligible runs went unseen -- so
        # the flag has to travel with the records and reach the warning.
        assert "overflow_is_lower_bound" in off_branch, (
            "the crew-log-off fallback drops overflow_is_lower_bound, so a "
            "saturated scan reads as a complete replay"
        )
        enclosing = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and any(child is gate for child in ast.walk(node))
        ]
        assert enclosing, "the gate has no enclosing function"
        handler = min(enclosing, key=lambda node: len(list(ast.walk(node))))
        warnings = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.BoolOp)
            and isinstance(node.test.op, ast.Or)
            and any(
                isinstance(call, ast.Attribute) and call.attr == "warning"
                for call in ast.walk(ast.Module(body=node.body, type_ignores=[]))
            )
        ]
        assert len(warnings) == 1, "the replay's truncation warning is no longer one or-gated if"
        # Gated on EITHER, so a saturated window reports even at a count of zero.
        assert len(_names(warnings[0].test.values)) >= 2
        # Control on the needles: both readers must be reached somewhere in the
        # module, or the assertions above pass because the names are wrong.
        whole = _names(tree.body)
        assert {"read_fold_subagent_records", "read_panel_records"} <= whole


class TestTheDismissalStoreIsRegisteredEverywhereItMustBe:
    """The store's protection is four registrations, and a rename breaks them silently.

    ``record_panel_dismissal`` writes an OWNER decision about what the panel hides,
    so no sandboxed process may forge or unlink one. ``trust/`` cannot host such a
    record: it is a declared sandbox READ-WRITE exception, so a record under it
    stays writable by a command that builds the path at runtime, which command
    matching cannot catch. The leaf therefore lives at the data-home root and is
    registered in four places. Nothing in the code fails when one is missing -- it
    just leaves the store unprotected -- so each is pinned by name here.
    """

    def test_the_leaf_is_not_under_trust(self):
        parts = subagent_persistence._panel_dismissals_dir().parts
        assert "trust" not in parts
        assert parts[-1] == subagent_persistence._PANEL_DISMISSAL_LEAF

    def test_the_leaf_is_a_direct_child_of_the_data_home(self):
        """A nested leaf would not be covered: every list keys on a top-level name."""
        from kiro_crew.subagent_persistence import _subagents_dir

        assert subagent_persistence._panel_dismissals_dir().parent == _subagents_dir().parent

    def test_the_leaf_is_on_the_file_gates_sensitive_floor(self):
        """Agent file tools must neither read nor write the store."""
        from kiro_crew.security import paths as security_paths

        floor = security_paths._CREW_SECRET_LEAVES
        # Control: an empty or renamed list would make the assertion below pass
        # for the wrong reason, so pin a sibling owner-authority leaf too.
        assert "crew-teams" in floor, "not the floor list this test means to read"
        assert subagent_persistence._PANEL_DISMISSAL_LEAF in floor

    def test_the_leaf_is_bind_masked_in_the_sandbox(self):
        from kiro_crew import sandbox

        assert subagent_persistence._PANEL_DISMISSAL_LEAF in sandbox._CREW_HIDDEN_LEAVES

    def test_the_leaf_is_precreated_so_the_mask_is_not_skipped(self):
        """Created on the first dismissal, so an absent name would skip the bind."""
        from kiro_crew import sandbox

        assert (
            subagent_persistence._PANEL_DISMISSAL_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        )

    def test_a_symlink_at_the_leaf_is_refused(self):
        """A link attaches the mask to the target and leaves the name writable."""
        from kiro_crew import sandbox

        assert subagent_persistence._PANEL_DISMISSAL_LEAF in sandbox._CREW_NO_ALIAS_LEAVES


class TestThePersistedGrantIsRecordedNotJustTheRefusals:
    """The ownership decision leaves a record whichever way it goes.

    This replay writes to the socket directly, so ``ws_event_allowed`` never sees
    the frame, and a dashboard user short-circuits ``_ws_client_allowed``
    unconditionally -- the two places a grant would otherwise be recorded. With
    only the deny sites audited, the SEL stream cannot distinguish a rebuild that
    delivered a run from one that never considered it.

    Structural rather than behavioural: the decision sits inside the
    ``subscribe_subagents`` handler, and what can regress is the audit call being
    deleted, which reading the source catches exactly.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    REL = "src/kiro_crew/dashboard/ws.py"

    def block(self) -> list:
        """The INNERMOST statement block enclosing the persisted-frame append.

        Innermost matters and is the whole reason this helper is not two lines:
        ``ws.py`` already calls ``_audit_grant_quietly`` at three unrelated sites,
        so any outer block -- the handler body, the module -- contains one of those
        and would satisfy this pin no matter what the append site does. The
        mutation matrix caught exactly that, so the block is chosen as the one
        whose own enclosing statement spans the fewest lines.
        """
        import ast

        tree = ast.parse((self.ROOT / self.REL).read_text(encoding="utf-8"))

        def holds_append(node) -> bool:
            return any(
                isinstance(c, ast.Call)
                and getattr(c.func, "id", "") == "build_persisted_subagent_frame"
                for c in ast.walk(node)
            )

        best: tuple[int, list] | None = None
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for stmt in body:
                if not holds_append(stmt):
                    continue
                span = getattr(stmt, "end_lineno", stmt.lineno) - stmt.lineno
                if best is None or span < best[0]:
                    best = (span, body)
        if best is None:
            raise AssertionError(
                f"{self.REL} no longer appends a persisted frame -- this pin names the wrong site"
            )
        return best[1]

    def names_called(self, body: list) -> list[str]:
        import ast

        out: list[str] = []
        for stmt in body:
            for call in ast.walk(stmt):
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", "") or getattr(call.func, "attr", "")
                    if name:
                        out.append(name)
        return out

    def test_the_append_block_records_the_grant(self):
        called = self.names_called(self.block())
        # Control: the block really is the one that appends the frame, so an
        # empty or wrong match cannot pass this as a success.
        assert "build_persisted_subagent_frame" in called
        # Control: and it is the TIGHT block. ws.py holds three unrelated
        # ``_audit_grant_quietly`` calls, so a block wide enough to reach them
        # would pass this pin whatever the append site did.
        assert "persisted_replay_denial_reason" in called, called
        assert called.count("_audit_grant_quietly") <= 1, called
        assert "_audit_grant_quietly" in called, (
            "the persisted frame is appended with no grant recorded; its refusals "
            "are audited, so only the deny side would appear in the SEL stream"
        )

    def test_the_grant_is_recorded_before_the_append(self):
        called = self.names_called(self.block())
        assert called.index("_audit_grant_quietly") < called.index("build_persisted_subagent_frame")

    def test_the_grant_names_the_same_auditee_as_the_refusals(self):
        """A grant labelled differently from its own denies cannot be correlated.

        ``_audit_grant_quietly`` falls back to ``<unknown>`` while both refusals
        beside it record ``<dashboard>``, so passing a bare ``ws_app`` here would
        file the two halves of ONE ownership decision under two subjects.
        """
        import ast

        args: list[str] = []
        for stmt in self.block():
            for call in ast.walk(stmt):
                if not isinstance(call, ast.Call):
                    continue
                if getattr(call.func, "id", "") != "_audit_grant_quietly":
                    continue
                assert call.args, "the grant records no auditee at all"
                args.append(ast.unparse(call.args[0]).replace("'", '"'))
        # Control: exactly one grant in the tight block, so an empty list cannot
        # pass this as a success.
        assert len(args) == 1, args
        assert args[0] == 'ws_app or "<dashboard>"', args

    def test_both_refusal_sites_are_still_audited(self):
        """The grant must be added beside the denies, never instead of them."""
        source = (self.ROOT / self.REL).read_text(encoding="utf-8")
        assert source.count('_audit_deny(ws_app or "<dashboard>", "subagent_done"') == 2

    def test_the_rest_listing_records_the_same_grant(self):
        """One ownership decision, so the trail cannot depend on which reader asked."""
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in _messaging_handler_files(self.ROOT, "api_spawn_list")
        )
        assert '_audit_allow(auditee, "api_spawn_list")' in source


class _DeleteRequest:
    """Request double for the delete route, with a real ``in`` for the app claim.

    ``app=None`` means the key is ABSENT, which is a different state from an empty
    claim and is the one the route refuses -- so the double has to be able to
    produce both, and an instance attribute cannot: ``in`` dispatches on the type.
    """

    def __init__(self, agent_id: str, app: object = "", slots: dict | None = None) -> None:
        self.app = {"state": SimpleNamespace(subagents=None, _slots=slots or {})}
        self.headers: dict[str, str] = {}
        self.match_info = {"agent_id": agent_id}
        self.query: dict[str, str] = {}
        self._extra = {} if app is None else {"app": app}

    def __contains__(self, key: str) -> bool:
        return key in self._extra

    def __getitem__(self, key: str):
        return self._extra[key]

    def get(self, key: str, default=None):
        return self._extra.get(key, default)


class TestACardRebuiltFromDiskCanBeDismissed:
    """After a restart every finished card has no manager entry.

    ``DELETE /api/spawn/{id}`` answered 404 for exactly those ids, so the one
    control the user has over a rebuilt card refused, showed the failure banner,
    and the next reconnect sent the card again. The route records the dismissal
    against the folder instead.
    """

    def request(self, agent_id: str, app: object = ""):
        return _DeleteRequest(agent_id, app)

    def delete(self, agent_id: str, app: object = ""):
        response = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_delete(self.request(agent_id, app))
        )
        return response.status, json.loads(response.text or "{}")

    def test_a_persisted_run_is_dismissed_and_stays_dismissed(self, agent_root, monkeypatch):
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        write_record(agent_root, "ondisk1")
        status, body = self.delete("ondisk1")
        assert (status, body["dismissed"], body["cancelled"]) == (200, True, False)
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_an_unknown_id_is_still_not_found(self, agent_root, monkeypatch):
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        assert self.delete("never-ran")[0] == 404
        assert dismissed_panel_ids() == frozenset()

    def test_an_app_token_cannot_dismiss_a_run(self, agent_root, monkeypatch):
        """No caller gains reach over a run it could not list."""
        denied: list[dict] = []
        monkeypatch.setattr(
            messaging,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: denied.append(kw)),
        )
        write_record(agent_root, "ondisk2", app="appA")
        status, body = self.delete("ondisk2", app="appA")
        assert (status, body["code"]) == (403, "app_token_forbidden")
        assert dismissed_panel_ids() == frozenset()
        assert [entry["outcome"] for entry in denied] == ["denied"]

    def test_an_absent_app_claim_is_refused_rather_than_trusted(self, agent_root, monkeypatch):
        """Absent means the request never passed the auth middleware."""
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        write_record(agent_root, "ondisk3")
        assert self.delete("ondisk3", app=None)[0] == 403
        assert dismissed_panel_ids() == frozenset()

    def test_the_dismissal_is_audited(self, agent_root, monkeypatch):
        recorded: list[dict] = []
        monkeypatch.setattr(
            messaging,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: recorded.append(kw)),
        )
        write_record(agent_root, "ondisk4")
        self.delete("ondisk4")
        assert [(e["operation"], e["outcome"]) for e in recorded] == [("spawn.dismiss", "allowed")]


class TestTheRouteRecordsTheDismissalInTheOwningSessionsLog:
    """The record the PANEL reads, written by the route that accepts the dismissal.

    The folder registry below it is kept for ``GET /api/spawn``, which reads the
    folders for the run's own output text. This half is what keeps the card gone,
    because the panel's durable source is a fold of the session's crew log.
    """

    def delete(self, agent_id: str, slots: dict | None = None):
        response = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_delete(_DeleteRequest(agent_id, "", slots))
        )
        return response.status, json.loads(response.text or "{}")

    @pytest.fixture(autouse=True)
    def _crew_log_on(self, monkeypatch):
        """The suite runs with the crew log OFF, so a test opts in.

        This class is about the entry the route writes, so the record has to be
        switched on for it. The opt-OUT is behaviour of its own and is pinned by
        the last test here rather than left to this fixture's absence.
        """
        monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
        yield

    def _session_with_child(self, unit_id: str, agent_id: str, slot: str = "chat-1"):
        """One crew-log UNIT holding a finished child, with *slot* in its header.

        The unit id is an ACP session id and is deliberately unlike the slot and
        its session key: the route resolves the unit by searching the units whose
        headers name the slot, which a unit id that happened to BE the session key
        would not exercise.
        """
        from kiro_crew import crew_log as lg
        from kiro_crew.crew_log import CrewLog

        handle = CrewLog.create(
            lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot
        )
        handle.append("subagent/spawned", {"agent_id": agent_id}, src="gateway")
        handle.append("subagent/completed", {"agent_id": agent_id, "ms": 5}, src="gateway")
        return handle

    def _drawn(self, unit_id: str) -> list[str]:
        from kiro_crew.crew_log import emit
        from kiro_crew.crew_log import projection as crew_log

        # The emitter QUEUES its appends, so the route's entry has to reach the file
        # before the fold is read: an unflushed write would leave the row drawn and
        # read as a dismissal that did not work, or -- worse, in the other
        # direction -- let a later flush make a failing assertion pass.
        emit.flush()
        value = crew_log.fold_session(unit_id, names=("subagents",)).projection("subagents").value
        return sorted(value["by_id"])

    def test_a_run_with_no_folder_is_dismissed_through_the_log(self, agent_root, monkeypatch):
        """The case the folder registry cannot answer at all.

        It refuses to record without a folder, which is the state every run reaches
        once its folder is reclaimed -- and the log still carries the child, so the
        card came back. The route writes the entry, and the fold stops drawing it.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-nf", "nofolder1")
        assert self._drawn("acp-nf") == ["nofolder1"]

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("nofolder1", slots)
        assert (status, body["dismissed"]) == (200, True)
        assert self._drawn("acp-nf") == []

    def test_an_id_no_log_and_no_folder_knows_is_still_not_found(self, agent_root, monkeypatch):
        """Neither record can be written, so there is no run here to speak of."""
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-other", "other1")
        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        assert self.delete("never-ran", slots)[0] == 404

    def test_a_child_of_no_live_slots_session_is_not_resolved(self, agent_root, monkeypatch):
        """The resolver is bounded by the live slots, like the panel read.

        A child whose session is not among them is not one the panel could be
        drawing from the log either, so there is no folded card to clear.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-offstage", "offstage1", slot="chat-9")
        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        assert self.delete("offstage1", slots)[0] == 404
        assert self._drawn("acp-offstage") == ["offstage1"]

    def test_an_unwritable_folder_registry_is_reported_even_when_the_log_arm_wrote(
        self, agent_root, monkeypatch
    ):
        """One reader honours the dismissal and the other does not, so it is partial.

        ``GET /api/spawn`` reads the folders, so an unwritable registry leaves that
        listing offering the run however well the log arm did. Answering ok claims
        a dismissal one reader goes on contradicting, with nothing anywhere saying
        the write failed -- so the caller cannot retry the half that did not land.
        """
        denied: list[dict] = []
        monkeypatch.setattr(
            messaging,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: denied.append(kw)),
        )
        self._session_with_child("acp-partial", "partial1")
        monkeypatch.setattr(
            messaging,
            "record_panel_dismissal_outcome",
            lambda agent_id: subagent_persistence.DISMISSAL_FAILED,
        )

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("partial1", slots)
        assert (status, body["code"]) == (503, "dismissal_unwritable")
        assert [(e["operation"], e["outcome"]) for e in denied] == [("spawn.dismiss", "denied")]
        # And the log arm's own write still happened: the half that CAN land does,
        # so a retry has less to do rather than more.
        assert self._drawn("acp-partial") == []

    def test_an_append_that_never_commits_is_not_published_as_a_dismissal(
        self, agent_root, monkeypatch
    ):
        """The queue returns on handover, so handover is not the record.

        With the run's folder already reclaimed this entry is the ONLY record of
        the dismissal. Reporting success on the handover tells the user the card is
        gone and lets the next reconnect bring it back, with nothing left to
        explain it and nothing for a retry to act on.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-drop", "dropped1")

        def _never_commits(unit, *, agent_id, on_settled=None):
            """Queued, then given up on -- the writer's permanent-drop outcome."""
            if on_settled is not None:
                on_settled(False)

        monkeypatch.setattr(messaging_crew_log_emit(), "on_subagent_dismissed", _never_commits)
        monkeypatch.setattr(messaging_crew_log_emit(), "dismiss_child", lambda agent_id, **_: "")

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("dropped1", slots)
        # 503, not 404: a unit holds this child, so the run exists. A 404 would say
        # it never did, which is the answer that makes the card undismissable.
        assert (status, body["code"]) == (503, "dismissal_unwritable")
        assert self._drawn("acp-drop") == ["dropped1"]

    def test_a_committed_append_is_published(self, agent_root, monkeypatch):
        """Control: the gate must not refuse the dismissal that did land."""
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-ok", "ok1")
        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}

        status, body = self.delete("ok1", slots)
        assert (status, body["dismissed"]) == (200, True)
        assert self._drawn("acp-ok") == []

    def test_a_failed_unit_search_answers_retryable_not_not_found(self, agent_root, monkeypatch):
        """ABSENT would become a 404 or a plain success, both of them wrong.

        The run exists as far as anyone knows; the store simply would not say where
        its row is. A 404 tells the caller the run never existed, and a success
        tells it the card is cleared, so the only honest answer is retryable.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        from kiro_crew.crew_log import resolve as crew_log_resolve

        self._session_with_child("acp-nosay", "nosay1")

        def _boom(*_a, **_kw):
            raise crew_log_resolve.UnitSearchFailed("the store is unreadable")

        monkeypatch.setattr(crew_log_resolve, "unit_holding_child", _boom)
        monkeypatch.setattr(messaging_crew_log_emit(), "dismiss_child", lambda agent_id, **_: "")

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("nosay1", slots)
        assert (status, body["code"]) == (503, "dismissal_unwritable")

    def test_an_unexpected_fault_in_the_log_arm_answers_retryable(self, agent_root, monkeypatch):
        """The catch-all is a store fault too, so it cannot read as "nothing owed".

        Whatever reaches it is an emitter or store problem, and ABSENT would become
        a 404 for a run that exists or a plain success for a card still drawn. The
        one case that genuinely owes no record, a switched-off emitter, is decided
        before anything here can fail.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-fault", "fault1")

        def _boom(*_a, **_kw):
            raise ValueError("something nobody anticipated")

        monkeypatch.setattr(messaging_crew_log_emit(), "dismiss_child", _boom)

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("fault1", slots)
        assert (status, body["code"]) == (503, "dismissal_unwritable")

    def test_with_the_crew_log_switched_off_the_folder_record_is_all_there_is(
        self, agent_root, monkeypatch
    ):
        """An install that opted out of the record has no record to write to.

        The folder registry still answers on its own terms, which is what the
        listing endpoint reads anyway -- so the opt-out costs the panel its durable
        half rather than breaking the route.
        """
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        write_record(agent_root, "withfolder1")
        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("withfolder1", slots)
        assert (status, body["dismissed"]) == (200, True)
        assert dismissed_panel_ids() == frozenset({"withfolder1"})


class TestTheLiveManagerPathRecordsTheDismissalToo:
    """The dismiss route has TWO arms, and both have to reach the same records.

    A finished child the manager still holds goes through
    ``settle_before_delete``; one the manager has already dropped goes through the
    route's own durable arm. The panel folds the crew log for both, so an arm that
    writes only the folder registry clears the card from live state and leaves the
    fold still
    offering it -- the card returns on the very next reconnect, inside the same
    process, with the user having been told the dismissal worked.
    """

    @pytest.fixture(autouse=True)
    def _crew_log_on(self, monkeypatch):
        """The crew log on, and the emitter's spawn pin clear.

        ``_child_origin`` is a module global that outlives one test, so a pin left
        by an earlier test would decide a later one -- and the two cases here
        differ by exactly whether a pin exists. Cleared both sides, so neither
        order hides a failure.
        """
        from kiro_crew.crew_log import emit

        monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
        emit.forget_child_origin("live1")
        yield
        emit.forget_child_origin("live1")

    def manager(self):
        return SimpleNamespace(
            _agents={
                "live1": SimpleNamespace(
                    id="live1",
                    _ending_claimed=False,
                    done=True,
                    parent_session_key="dashboard:chat-1",
                )
            },
            _tasks={"live1": object()},
            _report_owners={},
        )

    def settle(self, manager):
        import kiro_crew.subagent as subagent_module

        return asyncio.run(subagent_module.SubagentManager.settle_before_delete(manager, "live1"))

    def _session_with_child(self, unit_id: str, agent_id: str, slot: str = "chat-1"):
        """A dispatched child, closed through the REAL terminal report.

        The terminal report is what releases the emitter's spawn pin, in a
        ``finally`` that always runs -- so by the time a finished child reaches
        ``settle_before_delete`` there is no pin, and a test that closed the child
        by calling ``on_subagent_completed`` directly leaves one standing and
        proves nothing about production. Driving the real reporter is what makes
        this the live arm's own case.
        """
        from kiro_crew import crew_log as lg
        from kiro_crew.crew_log import CrewLog, emit
        from kiro_crew.subagent_manager.terminal import TerminalCoordinator

        CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)
        emit.remember_child_origin(agent_id, unit_id, 1)
        sid, turn = emit.open_child_origin(agent_id)
        emit.on_subagent_spawned(sid, turn, agent_id=agent_id)
        info = SimpleNamespace(
            id=agent_id,
            outcome="completed",
            error=None,
            user_stopped=False,
            elapsed=0.005,
            credits=0.0,
        )
        TerminalCoordinator._record_crew_log_terminal(SimpleNamespace(), info)
        assert emit.flush()
        assert emit.child_origin(agent_id) == ("", 0), (
            "the terminal report left the spawn pin standing, so this test is not the "
            "state a finished child is actually in"
        )

    def _drawn(self, unit_id: str) -> list[str]:
        from kiro_crew.crew_log import emit
        from kiro_crew.crew_log import projection as crew_log

        assert emit.flush()
        value = crew_log.fold_session(unit_id, names=("subagents",)).projection("subagents").value
        return sorted(value["by_id"])

    def test_the_fold_stops_offering_a_card_the_live_arm_dismissed(self, agent_root, monkeypatch):
        self._session_with_child("acp-live", "live1")
        write_record(agent_root, "live1")
        assert self._drawn("acp-live") == ["live1"]

        manager = self.manager()
        assert self.settle(manager) == "delivered"
        assert "live1" not in manager._agents
        assert self._drawn("acp-live") == []
        # And the folder half is written too, so the listing endpoint agrees.
        assert dismissed_panel_ids() == frozenset({"live1"})

    def test_a_child_no_unit_holds_does_not_hold_the_pop(self, agent_root, monkeypatch):
        """Nothing to suppress is not a failure to suppress.

        A child whose dispatch this slot's logs never recorded has no row for the
        fold to offer, so there is no folded card to clear -- and holding the pop
        on it would answer 409 for every such dismissal, which is undismissable
        rather than safe. The folder record still answers for the listing.
        """
        write_record(agent_root, "live1")  # a folder, and no unit holding the row
        manager = self.manager()
        assert self.settle(manager) == "delivered"
        assert "live1" not in manager._agents
        assert dismissed_panel_ids() == frozenset({"live1"})

    def test_an_unstorable_folder_record_still_holds_the_pop(self, agent_root, monkeypatch):
        """The half that CAN fail still gates the publish.

        The folder write is a synchronous file write and can genuinely fail, and
        the listing reads that record -- so a pop on a failed write reports a
        dismissal the listing will not honour.
        """
        import kiro_crew.subagent as subagent_module

        self._session_with_child("acp-live", "live1")
        monkeypatch.setattr(
            subagent_module,
            "record_panel_dismissal_outcome",
            lambda agent_id: subagent_persistence.DISMISSAL_FAILED,
        )
        manager = self.manager()
        assert self.settle(manager) == "pending"
        assert "live1" in manager._agents

    def test_an_append_that_never_commits_holds_the_pop(self, agent_root, monkeypatch):
        """The pop is the publish, on this arm as much as on the route's.

        The writer can refuse the entry at its memory ceiling, and the queue has
        already returned by then. A pop on that reports a dismissal to the route as
        200 and leaves the fold still offering the card, so it comes back on the
        very next reconnect -- which is the same published-too-early failure the
        folder write above this is deliberately ordered to avoid.
        """
        self._session_with_child("acp-live-drop", "live1")
        write_record(agent_root, "live1")

        def _never_commits(unit, *, agent_id, on_settled=None):
            if on_settled is not None:
                on_settled(False)

        monkeypatch.setattr(messaging_crew_log_emit(), "on_subagent_dismissed", _never_commits)
        manager = self.manager()
        assert self.settle(manager) == "pending"
        assert "live1" in manager._agents
        assert "live1" in manager._tasks
        # And the folder half was never written either, so a retry starts clean
        # rather than from a half-recorded dismissal.
        assert dismissed_panel_ids() == frozenset()

    def test_a_failed_unit_search_holds_the_pop(self, agent_root, monkeypatch):
        """A store that would not say is not a store that said no.

        Popping on it reports a dismissal that was never looked for, and the card
        returns once the store recovers -- with the user having been told it worked.
        """
        from kiro_crew.crew_log import resolve as crew_log_resolve

        self._session_with_child("acp-live-search", "live1")
        write_record(agent_root, "live1")

        def _boom(*_a, **_kw):
            raise crew_log_resolve.UnitSearchFailed("the store is unreadable")

        monkeypatch.setattr(crew_log_resolve, "unit_holding_child", _boom)
        manager = self.manager()
        assert self.settle(manager) == "pending"
        assert "live1" in manager._agents
        assert dismissed_panel_ids() == frozenset()

    def test_with_the_crew_log_off_the_live_arm_still_dismisses(self, agent_root, monkeypatch):
        """The opt-out must not make every managed card undismissable."""
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        write_record(agent_root, "live1")
        manager = self.manager()
        assert self.settle(manager) == "delivered"
        assert "live1" not in manager._agents
        assert dismissed_panel_ids() == frozenset({"live1"})
