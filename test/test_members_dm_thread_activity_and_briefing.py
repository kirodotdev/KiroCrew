"""Crew Members DM threads: the activity and briefing reads, and the audits they leave.

``GET /api/members/{slug}/activity`` filters by the exact member name before its display
cap, asks the event log only for bounded pages and redacts project values; the event-log
service singleton never crosses a test boundary and releases its handles when retired.
``GET /api/members/{slug}/briefing`` is owner-only, refuses a colliding slug and a
symlinked member directory, redacts before its cap and leaves an allowed audit row; the
deny-path audits run inline on the event loop. Shared fakes live in
``members_dm_thread_helpers.py``.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from members_dm_thread_helpers import (
    CREW,
    OTHER,
    _make_members_app,
    _patched_config,
    _record_or_explain,
)

from kiro_crew.members import (
    MEMBER_BRIEFING_MAX_CHARS,
    member_slot_key,
    members_root,
    write_dm_binding,
)


class TestMemberActivityRoute:
    """GET /api/members/{slug}/activity — the drawer's timeline feed."""

    @pytest.mark.asyncio
    async def test_returns_recorded_entries_newest_first_with_allowlist_fields(self, tmp_path):
        state = _make_state(tmp_path)

        assert _record_or_explain(CREW, "dashboard_chat-1", "persistent", via="chat")
        assert _record_or_explain(
            CREW, "dashboard_chat-2", "persistent", project="/repo", via="select_crew"
        )
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert data["slug"] == "code-reviewer"
        assert data["member"] == CREW
        assert data["capped"] is False
        assert len(data["entries"]) == 2
        # Newest first — the drawer renders top-down.
        assert data["entries"][0]["via"] == "select_crew"
        assert data["entries"][0]["project"] == "/repo"
        assert data["entries"][0]["ts"] >= data["entries"][1]["ts"] > 0
        # Session keys stay OUT of the payload: the drawer renders what
        # happened, never handles into other sessions. This is the response's
        # field allowlist, pinned exactly.
        assert set(data["entries"][0]) == {"ts", "via", "project"}

    @pytest.mark.asyncio
    async def test_the_read_never_asks_the_log_for_every_event(self, tmp_path):
        """The allocation happens INSIDE `history`, so the caller must bound the ask.

        With `limit=None` and a log past `MAX_RETAINED_EVENTS`, `history` builds every
        event in the lifetime file into a list and reverses it before the caller sees
        anything -- so no amount of care in this handler bounds it. The member log has
        no rotation, so outgrowing the retained tail is ordinary ageing, and the
        response it feeds shows `_ACTIVITY_LIMIT` rows.

        The test above (1001 buried envelopes) proves paging still FINDS the records.
        This one pins the reason paging exists: every ask carries a limit.
        """
        state = _make_state(tmp_path)
        from unittest import mock

        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog import types as _types
        from kiro_crew.members import slug_for_name

        assert _record_or_explain(
            CREW, "dashboard_chat-1", "persistent", project="/repo", via="chat"
        )
        svc = svc_mod.get_service()
        slug = slug_for_name(CREW)
        for i in range(1001):
            svc.append(slug, _types.MEMBER_MESSAGE, {"text": f"m{i}"})

        asks: list[object] = []
        real_history = type(svc).history

        def _recording_history(self, slug_in, *, before=None, limit=None):
            asks.append(limit)
            return real_history(self, slug_in, before=before, limit=limit)

        with _patched_config([CREW]):
            with mock.patch.object(type(svc), "history", _recording_history):
                async with TestClient(TestServer(_make_members_app(state))) as client:
                    resp = await client.get(
                        "/api/members/code-reviewer/activity", params={"member": CREW}
                    )
                    assert resp.status == 200
                    data = await resp.json()

        assert asks, "the endpoint did not read the log at all"
        assert None not in asks, (
            "the activity read asked for the whole log (limit=None); `history` then "
            f"materialises the entire lifetime file before returning. asks={asks}"
        )
        assert all(
            isinstance(a, int) and 0 < a <= 1000 for a in asks
        ), f"an ask was not a small bounded page: {asks}"
        # Still correct: the buried record is found despite the bounded asks.
        assert len(asks) > 1, "1001 envelopes should have needed more than one page"
        assert [e["project"] for e in data["entries"]] == ["/repo"]

    @pytest.mark.asyncio
    async def test_activity_survives_more_than_a_thousand_later_events(self, tmp_path):
        """The cap applies to this member's ACTIVITY, not to a slice of the log.

        One log carries config, binding, rules, message, slot and patrol events
        beside activity records, and a colliding slug's log carries another exact
        name's records too. Reading a fixed slice of the newest envelopes and
        filtering afterwards therefore drops activity the drawer promises to show:
        a member with a busy message history loses their whole timeline even though
        the records are still in the log. The filter runs before any cap.
        """
        state = _make_state(tmp_path)
        from kiro_crew.eventlog import types as _types
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.members import slug_for_name

        assert _record_or_explain(
            CREW, "dashboard_chat-1", "persistent", project="/repo", via="chat"
        )
        # Bury it behind more envelopes than the former read window held.
        svc = get_service()
        slug = slug_for_name(CREW)
        for i in range(1001):
            svc.append(slug, _types.MEMBER_MESSAGE, {"text": f"m{i}"})

        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert len(data["entries"]) == 1, f"activity was cut off by the envelope read: {data}"
        assert data["entries"][0]["project"] == "/repo"
        assert data["capped"] is False

    @pytest.mark.asyncio
    async def test_colliding_slugs_do_not_mix_histories(self, tmp_path):
        """Two names sharing a slug share a log file, never a timeline.

        Slugification is lossy ('Code Review' and 'code-review' both derive
        code-review), so the endpoint filters by the exact member name each
        record carries — one member's drawer must not render (or count) the
        other's events.
        """
        state = _make_state(tmp_path)

        other = "Code_Reviewer"  # distinct exact name, same derived slug
        assert _record_or_explain(CREW, "dashboard_chat-1", "persistent", via="chat")
        assert _record_or_explain(other, "dashboard_chat-2", "persistent", via="chat")
        with _patched_config([CREW, other]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                mine = await (
                    await client.get("/api/members/code-reviewer/activity", params={"member": CREW})
                ).json()
                theirs = await (
                    await client.get(
                        "/api/members/code-reviewer/activity", params={"member": other}
                    )
                ).json()
        assert len(mine["entries"]) == 1
        assert len(theirs["entries"]) == 1

    @pytest.mark.asyncio
    async def test_member_param_is_required(self, tmp_path):
        """Without the exact name a colliding slug's read is unsound, so the
        parameter is required by construction rather than caller discipline."""
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members/code-reviewer/activity")
                assert resp.status == 400
                assert (await resp.json())["code"] == "missing_member"

    @pytest.mark.asyncio
    async def test_empty_log_and_invalid_slug(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                assert (await resp.json())["entries"] == []
                # Path traversal / bad grammar refused before any file IO.
                bad = await client.get("/api/members/Bad_Slug!/activity", params={"member": CREW})
                assert bad.status == 400
                assert (await bad.json())["code"] == "invalid_member_slug"

    @pytest.mark.asyncio
    async def test_unreadable_timestamps_are_skipped_not_sorted_as_garbage(self, tmp_path):
        """A record without a parseable STRING ts cannot be placed on a
        timeline — including a numeric epoch from a foreign writer, which
        must read as unplaceable rather than crash the endpoint."""
        state = _make_state(tmp_path)
        from kiro_crew.members import ACTIVITY_FILE_NAME, member_dir

        assert _record_or_explain(CREW, "dashboard_chat-1", "persistent", via="chat")
        path = member_dir("code-reviewer") / ACTIVITY_FILE_NAME
        # Only the LEGACY file lives in the member directory now -- the log moved
        # under the fenced crew-log tree -- so nothing has created it yet.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f'\n{{"ts": "not-a-date", "member": "{CREW}", "via": "chat"}}\n')
            fh.write(f'\n{{"ts": 1735689600, "member": "{CREW}", "via": "chat"}}\n')
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert len(data["entries"]) == 1

    @pytest.mark.asyncio
    async def test_project_values_are_redacted_at_the_boundary(self, tmp_path):
        """A project value is operator-supplied text that can embed a
        credential; the response is a network boundary, so it runs the same
        redaction chain as the roster's message preview."""
        state = _make_state(tmp_path)

        assert _record_or_explain(
            CREW,
            "dashboard_chat-1",
            "persistent",
            project="/repos/AKIAIOSFODNN7EXAMPLE/app",
            via="chat",
        )
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert len(data["entries"]) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in data["entries"][0]["project"]

    @pytest.mark.asyncio
    async def test_display_cap_reports_capped_and_keeps_newest(self, tmp_path):
        """Entries beyond the display cap trim the OLDEST tail, and the
        response says the window is saturated so the drawer renders its
        derived counters as floors ("N+") instead of asserting exact totals."""
        state = _make_state(tmp_path)
        from kiro_crew.dashboard.handlers import members as handler_mod

        for i in range(handler_mod._ACTIVITY_LIMIT + 3):
            assert _record_or_explain(CREW, f"dashboard_chat-{i}", "persistent", via="chat")
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert data["capped"] is True
        assert len(data["entries"]) == handler_mod._ACTIVITY_LIMIT


@pytest.mark.xdist_group(name="eventlog_singleton_leak_across_tests")
class TestEventlogSingletonDoesNotLeakAcrossTests:
    """The process-wide ``get_service()`` singleton must not survive a test.

    Grouped onto ONE worker: the pair is ordered, and under ``--dist loadgroup`` an
    ungrouped pair is split across workers, where the second test asserts nothing.

    ``get_service()`` memoises one service per process and rebuilds it only when
    the crew-log root moves. The root follows ``KIROCREW_HOME``, which the autouse
    ``_isolate_kirocrew_home`` fixture repoints at a fresh tmp dir per test -- so a
    test that touches the service leaves a live singleton BOUND TO ITS OWN HOME,
    and the next test on the same xdist worker inherits it after that home is torn
    down. On POSIX ``get_service()`` then rebuilds against the new home and the
    inheritor is fine; on Windows the leaked ``MemberLog`` handles under the dead
    directory block teardown and the inheritor's first ``record_activity`` write
    fails, returning ``False`` -- the Windows-only shard-8 red on the class above,
    reproduced by any predecessor that used the service without resetting it
    (``test_members_roster_recency``'s roster-read tests do exactly that).

    These two tests run in order WITHOUT a local reset fixture, so they exercise
    only the ``_reset_member_eventlog_singleton`` floor in ``conftest.py``. The
    first builds a service bound to its own home; the second asserts it did NOT
    inherit that binding. Platform-independent: it pins the leak's CAUSE (a
    singleton crossing the boundary) rather than the Windows-only SYMPTOM, so it is
    a real regression guard on Linux CI too.
    """

    #: Set by the first test to the home its service bound to, read by the second
    #: to prove the singleton did not carry that binding across the boundary.
    _bound_home: str = ""

    def test_a_service_built_here_binds_to_this_home(self, tmp_path):
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.members import slug_for_name

        # Touch the service the way a real test does, leaving it live at teardown.
        assert _record_or_explain(CREW, "dashboard_chat-1", "persistent", via="chat")
        svc = svc_mod.get_service()
        assert slug_for_name(CREW) in set(svc.slugs())
        type(self)._bound_home = str(svc.root)
        # The singleton is live now; the conftest floor must clear it at teardown.
        assert svc_mod._singleton is not None

    def test_the_next_test_does_not_inherit_that_service(self, tmp_path):
        from kiro_crew.eventlog import service as svc_mod

        # The floor reset the singleton at the previous test's teardown (and this
        # test's setup), so nothing is inherited: the first read here rebuilds
        # against THIS test's own home, never the previous, torn-down one.
        assert svc_mod._singleton is None, (
            "eventlog service leaked across the test boundary; a Windows worker "
            "would then write into the previous test's torn-down home"
        )
        # And a fresh read binds to this test's home, not the leaked one.
        svc = svc_mod.get_service()
        assert str(svc.root) != type(self)._bound_home


class TestEventlogServiceRetirementReleasesHandles:
    """Retiring the service must release its cached handles NOW, not at a GC pass.

    The class above pins that the singleton reference does not cross a test
    boundary. That is necessary but not sufficient for the Windows-only shard-8
    red on ``TestMemberActivityRoute``: ``get_service()`` and its cached
    ``MemberLog`` -> ``CrewLog`` chain sit in a REFERENCE CYCLE (the projection
    registry holds ``set_on_change(service._on_change)``, a bound method back to
    the service), so ``set_service(None)`` drops the last EXTERNAL reference but
    does not free the graph -- the cycle keeps it alive until the cyclic collector
    runs. Each cached ``CrewLog`` releases its write lease through a
    ``weakref.finalize`` that fires only then, and holds its file descriptor until
    then. On POSIX that deferral is invisible; on Windows the still-open descriptor
    keeps a mandatory lock that pins the (torn-down) home directory and fails the
    next writer on the inheriting worker -- ``record_activity`` returns ``False``,
    which is exactly the observed traceback (`assert _record_or_explain(...) is False`).

    So retirement (``set_service`` replacing the singleton, and ``get_service``
    rebuilding on a home change) must DETERMINISTICALLY release those handles and
    sever the cycle rather than wait for GC. These tests pin that contract on the
    retired instance directly, so they fail on Linux the moment the release is
    dropped -- the CAUSE, provable without Windows.
    """

    def test_set_service_none_clears_cached_logs_and_breaks_cycle(self, tmp_path):
        from kiro_crew.eventlog import service as svc_mod

        # Touch the service so it caches a MemberLog whose CrewLog took the lease.
        assert _record_or_explain(CREW, "dashboard_chat-1", "persistent", via="chat")
        svc = svc_mod.get_service()
        assert len(svc._logs) >= 1
        assert svc._registry._on_change is not None

        # Retire exactly as the conftest floor does.
        svc_mod.set_service(None)

        # Without the deterministic close these both stay put: the service is
        # cycle-pinned, so its _logs dict (and the Windows file handles those logs
        # hold) survive the reset until a GC pass that may never come mid-run.
        assert len(svc._logs) == 0, (
            "retired service still caches MemberLog handles; on Windows their open "
            "descriptors pin the torn-down home and fail the next worker's write"
        )
        assert svc._registry._on_change is None, (
            "registry -> service cycle not severed on retirement; the service and "
            "its handles then live until the cyclic collector runs"
        )

    def test_home_change_rebuild_retires_the_previous_service(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import service as svc_mod

        # First home: build a live service with a cached log.
        assert _record_or_explain(CREW, "dashboard_chat-1", "persistent", via="chat")
        first = svc_mod.get_service()
        assert len(first._logs) >= 1

        # Move the home so the NEXT get_service() rebuilds (production never does
        # this; a test boundary does). The rebuild must retire `first`.
        new_home = tmp_path / "second-home"
        new_home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(new_home))
        import kiro_crew.config.paths as paths

        monkeypatch.setattr(paths, "_resolved_home", None, raising=False)

        second = svc_mod.get_service()
        assert second is not first
        assert str(second.root) != str(first.root)
        # The replaced service was retired by the rebuild, not left for GC.
        assert len(first._logs) == 0
        assert first._registry._on_change is None

    def test_close_is_idempotent_and_reopens_on_demand(self, tmp_path):
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.members import slug_for_name

        assert _record_or_explain(CREW, "dashboard_chat-1", "persistent", via="chat")
        svc = svc_mod.get_service()
        slug = slug_for_name(CREW)

        svc.close()
        assert len(svc._logs) == 0
        svc.close()  # second call must not raise

        # close() releases the cached handle; it does not delete data. A read after
        # it re-opens the on-disk log on demand (the same behaviour a fresh service
        # gives), so the member's history is still there -- what was released is the
        # HANDLE, not the record.
        history = svc.history(slug)
        assert [ev["data"].get("session") for ev in history] == ["dashboard_chat-1"]


# The briefing read fails CLOSED on platforms without O_NOFOLLOW (Windows) --
# see read_member_briefing. Tests asserting briefing CONTENT through the
# endpoint are therefore POSIX-only; the fail-closed flag itself is what the
# response's ``supported`` field carries on every platform.
_requires_nofollow = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="briefing reads fail closed without O_NOFOLLOW",
)


class TestMemberBriefingEndpoint:
    """GET /api/members/{slug}/briefing — the panel's read-only Notes tab feed."""

    @staticmethod
    def _write_briefing(text: str):
        from kiro_crew.members import member_briefing_path

        path = member_briefing_path("code-reviewer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @_requires_nofollow
    @pytest.mark.asyncio
    async def test_written_briefing_is_returned_with_its_mtime_and_path(self, tmp_path):
        state = _make_state(tmp_path)
        path = self._write_briefing("This week: crash-tagged issues first.\n")
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert data["slug"] == "code-reviewer"
        assert data["member"] == CREW
        assert data["supported"] is True
        assert data["text"] == "This week: crash-tagged issues first."
        assert isinstance(data["updated_ts"], float)
        assert abs(data["updated_ts"] - path.stat().st_mtime) < 5
        # The response's field allowlist, pinned exactly: no file pointer --
        # the panel offers no editor for an agent-written file.
        assert set(data) == {
            "slug",
            "member",
            "supported",
            "text",
            "updated_ts",
            "redacted",
            "truncated",
            "upkeep",
        }
        assert data["redacted"] is False
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_the_upkeep_skill_is_the_one_the_crewmate_is_pointed_at(self, tmp_path):
        """Same renderer as the pointer's skill, so the person reads exactly what the
        crewmate reads -- and not the briefing read, so it is there even where notes
        cannot be read."""
        from kiro_crew import dashboard_agentic, dashboard_upkeep
        from kiro_crew.dashboard_templates import instance

        # An ADOPTED page: an unadopted one shows the suggestions page and offers no upkeep.
        instance.adopt("code-reviewer", "project-report")
        # Adopting syncs the helper scripts; this test is about the READ writing none.
        import shutil

        shutil.rmtree(dashboard_upkeep.scripts_dir("code-reviewer"), ignore_errors=True)
        state = _make_state(tmp_path)
        with (
            _patched_config([CREW]),
            patch("kiro_crew.members.member_briefing_supported", return_value=False),
        ):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        upkeep = data["upkeep"]
        assert not dashboard_upkeep.scripts_dir("code-reviewer").exists(), "a read wrote scripts"
        assert upkeep["pointer"] == dashboard_upkeep.POINTER
        assert upkeep["pointer"] in dashboard_agentic.turn_block("code-reviewer")
        assert [row["field"] for row in upkeep["fields"]] == ["ci", "for_you", "verdict"]
        ci = next(row for row in upkeep["fields"] if row["field"] == "ci")
        assert set(ci) == {"field", "means", "when", "how", "helper"}
        assert upkeep["scripts"] == ["project_report_ci.py"]

    @pytest.mark.asyncio
    async def test_the_upkeep_text_crosses_the_redaction_chain(self, tmp_path):
        """Field names come from the crewmate's editable copy of the manifest."""
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        state = _make_state(tmp_path)
        fake = {
            "pointer": "p",
            "fields": [{"field": "ci", "means": f"see {token}", "when": "w", "how": "h"}],
            "scripts": [],
        }
        with (
            _patched_config([CREW]),
            patch("kiro_crew.dashboard_upkeep.view", return_value=fake),
        ):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert token not in data["upkeep"]["fields"][0]["means"]
        assert data["redacted"] is True

    @pytest.mark.asyncio
    async def test_no_briefing_yet_is_empty_not_404(self, tmp_path):
        """A fresh crewmate has no notes; that is the normal state, not an error."""
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert data["text"] == ""
        assert data["updated_ts"] is None
        # No file, no pointer: an Edit on a "not on disk" placeholder could later
        # save that stale buffer over notes the crewmate wrote in the meantime.

    @pytest.mark.asyncio
    async def test_unsupported_platform_fails_closed_for_text_and_stamp_alike(
        self, tmp_path, monkeypatch
    ):
        """Where the read fails closed (no O_NOFOLLOW), the stamp must too.

        A response that says ``supported: false`` with an empty text but a real
        ``updated_ts`` would let the panel date notes it just said it cannot
        read; the two fields travel together.
        """
        state = _make_state(tmp_path)
        self._write_briefing("Notes the platform cannot read safely.\n")
        # Both the handler (module attribute) and the mtime helper (module global)
        # resolve the gate through kiro_crew.members at call time.
        monkeypatch.setattr("kiro_crew.members.member_briefing_supported", lambda: False)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert data["supported"] is False
        assert data["text"] == ""
        assert data["updated_ts"] is None

    @_requires_nofollow
    @pytest.mark.asyncio
    async def test_symlinked_member_dir_never_reads_a_peer(self, tmp_path):
        """A ``members/<slug>`` swapped for a symlink to a peer's directory is
        refused by the pinned read: the text reads as no notes and the stamp is
        ``null``, never the peer's file dated as this crewmate's."""

        state = _make_state(tmp_path)
        peer = members_root() / "peer"
        peer.mkdir(parents=True)
        (peer / "briefing.md").write_text("The peer's private notes.\n", encoding="utf-8")
        link = members_root() / "code-reviewer"
        link.symlink_to(peer, target_is_directory=True)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert data["text"] == ""
        assert data["updated_ts"] is None
        # The refused read yields no pointer at all -- least of all the peer's.

    @pytest.mark.asyncio
    async def test_colliding_slug_is_refused_not_shown_as_either_crewmates_notes(self, tmp_path):
        """One briefing file per slug; two names on it belong to neither.

        Rendering the shared file as one member's notes -- with an Edit that
        saves over it -- would let the two crewmates overwrite each other, so
        the read is refused for BOTH names with a coded 409.
        """
        state = _make_state(tmp_path)
        self._write_briefing("Whose notes are these?\n")
        other = "Code_Reviewer"  # distinct exact name, same derived slug
        with _patched_config([CREW, other]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                for name in (CREW, other):
                    resp = await client.get(
                        "/api/members/code-reviewer/briefing", params={"member": name}
                    )
                    assert resp.status == 409
                    assert (await resp.json())["code"] == "briefing_slug_ambiguous"

    @pytest.mark.asyncio
    async def test_member_must_derive_the_slug_and_exist(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW, OTHER]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                mismatch = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": OTHER}
                )
                assert mismatch.status == 400
                assert (await mismatch.json())["code"] == "member_slug_mismatch"
                gone = await client.get(
                    "/api/members/nobody-here/briefing", params={"member": "nobody-here"}
                )
                assert gone.status == 404
                assert (await gone.json())["code"] == "member_not_found"

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_caller_is_refused_before_any_read(self, tmp_path):
        """The briefing is the owner's to read, like the rules.

        A Telegram, Teams or Webex allowlist user can mint a dashboard
        session, so the app-caller guard alone would hand a non-owner colleague the
        crewmate's private notes. The owner gate answers first, so the refusal
        costs no file IO -- the briefing on disk is never opened.
        """
        state = _make_state(tmp_path)
        self._write_briefing("Owner-only working notes.\n")
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                with patch(
                    "kiro_crew.members.read_member_briefing_bounded",
                    side_effect=AssertionError("read must not run for a non-owner"),
                ):
                    resp = await client.get(
                        "/api/members/code-reviewer/briefing",
                        params={"member": CREW},
                        headers={"X-Test-User": "colleague"},
                    )
                assert resp.status == 403
                assert (await resp.json())["code"] == "owner_only"

    @pytest.mark.asyncio
    async def test_invalid_slug_is_refused_before_any_file_io(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                bad = await client.get("/api/members/Bad_Slug!/briefing", params={"member": CREW})
                assert bad.status == 400
                assert (await bad.json())["code"] == "invalid_member_slug"

    @pytest.mark.asyncio
    async def test_member_param_is_required(self, tmp_path):
        """The slug is lossy; the exact name is what the frontend keys its cache by."""
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members/code-reviewer/briefing")
                assert resp.status == 400
                assert (await resp.json())["code"] == "missing_member"
                # A name the display-name rule refuses (a tab) is "no member",
                # not a mismatch; spaces alone are display text and pass.
                bad = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": "no\ttabs"}
                )
                assert bad.status == 400
                assert (await bad.json())["code"] == "missing_member"

    @pytest.mark.asyncio
    async def test_app_tokens_are_denied_like_every_member_surface(self, tmp_path):
        state = _make_state(tmp_path)
        self._write_briefing("secret plans")

        @web.middleware
        async def _as_app(request: web.Request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_members_app(state)
        app.middlewares.insert(0, _as_app)
        with _patched_config([CREW]):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 404
                assert (await resp.json()) == {"error": "not found", "code": "not_found"}

    @_requires_nofollow
    @pytest.mark.asyncio
    async def test_credentials_in_the_briefing_are_redacted_at_the_boundary(self, tmp_path):
        """The briefing is an AGENT-written file; a token the crewmate pasted
        into its own notes must not reach the browser verbatim."""
        state = _make_state(tmp_path)
        self._write_briefing("Deploy key for staging: AKIAIOSFODNN7EXAMPLE — rotate monthly.")
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert "AKIAIOSFODNN7EXAMPLE" not in data["text"]
        assert "rotate monthly" in data["text"]
        # The flag is what lets the panel withhold Edit: the viewer's Save would
        # otherwise write this redacted text over the original.
        assert data["redacted"] is True

    @_requires_nofollow
    @pytest.mark.asyncio
    async def test_secret_straddling_the_cap_never_leaks_its_prefix(self, tmp_path):
        """The redaction runs over the bounded buffer BEFORE the character
        cap: a token the cap would split in two is matched whole and
        replaced, so no plaintext prefix crosses the wire, and the cut then
        drops the trailing split word so nothing ends mid-token."""
        state = _make_state(tmp_path)
        lead = "word " * ((MEMBER_BRIEFING_MAX_CHARS - 10) // 5)  # ends 10 chars short
        self._write_briefing(lead + "AKIAIOSFODNN7EXAMPLE rest of the line\n" + "tail\n" * 40)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert "AKIA" not in data["text"]
        assert "briefing truncated" in data["text"]
        assert data["redacted"] is True
        assert data["truncated"] is True
        # The shown text never ends in the first half of a word.
        shown = data["text"].split("\n[... briefing truncated")[0]
        assert shown == shown.rstrip() and not shown.endswith("wor")

    @_requires_nofollow
    @pytest.mark.asyncio
    async def test_secret_past_the_read_bound_is_reported_as_truncated(self, tmp_path):
        """A token past the bounded read is never seen, so ``redacted`` stays
        false for it -- while the file viewer reads (and redacts) the whole
        file and its Save would write the redacted tail back. ``truncated``
        is the flag that lets the panel withhold Edit for that file too."""
        state = _make_state(tmp_path)
        self._write_briefing(
            "y" * (MEMBER_BRIEFING_MAX_CHARS * 8) + "\nDeploy key: AKIAIOSFODNN7EXAMPLE\n"
        )
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert "AKIA" not in data["text"]
        assert "briefing truncated" in data["text"]
        assert data["redacted"] is False
        assert data["truncated"] is True

    @_requires_nofollow
    @pytest.mark.asyncio
    async def test_redaction_that_shrinks_below_the_cap_shows_the_whole_briefing(self, tmp_path):
        """The cap is judged on the REDACTED length: a briefing that only ran
        past the cap before its placeholders shrank it is shown whole -- no
        marker, no word dropped, ``truncated`` false."""
        state = _make_state(tmp_path)
        lead = "word " * ((MEMBER_BRIEFING_MAX_CHARS - 400) // 5)
        # Ten 60-char keys (~610 chars) push the raw text past the cap; each
        # collapses to a 22-char placeholder, so the redacted text fits.
        keys = " ".join("ghp_" + ("A" * 56) for _ in range(10))
        self._write_briefing(lead + keys + " last\n")
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert "ghp_" not in data["text"]
        assert data["text"].endswith("last")
        assert "briefing truncated" not in data["text"]
        assert data["redacted"] is True
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_successful_read_leaves_an_allowed_audit_row(self, tmp_path, monkeypatch):
        """WHO read a crewmate's private notes matters as much as who was
        refused (the rules read's posture): a denied-only trail cannot answer
        "was this boundary disclosed"."""
        state = _make_state(tmp_path)
        self._write_briefing("Owner-only working notes.\n")
        record: dict = {}

        class _RecordingSel:
            def log_api_access(self, **kwargs):
                record["kwargs"] = kwargs

        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: _RecordingSel())
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert resp.status == 200
        assert record["kwargs"]["operation"] == "members.briefing.read"
        assert record["kwargs"]["outcome"] == "allowed"
        assert record["kwargs"]["source"] == "dashboard"
        assert record["kwargs"]["resources"] == "slug=code-reviewer"


class TestDenialAuditOffload:
    """Deny-path SEL audits are direct enqueues, because startup warms SEL.

    A per-site ``asyncio.to_thread`` wrapper would only be needed if a fresh
    gateway's first ``_sel()`` touch performed synchronous filesystem
    initialization (HMAC key load/create, chain-head read). That first touch
    now happens once at gateway startup (``sel.warm_sel_singleton``, awaited
    by both server start paths — pinned in test_sel_startup_warm.py), so a
    handler-side ``log_api_access`` is a non-blocking enqueue and the thread
    hop is gone. Each test records the thread the audit ran on and fails if
    it is NOT the event-loop thread — re-adding a pointless per-site offload
    turns the recorded ident back into a worker's and fails these.
    """

    @staticmethod
    def _recording_sel(record: dict):
        class _RecordingSel:
            def log_api_access(self, **kwargs):
                record["thread_ident"] = threading.get_ident()
                record["kwargs"] = kwargs

        return _RecordingSel()

    @pytest.mark.asyncio
    async def test_app_denial_audit_runs_inline(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        record: dict = {}
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: self._recording_sel(record))

        @web.middleware
        async def _as_app(request: web.Request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_members_app(state)
        app.middlewares.insert(0, _as_app)
        loop_ident = threading.get_ident()
        with _patched_config([CREW]):
            async with TestClient(TestServer(app)) as client:
                assert (await client.get("/api/members")).status == 404
        assert record["kwargs"]["outcome"] == "denied"
        assert record["kwargs"]["source"] == "app_isolation"
        assert record["kwargs"]["operation"] == "members.list"
        # The audit is a direct enqueue on the loop thread — no thread hop.
        assert record["thread_ident"] == loop_ident

    @pytest.mark.asyncio
    async def test_member_pin_denial_audit_runs_inline(self, tmp_path, monkeypatch):
        """The pin-mismatch denial (binding names a non-owner) audits inline."""
        state = _make_state(tmp_path)
        record: dict = {}
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: self._recording_sel(record))
        # ``Code_Reviewer`` slugifies to CREW's slug (so the binding reads back
        # as present) but is NOT a config-registered owner → pin mismatch.
        write_dm_binding(CREW, member="Code_Reviewer", slot_key=member_slot_key(CREW))
        loop_ident = threading.get_ident()
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post(f"/api/members/{CREW}/thread")
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_pin_mismatch"
        assert record["kwargs"]["source"] == "member_pin"
        assert record["kwargs"]["outcome"] == "denied"
        assert record["thread_ident"] == loop_ident
        assert not state._slots

    def test_no_members_sel_audit_is_offloaded(self):
        """AST guard: no ``log_api_access`` call in members.py hides inside an
        ``asyncio.to_thread`` lambda.

        The startup warm makes a post-init ``log_api_access`` a non-blocking
        enqueue, so a per-site thread hop is pure overhead — an extra
        suspension point and a worker dispatch per denial. A future site that
        genuinely needs a synchronous write (``critical=True``) must offload
        AND adjust this guard with that reasoning.
        """
        import ast
        import importlib
        import inspect

        from kiro_crew.dashboard.handlers import members as members_mod_py

        source = inspect.getsource(members_mod_py)
        source += "".join(
            inspect.getsource(importlib.import_module(o)) for o in members_mod_py._OWNERS
        )
        tree = ast.parse(source)
        offloaded: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "to_thread"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "asyncio"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Lambda):
                        for inner in ast.walk(arg):
                            offloaded.add(id(inner))
        audits = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "log_api_access"
        ]
        assert audits, "expected log_api_access audit sites in members.py"
        wrapped = [node.lineno for node in audits if id(node) in offloaded]
        assert not wrapped, (
            f"to_thread-wrapped _sel().log_api_access at lines {wrapped}; SEL is "
            "warmed at startup (sel.warm_sel_singleton), so a non-critical audit "
            "is a direct enqueue (#8608)"
        )
