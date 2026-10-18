"""The agent's crew-log door: the listing walk, caller classes and the workspace boundary.

A listing is filtered to the caller's dispatch tree; its candidate walk keeps no more
than its bound and orders units by their log's write time, not their directory's; the
retention listing behind a lost-front refusal runs only off the event loop. The inline
caller-class refusal refuses every state ``chat_runner._crew_log_class`` publishes, and a
dispatch grant stops at a workspace boundary on the per-unit door and on every lineage
row of a listing. Shared fixtures and builders come from ``crew_log_routes_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from crew_log_routes_helpers import _fresh_dispatch_scan  # noqa: F401
from crew_log_routes_helpers import _is_owner  # noqa: F401
from crew_log_routes_helpers import _isolated_home  # noqa: F401
from crew_log_routes_helpers import (
    CHILD_UNIT,
    CONDUCTOR_KEY,
    CONDUCTOR_SLOT,
    CONDUCTOR_UNIT,
    GRANDCHILD_UNIT,
    STRANGER_UNIT,
    _as_conductor,
    _class_moved,
    _dispatch_tree,
    _flag_on,
    _internal_request,
    _log,
    _opened,
    _Slot,
)

from kiro_crew import crew_log as lg
from kiro_crew.dashboard.handlers import crew_log as routes


class TestTheListingCarriesTheSameScope:
    """A listing enumerates units, so it is filtered rather than merely allowed."""

    def test_a_conductor_sees_its_own_tree_and_not_a_stranger(self, monkeypatch):
        """MUTATION-SENSITIVE: the listing filter.

        An unscoped listing would name every session on the host, which is the one
        leak a per-unit gate cannot refuse after the fact.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert listed == {CONDUCTOR_UNIT, CHILD_UNIT, GRANDCHILD_UNIT}

    def test_the_owner_sees_every_unit(self, monkeypatch):
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert STRANGER_UNIT in listed

    def test_an_excluded_class_sees_only_its_own_unit(self, monkeypatch):
        """Its own record, and nothing below it -- the listing form of the exclusion."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            "/api/crew-log/sessions",
            slots={"conductor": _Slot(linked_session_key="slack:C123")},
        )
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert listed == {CONDUCTOR_UNIT}

    def test_a_caller_with_no_unit_of_its_own_is_refused(self, monkeypatch):
        """Nothing to scope to, so there is no honest listing to return."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor("/api/crew-log/sessions", sessions={})
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert "no live crew log unit" in json.loads(response.text)["error"]


