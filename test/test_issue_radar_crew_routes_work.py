"""Issue Radar's crew work-record route, ``PUT /crew/work``, for a numbered item.

A write upserts the item and appends its ledger event in one call, and a refused write
leaves no ledger line; the record tool's ``clear`` empties a label list; ``phase: skipped``
writes the repo-wide shared skip index in the same request; and one work write is one
crew-log entry. The agent's own session-resolved write and the numberless sweep are
tested in ``test_issue_radar_crew_routes.py``, and a non-finite number in a work body in
``test_issue_radar_crew_routes_controls.py``.
"""

import asyncio
from typing import Any
from unittest import mock

from aiohttp import web
from issue_radar_crew_routes_helpers import (
    BASE,
    OWNER,
    REPO,
    _CrewRouteCase,
    _payload,
    _tick,
)
from off_loop_helpers import off_loop

from kiro_crew import mcp_core
from kiro_crew.apps.builtins.issue_radar.backend import crew_routes, crew_store
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog

# ── PUT /crew/work ──────────────────────────────────────────────────────────


class TestWorkItems(_CrewRouteCase):
    async def test_it_upserts_the_item_and_logs_one_event_in_one_call(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 7,
                "phase": "implementing",
                "next": "write the failing test",
                "event": "reproduced the crash",
                "event_kind": "implement",
            },
        )
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["item"]["phase"], "implementing")
        self.assertEqual(page["item"]["next"], "write the failing test")
        self.assertEqual(page["event"]["kind"], "implement")
        self.assertEqual(page["event"]["text"], "reproduced the crash")
        self.assertEqual(page["event"]["number"], 7)
        self.assertEqual(len(self.ledger(crew["id"])), 1)

    async def test_the_envelope_keys_do_not_leak_into_the_work_item(self):
        crew = self.crew("Andromeda")
        item = _payload(
            await self.call(
                "PUT",
                "/crew/work",
                body={
                    "owner": OWNER,
                    "repo": REPO,
                    "crew_id": crew["id"],
                    "number": 7,
                    "phase": "claimed",
                    "event": "took it",
                    "event_kind": "claim",
                },
            )
        )["item"]
        self.assertNotIn("event", item)
        self.assertNotIn("event_kind", item)

    async def test_clear_empties_a_field_the_body_names(self):
        """`clear: ["pr_number"]` is how a caller says "no pull request any more":
        the tool schema types every field strictly, so a null cannot travel as the
        field itself. A field both cleared and set in one call keeps the set value."""
        crew = self.crew("Andromeda")
        base = {"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7}
        await self.call(
            "PUT",
            "/crew/work",
            body={
                **base,
                "phase": "awaiting-ci",
                "pr_number": 91,
                "next": "watch CI",
                "claim_comment_id": 5,
                "event": "opened the PR",
                "event_kind": "ci",
            },
        )
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                **base,
                "clear": ["pr_number", "next", "claim_comment_id"],
                "claim_comment_id": 6,
                "event": "PR closed, comment re-posted",
                "event_kind": "ci",
            },
        )
        self.assertEqual(res.status, 200)
        item = _payload(res)["item"]
        self.assertIsNone(item["pr_number"])
        self.assertEqual(item["next"], "")
        self.assertEqual(item["claim_comment_id"], 6, "set beats clear in one call")
        stored = off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        self.assertIsNone(stored["pr_number"])
        self.assertEqual(stored["phase"], "awaiting-ci", "an unnamed field is untouched")

    async def test_a_clear_naming_an_unknown_field_is_400_and_writes_nothing(self):
        crew = self.crew("Andromeda")
        base = {"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7}
        for bad in (["phase"], ["pr_numbre"], "pr_number", [7]):
            res = await self.call(
                "PUT",
                "/crew/work",
                body={**base, "clear": bad, "event": "x", "event_kind": "implement"},
            )
            self.assertEqual(res.status, 400, bad)
            self.assertEqual(_payload(res)["code"], "invalid_clear", bad)
        self.assertIsNone(
            off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        )
        # A crew-level line has no item to clear a field on.
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "clear": ["pr_number"],
                "event": "swept",
                "event_kind": "sweep",
            },
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "item_fields_without_number")

    async def test_an_append_the_writer_refused_is_503_not_a_record(self):
        """A drained writer whose entry is not in the log refused it. The caller is
        told so -- 503 `ledger_not_recorded`, retryable -- and is never handed an
        item that exists nowhere (the old answer was 200 with `durable: false`)."""
        from kiro_crew.crew_log import emit as crew_log_emit

        crew = self.crew("Andromeda")
        with mock.patch.object(crew_log_emit, "on_radar_recorded", lambda *_a, **_k: None):
            res = await self.call(
                "PUT",
                "/crew/work",
                body={
                    "owner": OWNER,
                    "repo": REPO,
                    "crew_id": crew["id"],
                    "number": 7,
                    "phase": "claimed",
                    "event": "took it",
                    "event_kind": "claim",
                },
            )
        self.assertEqual(res.status, 503)
        self.assertEqual(_payload(res)["code"], "ledger_not_recorded")
        self.assertIsNone(
            off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        )
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_a_write_without_an_event_is_refused(self):
        # The reason this route exists: a phase must not change without a logged
        # reason, so the log line is not optional.
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 7,
                "phase": "implementing",
                "event_kind": "implement",
            },
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "event_required")
        self.assertIsNone(
            off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        )

    async def test_an_unknown_event_kind_is_400_and_writes_nothing(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 7,
                "phase": "implementing",
                "event": "x",
                "event_kind": "vibes",
            },
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "invalid_event_kind")
        # Validated BEFORE the upsert, so the item was never written.
        self.assertIsNone(
            off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        )

    async def test_a_bad_number_is_400(self):
        crew = self.crew("Andromeda")
        for number in (0, -1, True, "7"):
            with self.subTest(number=number):
                res = await self.call(
                    "PUT",
                    "/crew/work",
                    body={
                        "owner": OWNER,
                        "repo": REPO,
                        "crew_id": crew["id"],
                        "number": number,
                        "event": "x",
                        "event_kind": "claim",
                    },
                )
                self.assertEqual(res.status, 400)

    async def test_a_second_editing_item_is_409(self):
        crew = self.crew("Andromeda")
        await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 7,
                "phase": "implementing",
                "event": "started #7",
                "event_kind": "implement",
            },
        )
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 8,
                "phase": "implementing",
                "event": "started #8",
                "event_kind": "implement",
            },
        )
        self.assertEqual(res.status, 409)
        self.assertIn("already editing #7", _payload(res)["error"])

    async def test_a_refused_write_leaves_no_ledger_line(self):
        # Ordering proof. The event is appended only AFTER the item write returns,
        # so the 409 above cannot leave the append-only log asserting a phase
        # change that never happened.
        crew = self.crew("Andromeda")
        await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 7,
                "phase": "implementing",
                "event": "started #7",
                "event_kind": "implement",
            },
        )
        await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 8,
                "phase": "implementing",
                "event": "started #8",
                "event_kind": "implement",
            },
        )
        texts = [e["text"] for e in self.ledger(crew["id"])]
        self.assertEqual(texts, ["started #7"])

    async def test_a_traversal_id_is_400_not_404_and_never_reaches_a_path(self):
        """A malformed id must be refused as a bad request.

        404 would be a lie (nothing was looked up) and 409 would tell a crew agent
        to retry an id that can never work. The dangerous ones are the path-shaped
        values: ``Path(store) / "/etc/policy"`` discards the base entirely, so an
        unchecked id is an arbitrary-file read here and an arbitrary-file write on
        the update route.
        """
        for bad in ("/etc/policy", "../../../../etc/policy", "c_1234abcd/../escape"):
            res = await self.call(
                "PUT",
                "/crew/work",
                body={
                    "owner": OWNER,
                    "repo": REPO,
                    "crew_id": bad,
                    "number": 7,
                    "event": "x",
                    "event_kind": "claim",
                },
            )
            self.assertEqual(res.status, 400, f"{bad!r} should be a bad request")

    async def test_an_unknown_crew_is_404(self):
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": "c_deadbeef",
                "number": 7,
                "event": "x",
                "event_kind": "claim",
            },
        )
        self.assertEqual(res.status, 404)

    async def test_a_retired_crew_cannot_take_new_work(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 7,
                "phase": "claimed",
                "event": "took it",
                "event_kind": "claim",
            },
        )
        self.assertEqual(res.status, 409)
        self.assertEqual(_payload(res)["code"], "crew_retired")


class TestTheRecordToolEmptiesLabelsWithClear(_CrewRouteCase):
    """``issue_radar_crew_record`` with ``clear: ["labels_applied"]``, end to end.

    A client that drops empty lists never delivers ``labels_applied: []``, so the
    tool description points a crew that removed its last label at the ``clear``
    spelling. The tool runs from its own entry (schema validation and handler) with
    its two loopback legs bridged onto the registered read and write handlers, which
    write the real store.
    """

    def agent(self, crew: dict) -> str:
        return f"dashboard:{crew['slot_key']}"

    async def record(self, crew: dict, args: dict[str, Any]) -> str:
        loop = asyncio.get_running_loop()

        def leg(method: str, path: str, body: Any, session_key: Any) -> dict:
            self.assertTrue(path.startswith(BASE), path)
            res = asyncio.run_coroutine_threadsafe(
                self.call(
                    method, path[len(BASE) :], body=body, internal_auth=True, session=session_key
                ),
                loop,
            ).result(timeout=30)
            payload = _payload(res)
            # The shape ``mcp_core._send`` hands back for a refusal.
            return payload if res.status < 400 else {"error": payload.get("error", ""), **payload}

        def get(path: str, session_key: Any = None, **_kw: Any) -> dict:
            return leg("GET", path, "", session_key)

        def put(path: str, body: Any = None, session_key: Any = None) -> dict:
            return leg("PUT", path, body, session_key)

        sk = self.agent(crew)
        with (
            mock.patch.object(mcp_core, "_resolve_session_key_strict", return_value=sk),
            mock.patch.object(mcp_core, "_get", side_effect=get),
            mock.patch.object(mcp_core, "_put", side_effect=put),
        ):
            return await asyncio.wait_for(
                asyncio.to_thread(mcp_core._call_tool, "issue_radar_crew_record", args), timeout=30
            )

    def stored_labels(self, crew: dict) -> object:
        item = off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        return item["labels_applied"]

    async def test_clear_empties_the_labels_the_crew_applied(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "implementing", labels_applied=["crew: in progress"])

        out = await self.record(
            crew,
            {
                "number": 7,
                "clear": ["labels_applied"],
                "event": "handed back",
                "event_kind": "implement",
            },
        )

        self.assertFalse(out.startswith("Error:"), out)
        self.assertEqual(self.stored_labels(crew), [])

    async def test_a_label_list_is_still_recorded_as_given(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "implementing", labels_applied=["crew: in progress"])

        out = await self.record(
            crew,
            {
                "number": 7,
                "labels_applied": ["crew: in progress", "crew: needs human"],
                "event": "asked for a human",
                "event_kind": "implement",
            },
        )

        self.assertFalse(out.startswith("Error:"), out)
        self.assertEqual(self.stored_labels(crew), ["crew: in progress", "crew: needs human"])

    async def test_a_misspelled_clear_is_refused_and_keeps_the_labels(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "implementing", labels_applied=["crew: in progress"])

        out = await self.record(
            crew,
            {"number": 7, "clear": ["labels"], "event": "handed back", "event_kind": "implement"},
        )

        self.assertTrue(out.startswith("Error:"), out)
        self.assertEqual(self.stored_labels(crew), ["crew: in progress"])