class TestTheListingBoundsWhatItRetains:
    """The count bound is applied while walking, not by the scan cap downstream.

    A host that has run many sessions is the ordinary case, not the adversarial
    one. If the walk retained every directory before the cap, the cost of one
    listing would scale with how many sessions the host had ever run, which is
    what the bound exists to prevent.
    """

    @staticmethod
    def _dirs(count: int, *, newest_first_names: bool = True) -> list[str]:
        """*count* session unit directories, each with a distinct write time.

        The stamp goes on the LOG, not the directory, because that is what the
        reader orders by -- a real unit is a directory holding ``log.jsonl``, and a
        directory with no log is not a unit at all.
        """
        from kiro_crew.crew_log import read as reader

        root = lg.crew_log_root(lg.KIND_SESSION)
        root.mkdir(parents=True, exist_ok=True)
        names = []
        for index in range(count):
            unit = root / f"s-unit-{index:04d}"
            unit.mkdir()
            log = unit / reader.LOG_FILE
            log.write_text("", encoding="utf-8")
            # Ascending write time, so the LAST created is the newest.
            os.utime(log, (1_700_000_000 + index, 1_700_000_000 + index))
            names.append(unit.name)
        assert reader  # the module under test is importable from here
        return names if newest_first_names else list(reversed(names))

    def test_only_the_newest_candidates_are_retained(self, monkeypatch):
        from kiro_crew.crew_log import read as reader

        names = self._dirs(7)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 3)
        kept, cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert [path.name for path in kept] == names[-3:][::-1]
        assert cut is True

    def test_a_root_within_the_bound_is_not_reported_cut(self, monkeypatch):
        from kiro_crew.crew_log import read as reader

        self._dirs(3)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 3)
        kept, cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert len(kept) == 3
        assert cut is False

    def test_a_cut_candidate_set_makes_the_listing_report_truncated(self, monkeypatch):
        """A caller must not read a bounded walk as the whole tree."""
        from kiro_crew.crew_log import read as reader

        self._dirs(5)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 2)
        payload = reader.list_session_units(limit=50)
        assert payload["truncated"] is True

    def test_the_walk_never_holds_more_than_the_bound(self, monkeypatch):
        """The bound is on RETENTION: the heap is capped during iteration, so the
        peak held size cannot grow with the directory count."""
        from kiro_crew.crew_log import read as reader

        self._dirs(9)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 2)
        peaks: list[int] = []
        real_push = reader.heapq.heappush

        def watched(heap, item):
            real_push(heap, item)
            peaks.append(len(heap))

        monkeypatch.setattr(reader.heapq, "heappush", watched)
        reader._candidate_dirs(lg.KIND_SESSION)
        assert peaks and max(peaks) <= 2

    def test_a_root_that_was_never_created_is_empty_and_not_cut(self):
        from kiro_crew.crew_log import read as reader

        assert not lg.crew_log_root(lg.KIND_SESSION).exists()
        assert reader._candidate_dirs(lg.KIND_SESSION) == ([], False)


class TestTheRetentionListingStaysOffTheLoop:
    """A directory listing must never be reached from a coroutine's own body.

    The listing only chooses between two refusal TEXTS, so blocking the loop for it
    spends every session's latency on wording. A review lane found exactly that, and a
    reading of the source is what catches it returning: the repo's sync-IO gate did not
    fire here, because the blocking call is several frames below the coroutine and the
    gate matches direct ones.

    The rule is derived rather than listed: every call site is found in the source, so a
    THIRD one added later is checked without anyone remembering to name it here.
    """

    def _calls_to(self, func_name):
        """Every call of *func_name*, with the enclosing function and whether it awaits."""
        import ast
        import importlib
        import inspect

        from kiro_crew.dashboard.handlers import crew_log as mod

        # a mirror facade: its code is in the owners it lists
        tree = ast.parse(
            inspect.getsource(mod)
            + "".join(inspect.getsource(importlib.import_module(o)) for o in mod._OWNERS)
        )
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                target = inner.func
                if isinstance(target, ast.Name) and target.id == func_name:
                    found.append((node.name, isinstance(node, ast.AsyncFunctionDef), "direct"))
                elif (
                    isinstance(target, ast.Attribute)
                    and target.attr == "to_thread"
                    and any(isinstance(a, ast.Name) and a.id == func_name for a in inner.args)
                ):
                    found.append((node.name, isinstance(node, ast.AsyncFunctionDef), "offloaded"))
        return found

    def test_the_listing_is_only_ever_offloaded(self):
        calls = self._calls_to("_front_of_log_is_gone")
        assert calls, "no call site found, so this pin measures nothing"
        on_loop = [(where, how) for where, is_async, how in calls if is_async and how == "direct"]
        assert not on_loop, f"reached on the event loop from {on_loop}"

    def test_the_offloaded_reader_is_the_one_that_may_call_it_directly(self):
        """The non-async helper is allowed to, because it is what gets offloaded."""
        direct = {
            where
            for where, is_async, how in self._calls_to("_front_of_log_is_gone")
            if how == "direct"
        }
        assert direct <= {"_recorded_class_and_front"}, direct


class TestTheCallerTestAgreesWithTheSharedDerivation:
    """The inline caller test must refuse every state the shared derivation publishes.

    ``_caller_class_refusal`` respells the test ``chat_runner._crew_log_class`` makes,
    and deliberately: the derivation answers with a class, while a refusal has to NAME
    which member refused and why it matters, and a caller that reads its own reason is
    the difference between a usable refusal and a bare 403. What that duplication risks
    is divergence -- a member the derivation learns to report and the refusal never
    tests would let a publishing caller read another session's log, which fails OPEN.

    So the agreement is pinned rather than the duplication removed. Each case asserts
    the shape actually moved the DERIVATION before asking the refusal about it, so a
    fixture that stopped constructing the state it names fails instead of passing
    vacuously, and the width assertion makes a NEW member fail here until someone
    decides whether the refusal owes it a test.
    """

    def _request_for(self, slot):
        return _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: slot},
        )

    def test_the_derivation_reports_exactly_the_members_the_refusal_tests(self):
        """A fourth member has to be considered here, not silently untested."""
        from kiro_crew.dashboard.chat_runner import _crew_log_class

        assert len(_crew_log_class(None, _Slot())) == 3

    @pytest.mark.parametrize(
        "shape,expected",
        [
            ({"memory_mode": "incognito"}, "leave and learn nothing"),
            ({"_app": "notes"}, "app-owned session"),
            ({"linked_session_key": "telegram:-100999"}, "lands in that channel's thread"),
        ],
        ids=["memory", "app", "channel"],
    )
    def test_a_state_the_derivation_publishes_is_refused(self, monkeypatch, shape, expected):
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        for name, value in shape.items():
            setattr(slot, name, value)
        request = self._request_for(slot)
        from kiro_crew.dashboard.chat_runner import _crew_log_class

        state = request.app.get("state")
        assert _crew_log_class(state, slot) != (
            "persistent",
            "",
            False,
        ), "the fixture no longer moves the shared derivation, so it pins nothing"
        assert expected in routes._caller_class_refusal(request, CONDUCTOR_SLOT)

    def test_a_state_the_derivation_calls_private_is_admitted(self, monkeypatch):
        """The accepting case, without which every assertion above could be a blanket no."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        request = self._request_for(slot)
        from kiro_crew.dashboard.chat_runner import _crew_log_class

        assert _crew_log_class(request.app.get("state"), slot) == ("persistent", "", False)
        assert routes._caller_class_refusal(request, CONDUCTOR_SLOT) == ""


class TestRecencyComesFromTheLogNotItsDirectory:
    """A long-lived session is the case that breaks reading the directory's mtime.

    A directory's mtime moves when its entry SET changes -- a file created, renamed
    or removed -- not when a file inside it is written. A unit directory gets its
    ``log.jsonl`` once and is appended to for the rest of the session, so its own
    mtime freezes at creation. Order by it and "newest first" silently means "most
    recently STARTED first", and a recency window drops the session that has been
    open and busy for a day while keeping one created a minute ago and idle since.
    That is backwards for both callers: the listing exists to find active sessions.
    """

    @staticmethod
    def _unit(unit_id: str, *, dir_at: float, log_at: float) -> None:
        """One real unit whose directory and log carry DIFFERENT times.

        Through ``crew_log_dir``, never ``root / unit_id``: a unit directory is named
        with a readable-plus-digest fold of the id, not the id itself.
        """
        from kiro_crew.crew_log import read as reader

        _opened(_log(unit_id))
        directory = lg.crew_log_dir(lg.KIND_SESSION, unit_id)
        os.utime(directory / reader.LOG_FILE, (log_at, log_at))
        # The directory LAST, so creating the log cannot move it afterwards.
        os.utime(directory, (dir_at, dir_at))

    def test_the_busy_old_session_sorts_newer_than_the_idle_new_one(self):
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-open-all-day", dir_at=now - 86_400, log_at=now)
        self._unit("s-started-just-now", dir_at=now, log_at=now - 86_400)
        kept, _cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert [path.name for path in kept] == [
            lg.crew_log_dir(lg.KIND_SESSION, "s-open-all-day").name,
            lg.crew_log_dir(lg.KIND_SESSION, "s-started-just-now").name,
        ]

    def test_a_recency_window_keeps_the_busy_session_and_drops_the_idle_one(self):
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-open-all-day", dir_at=now - 86_400, log_at=now)
        self._unit("s-started-just-now", dir_at=now, log_at=now - 86_400)
        payload = reader.list_session_units(limit=50, active_within_ms=3_600_000)
        assert [row["unit"] for row in payload["units"]] == ["s-open-all-day"]

    def test_a_unit_with_no_readable_log_sorts_oldest_rather_than_raising(self):
        """An unreadable member of the root must not fail the whole listing.

        It is refused a moment later for having no unit id, so the only question
        here is whether one bad directory can take the listing down with it.
        """
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-real", dir_at=now, log_at=now)
        (lg.crew_log_root(lg.KIND_SESSION) / "s-no-log").mkdir()
        assert reader._written_at(lg.crew_log_root(lg.KIND_SESSION) / "s-no-log") == 0.0
        payload = reader.list_session_units(limit=50)
        assert [row["unit"] for row in payload["units"]] == ["s-real"]


class TestTheWorkspaceBoundary:
    """A dispatch grant does not cross a workspace, because a lineage outlives a switch.

    The creator edge is written on the CHILD and nothing rewrites it, so a conductor that
    dispatched a child and then moved to another workspace still names that child in its
    tree. Switching workspace is ordinary operation, so the recorded lineage alone would
    hand that conductor a log belonging to the workspace it left -- the boundary a
    workspace exists to draw.

    So the class records which workspace stated it, and the fence compares. Three states
    refuse, one fact in three shapes: the workspaces DIFFER, the log's workspace MOVED so
    its content spans two and neither owns it, or the log records NO workspace, which no
    live slot can produce.

    The arm is ordered LAST of the target arms on purpose -- it asks about ownership while
    the arms above ask about publication, and a caller refused for either deserves the
    more specific reason.
    """

    def test_a_conductor_in_the_same_workspace_still_reads_its_child(self, monkeypatch):
        """The accepting case, without which every assertion below could be a blanket no."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_conductor_that_moved_workspace_cannot_read_its_own_child(self, monkeypatch):
        """MUTATION-SENSITIVE: the comparison itself, on the scenario the lane named.

        Every other arm admits this read. The lineage is recorded and intact, the caller
        is a persistent non-app session publishing to nobody, and the target's class is
        clean -- so the workspace comparison is the only thing standing between this
        caller and a log from the workspace it left.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: _Slot(workspace="beta")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "belongs to a different workspace" in json.loads(response.text)["error"]

    def test_a_log_whose_workspace_moved_is_read_by_neither(self, monkeypatch):
        """A log whose content spans two workspaces is owned by neither, so both refuse.

        Asserted from the workspace the log STARTED in, which is the caller most likely to
        be admitted by a naive first-one-wins comparison.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, workspace="beta")
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "spans more than one workspace" in json.loads(response.text)["error"]

    def test_a_log_that_records_no_workspace_is_refused(self, monkeypatch):
        """Silence is not a match. A live slot always states a workspace, so a log that
        does not is one this build cannot place -- and placing it by assumption is the
        whole thing being prevented."""
        _flag_on(monkeypatch)
        _dispatch_tree(workspace="")
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "does not record which workspace" in json.loads(response.text)["error"]

    def test_a_workspace_that_moves_mid_read_is_caught_by_the_recheck(self, monkeypatch):
        """MUTATION-SENSITIVE: the workspace arm in the STALE re-check, not just the gate.

        At the grant the log named one workspace and the caller matched it, so the gate
        admitted the read. The log then records a move while the payload is being built,
        which puts content from a second workspace in this same log. Only a fold taken
        AFTER the read sees it, which is why the arm runs in both places rather than once.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        real = routes._read_page

        def _moving(*args, **kwargs):
            _class_moved(CHILD_UNIT, workspace="beta")
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "_read_page", _moving)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "spans more than one workspace" in json.loads(response.text)["error"]

    def test_the_gate_refuses_before_a_payload_is_built(self, monkeypatch):
        """MUTATION-SENSITIVE: the arm in the GATE, which the route alone cannot pin.

        Every unit read also passes the stale re-check, and that runs this arm too -- so
        deleting the gate's call leaves each route test above still green, refused a moment
        later by the re-check. The difference is not the verdict but WHEN: without the gate
        arm the payload is read off disk first and refused afterwards, so the log is pulled
        into memory for a caller who must not see it.

        So the gate is asked directly, where no re-check can answer for it.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: _Slot(workspace="beta")},
        )
        refusal = asyncio.run(routes._read_scope_refusal(request, CONDUCTOR_KEY, CHILD_UNIT))
        assert "belongs to a different workspace" in refusal

    def test_the_gate_admits_a_caller_in_the_same_workspace(self):
        """The accepting half of the assertion above, so it is a boundary not a wall."""
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        refusal = asyncio.run(routes._read_scope_refusal(request, CONDUCTOR_KEY, CHILD_UNIT))
        assert refusal == ""