# ── the shared skip index ───────────────────────────────────────────────────


class TestSharedSkipIndex(_CrewRouteCase):
    """The index's whole value is that it crosses crews, so every assertion here
    writes as one crew and reads as another."""

    async def _skip(self, crew_id: str, number: int, **extra) -> web.Response:
        return await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew_id,
                "number": number,
                "phase": "skipped",
                "event": f"passing on #{number}",
                "event_kind": "skip",
                **extra,
            },
        )

    async def _page(self, crew_id: str) -> dict:
        return _payload(
            await self.call("GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": crew_id})
        )

    async def test_a_skip_by_one_crew_is_visible_to_another_through_get_crew(self):
        author = self.crew("Andromeda")
        reader = self.crew("Whirlpool")
        res = await self._skip(
            author["id"], 42, why="needs an owner decision", skip_scope="needs-design"
        )
        self.assertEqual(res.status, 200)

        page = await self._page(reader["id"])
        # The membership list the OTHER crew tests an issue against before it
        # spends a turn investigating.
        self.assertEqual(page["skipped_numbers"], [42])
        self.assertEqual(
            page["recent_skips"],
            [{"number": 42, "reason": "needs an owner decision", "scope": "needs-design"}],
        )
        # And it did not become the reader's own work item — the index is the
        # shared surface, the work item stays the author's.
        self.assertIsNone(
            off_loop(crew_store.read_work_item, OWNER, REPO, reader["id"], 42, self.root)
        )

    async def test_a_needs_human_pass_is_accepted_and_indexed_for_the_fleet(self):
        """The write path that replaces holding an issue for a human.

        This is the whole handover: the crew records ``skipped`` with a
        ``needs-decision`` scope, its claim is released, and the reason and scope are
        visible to every OTHER crew in the repo — so the next one does not
        re-investigate an issue that is already waiting on a person. Asserted through
        the route rather than the store because this route is the only place the "a
        pass is always indexed" invariant is enforced.
        """
        author = self.crew("Andromeda")
        reader = self.crew("Whirlpool")
        for number, scope in ((42, "needs-decision"), (43, "needs-investigation")):
            with self.subTest(scope=scope):
                res = await self._skip(
                    author["id"], number, why=f"a human must {scope}", skip_scope=scope
                )
                self.assertEqual(res.status, 200)
                self.assertEqual(_payload(res)["skip"]["scope"], scope)
                # Terminal, so the claim is released rather than held on the human.
                self.assertIn(_payload(res)["item"]["phase"], crew_store.TERMINAL_PHASES)
                page = await self._page(reader["id"])
                self.assertIn(number, page["skipped_numbers"])
                self.assertIn(
                    {"number": number, "reason": f"a human must {scope}", "scope": scope},
                    page["recent_skips"],
                )
        # Two passes, no slot held by either crew.
        self.assertEqual(
            off_loop(crew_store.open_slot_count, OWNER, REPO, author["id"], self.root), 0
        )

    async def test_a_skip_is_indexed_even_with_no_skip_scope(self):
        """The invariant, at its weakest input.

        ``skip_scope`` is optional so the existing work-write contract keeps
        working, so the interesting case is the write that omits it: it must still
        index, classified ``other``, with the progress line standing in as the
        reason when no prose was sent.
        """
        author = self.crew("Andromeda")
        reader = self.crew("Whirlpool")
        res = await self._skip(author["id"], 42)
        self.assertEqual(res.status, 200)
        self.assertEqual(_payload(res)["skip"]["scope"], "other")

        page = await self._page(reader["id"])
        self.assertEqual(page["skipped_numbers"], [42])
        self.assertEqual(
            page["recent_skips"],
            [{"number": 42, "reason": "passing on #42", "scope": "other"}],
        )

    async def test_an_unknown_skip_scope_is_indexed_as_other(self):
        crew = self.crew("Andromeda")
        await self._skip(crew["id"], 42, skip_scope="vibes")
        self.assertEqual((await self._page(crew["id"]))["recent_skips"][0]["scope"], "other")

    async def test_the_reason_prefers_the_prose_over_the_progress_line(self):
        crew = self.crew("Andromeda")
        await self._skip(crew["id"], 42, why="the fix needs a schema migration")
        page = await self._page(crew["id"])
        self.assertEqual(page["recent_skips"][0]["reason"], "the fix needs a schema migration")

    async def test_re_skipping_through_the_route_keeps_the_first_crews_reason(self):
        first = self.crew("Andromeda")
        second = self.crew("Whirlpool")
        await self._skip(first["id"], 42, why="first reason", skip_scope="architecture")
        res = await self._skip(second["id"], 42, why="second reason", skip_scope="duplicate")
        # The second crew is told what STANDS, not what it sent.
        self.assertEqual(_payload(res)["skip"]["reason"], "first reason")
        self.assertEqual(_payload(res)["skip"]["crew_id"], first["id"])
        page = await self._page(second["id"])
        self.assertEqual(
            page["recent_skips"],
            [{"number": 42, "reason": "first reason", "scope": "architecture"}],
        )

    async def test_a_phase_that_is_not_skipped_indexes_nothing(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 42,
                "phase": "resolved",
                "event": "merged",
                "event_kind": "merge",
            },
        )
        self.assertIsNone(_payload(res)["skip"])
        page = await self._page(crew["id"])
        self.assertEqual(page["skipped_numbers"], [])
        self.assertEqual(page["recent_skips"], [])

    async def test_recent_skips_is_bounded_while_the_number_list_is_not(self):
        # The two fields answer different questions and so carry different bounds:
        # prose is capped for the crew's context, membership must stay complete or
        # it answers "not skipped" for an issue that is.
        crew = self.crew("Andromeda")
        total = crew_routes._MAX_RECENT_SKIPS + 5
        for number in range(1, total + 1):
            await self._skip(crew["id"], number)
        page = await self._page(crew["id"])
        self.assertEqual(len(page["skipped_numbers"]), total)
        self.assertEqual(len(page["recent_skips"]), crew_routes._MAX_RECENT_SKIPS)