class TestTheListingCarriesTheWorkspaceBoundary:
    """A listing discloses each row's unit, slot, model and activity, so the workspace
    boundary the per-unit door draws has to reach the rows as well.

    Lineage alone does not bound which workspace a row belongs to, and the dispatch edge
    is written on the child and never rewritten -- so a conductor that dispatched in one
    workspace and then switched still names those children in its tree. Without a per-row
    test the listing becomes an enumeration oracle for exactly the units the read path
    refuses to open.

    The test is asked of LINEAGE rows only, which is what the per-unit door does: a
    caller's own record is answered before any target test, and an owner's unscoped
    listing runs none at all.
    """

    def test_a_conductor_that_moved_workspace_sees_only_its_own_row(self, monkeypatch):
        """MUTATION-SENSITIVE: the per-row filter.

        Same tree and same caller as the scope test above, which lists all three. Only
        the workspace comparison removes the two descendants here -- and the caller's own
        row staying is what proves the filter is scoped to lineage rather than blanket.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            "/api/crew-log/sessions", slots={CONDUCTOR_SLOT: _Slot(workspace="beta")}
        )
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert listed == {CONDUCTOR_UNIT}

    def test_a_descendant_whose_workspace_moved_drops_out_alone(self, monkeypatch):
        """One row spanning two workspaces is refused without taking its siblings."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, workspace="beta")
        request = _as_conductor("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert listed == {CONDUCTOR_UNIT, GRANDCHILD_UNIT}

    def test_the_owner_still_sees_a_unit_from_another_workspace(self, monkeypatch):
        """The owner's listing is not narrowed, so it is never asked -- the recorded
        product decision for that door, which this change must not quietly revise."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, workspace="beta")
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert {CONDUCTOR_UNIT, CHILD_UNIT, GRANDCHILD_UNIT} <= listed

    def test_a_caller_that_switches_workspace_mid_listing_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check's workspace arm, and why the gate stores a
        VALUE.

        The rows were admitted against the workspace read at the gate. The caller then
        switches while the walk runs, so the payload it is about to receive names rows
        from the workspace it just left. The scope tuple is a unit and a slot and neither
        moves on a switch, so a tuple comparison alone cannot see this.

        It also pins the storage choice: holding the slot OBJECT would make the re-check
        re-read the same mutable attribute and compare the new workspace against itself,
        which is always equal and would never refuse.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot(workspace="default")
        reader = routes._crew_log_read()
        real = reader.list_session_units

        def _switching(*args, **kwargs):
            out = real(*args, **kwargs)
            slot.workspace = "beta"
            return out

        monkeypatch.setattr(reader, "list_session_units", _switching)
        request = _as_conductor("/api/crew-log/sessions", slots={CONDUCTOR_SLOT: slot})
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert (
            "changed workspace while this listing was built" in json.loads(response.text)["error"]
        )