# ── one work write is one crew log entry ────────────────────────────────────


class TestWorkWriteIsOneEntry(_CrewRouteCase):
    """The write route appends ONE ``radar/recorded`` entry to the crew's live crew
    log, and answers with the record that entry produces.

    The all-or-nothing property the retired file store bought with three locks and
    a rollback is now a property of the append: the item's delta, the line that
    explains it and the skip row that indexes a pass ride on one entry, so there is
    no partial state for a crash to leave and no rollback to get wrong. What the
    route owes instead is a NAMED refusal when the crew has nowhere to record, so an
    agent can tell "the log is not there" from "the update was refused".
    """

    def agent(self, crew: dict) -> str:
        return f"dashboard:{crew['slot_key']}"

    def _entries(self, crew: dict) -> list:
        return off_loop(self._read_entries, self._units[crew["id"]])

    @staticmethod
    def _read_entries(unit_id: str) -> list:
        handle = CrewLog.open(KIND_SESSION, unit_id)
        try:
            return [e for e in handle.iter_from(1) if e.type == crew_store.LEDGER_ENTRY_TYPE]
        finally:
            del handle

    async def test_a_pass_is_one_entry_carrying_item_line_and_skip(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "number": 7,
                "phase": "skipped",
                "event": "duplicate of #5",
                "event_kind": "skip",
                "skip_reason": "duplicate of #5",
                "skip_scope": "duplicate",
            },
            internal_auth=True,
            session=self.agent(crew),
        )
        self.assertEqual(res.status, 200)
        body = _payload(res)
        self.assertEqual(body["item"]["phase"], "skipped")
        self.assertEqual(body["skip"]["scope"], "duplicate")
        self.assertEqual(body["event"]["text"], "duplicate of #5")
        (entry,) = self._entries(crew)
        self.assertEqual(entry.data["phase"], "skipped")
        self.assertEqual(entry.data["skip"], {"reason": "duplicate of #5", "scope": "duplicate"})
        self.assertEqual(entry.data["event_kind"], "skip")
        # The answer IS the line the log holds -- same id, same stamp.
        self.assertEqual(self.ledger(crew["id"])[0], body["event"])
        self.assertTrue(body["durable"])

    async def test_a_refused_update_appends_nothing(self):
        """The store's refusals run against the folded record BEFORE the append, so a
        conflict leaves the log exactly as it was -- an append-only log cannot take
        a line back."""
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "implementing")
        before = self._entries(crew)
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "number": 9,
                "phase": "implementing",
                "event": "also editing",
                "event_kind": "implement",
            },
            internal_auth=True,
            session=self.agent(crew),
        )
        self.assertEqual(res.status, 409)
        self.assertEqual(_payload(res)["code"], "crew_conflict")
        self.assertEqual(self._entries(crew), before)
        self.assertIsNone(
            off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 9, self.root)
        )

    async def test_a_crew_whose_slot_has_no_live_session_is_told_so(self):
        """No live unit, no record: 409 with its own code, not a generic conflict, so
        the agent stops retrying the same body and reports the condition."""
        crew = self.crew("Andromeda", live=False)
        res = await self.call(
            "PUT",
            "/crew/work",
            body={"number": 7, "phase": "claimed", "event": "took #7", "event_kind": "claim"},
            internal_auth=True,
            session=self.agent(crew),
        )
        self.assertEqual(res.status, 409)
        self.assertEqual(_payload(res)["code"], "crew_log_unavailable")
        self.assertIn("no live session", _payload(res)["error"])
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_a_live_session_whose_log_has_not_been_created_yet_is_told_so(self):
        crew = self.crew("Andromeda", live=False)
        self.state.sessions.bind(f"dashboard:{crew['slot_key']}", "acp-not-yet")
        res = await self.call(
            "PUT",
            "/crew/work",
            body={"event": "queue empty", "event_kind": "sweep"},
            internal_auth=True,
            session=self.agent(crew),
        )
        self.assertEqual(res.status, 409)
        self.assertEqual(_payload(res)["code"], "crew_log_unavailable")
        self.assertIn("first turn", _payload(res)["error"])

    async def test_an_update_that_cannot_fit_one_entry_is_413(self):
        """Not a conflict: it can never land however often it is retried."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew/work",
            body={"number": 7, "next": "x" * (100 * 1024), "event": "huge", "event_kind": "claim"},
            internal_auth=True,
            session=self.agent(crew),
        )
        self.assertEqual(res.status, 413)
        self.assertEqual(_payload(res)["code"], "ledger_entry_too_large")
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_the_entry_lands_in_the_crews_unit_whoever_writes(self):
        """The entry goes to the CREW's log, resolved from the crew's slot -- never
        to the caller's session. The dashboard writing on a crew's behalf records
        into the same unit the crew's own agent does."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "owner": OWNER,
                "repo": REPO,
                "crew_id": crew["id"],
                "number": 7,
                "phase": "claimed",
                "event": "took #7",
                "event_kind": "claim",
            },
        )
        self.assertEqual(res.status, 200)
        self.assertEqual([e.data["number"] for e in self._entries(crew)], [7])

    async def test_a_crew_that_restarted_folds_its_earlier_units(self):
        """A slot owns one ACP session id at a time; the record spans every unit the
        slot ran under, and a write after a restart lands in the NEW unit while the
        read still joins the old one."""
        crew = self.crew("Andromeda")
        first = self._units[crew["id"]]
        self.work(crew["id"], 7, "claimed", next="read it")
        _tick()
        second = self.unit(crew)  # the slot came back on a new session
        res = await self.call(
            "PUT",
            "/crew/work",
            body={
                "number": 7,
                "phase": "implementing",
                "event": "fixing",
                "event_kind": "implement",
            },
            internal_auth=True,
            session=self.agent(crew),
        )
        self.assertEqual(res.status, 200)
        item = _payload(res)["item"]
        self.assertEqual((item["phase"], item["next"]), ("implementing", "read it"))
        self.assertEqual(
            crew_store.crew_log_units(OWNER, REPO, crew["id"], self.root), (first, second)
        )
        page = _payload(
            await self.call("GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": crew["id"]})
        )
        self.assertEqual([e["text"] for e in page["events"]], ["fixing", "seeded"])
