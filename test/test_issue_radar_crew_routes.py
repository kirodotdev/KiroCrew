"""Tests for Issue Radar's crew HTTP surface (``backend/crew_routes.py``).

Every test calls the **registered** handler — looked up out of a real
``web.Application`` by method and path — rather than the bare function. That is
deliberate: it means each happy path also proves the route exists at the verb and
path the contract names, and that it is wrapped in both the ``_require_enabled``
gate and the ``CrewStoreError -> 409`` guard. A route that is written but never
registered, or registered without a gate, fails here instead of in production.

The coverage is weighted toward the conditions whose failure is otherwise silent:

  * **The gates.** All 13 routes are asserted denied when the app is disabled and
    404 when the repo is not connected — as a table, so a route added later
    without a gate fails the inventory test rather than shipping open.
  * **409, never 500.** A duplicate crew name and a second item entering an
    editing phase are ordinary user conditions; surfaced as 500 they read as "the
    backend broke" and a crew agent retries them forever.
  * **The ledger cannot lie.** ``PUT /crew/work`` writes the item and appends the
    event in one call, and a refused write must leave NO ledger line behind.
  * **A skip is always indexed.** ``phase: skipped`` through ``PUT /crew/work``
    writes the repo-wide shared index in the same request. That is the load-bearing
    invariant of the index: if a pass can be recorded without indexing it, every
    other crew re-investigates the issue, which is the waste the index exists to
    remove. Asserted from ANOTHER crew's ``GET /crew``, because visible-to-me is
    not the property — visible-to-the-fleet is.
  * **``working`` is the NEWEST open item**, not any of them — the difference only
    shows up on a crew holding one parked and one active item.
  * **Nothing waits for a human.** There is no route through which a person answers
    a crew and no queue of items held for one, so the tests assert those endpoints
    are GONE rather than that they behave — a crew that needs a decision labels the
    issue, records the pass and releases its claim.

Data isolation is one patch: ``routes._scope`` is the only thing that decides
where crew records land, so pointing it at a ``TemporaryDirectory`` keeps every
store write inside the test. Nothing here touches the network or a real data home.

The numbered work-record writes (the ledger, the shared skip index, one crew-log entry per
write) are tested in ``test_issue_radar_crew_routes_work.py``; pause, stop, an update that
withdraws auto-approval, settings, the absent human-wait endpoints, the issue comment and
non-finite numbers in ``test_issue_radar_crew_routes_controls.py``. The base case, the shared
fakes and the request helpers live in ``issue_radar_crew_routes_helpers.py``.
"""

import unittest
from unittest import mock

from issue_radar_crew_routes_helpers import (
    OWNER,
    REPO,
    _CrewRouteCase,
    _payload,
    _registered,
    _Slot,
    _tick,
)
from off_loop_helpers import off_loop

from kiro_crew import mcp_core
from kiro_crew.apps.builtins.issue_radar.backend import crew_routes, crew_store, routes, store

#: The contract, as a table. Also the inventory the registrar is checked against.
CREW_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/crews"),
    ("POST", "/crews"),
    ("GET", "/crews/names"),
    ("GET", "/crews/settings"),
    ("PUT", "/crews/settings"),
    ("GET", "/crew"),
    ("GET", "/crew/fabric"),
    ("PUT", "/crew"),
    ("DELETE", "/crew"),
    ("PUT", "/crew/work"),
    ("POST", "/crew/pause"),
    ("POST", "/issue/comment"),
)

#: A minimally-valid request per route, used by the two table-driven gate tests.
#: Each one would SUCCEED if the gates passed, so a 403/404 can only come from the
#: gate under test rather than from validation firing first.
_MINIMAL: dict[tuple[str, str], dict] = {
    ("GET", "/crews"): {"query": {"owner": OWNER, "repo": REPO}},
    ("POST", "/crews"): {"body": {"owner": OWNER, "repo": REPO, "name": "Andromeda"}},
    ("GET", "/crews/names"): {"query": {"owner": OWNER, "repo": REPO}},
    ("GET", "/crews/settings"): {"query": {"owner": OWNER, "repo": REPO}},
    ("PUT", "/crews/settings"): {
        "body": {"owner": OWNER, "repo": REPO, "settings": {"claim_ttl_hours": 12}}
    },
    ("GET", "/crew"): {"query": {"owner": OWNER, "repo": REPO, "id": "c_dead"}},
    ("GET", "/crew/fabric"): {"query": {"owner": OWNER, "repo": REPO}},
    ("PUT", "/crew"): {"body": {"owner": OWNER, "repo": REPO, "id": "c_dead"}},
    ("DELETE", "/crew"): {"body": {"owner": OWNER, "repo": REPO, "id": "c_dead"}},
    ("PUT", "/crew/work"): {
        "body": {
            "owner": OWNER, "repo": REPO, "crew_id": "c_dead", "number": 7,
            "phase": "claimed", "event": "claimed it", "event_kind": "claim",
        }
    },
    ("POST", "/crew/pause"): {
        "body": {"owner": OWNER, "repo": REPO, "id": "c_dead", "paused": True}
    },
    ("POST", "/issue/comment"): {
        "body": {"owner": OWNER, "repo": REPO, "number": 7, "body": "hello"}
    },
}


# ── registration and the two gates ──────────────────────────────────────────


class TestRegistrationAndGates(_CrewRouteCase):
    def test_the_registrar_installs_exactly_the_documented_route_table(self):
        # An inventory, not a spot check: a route dropped by a bad merge, or one
        # added at a path the frontend does not call, both show up here.
        self.assertEqual(sorted(_registered()), sorted(CREW_ROUTES))

    async def test_every_route_is_denied_while_the_app_is_disabled(self):
        # Routes register ONCE at gateway startup and the app is
        # defaultEnabled:false, so an unwrapped handler stays callable while the
        # app is switched off.
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            for method, path in CREW_ROUTES:
                with self.subTest(route=f"{method} {path}"):
                    res = await self.call(method, path, **_MINIMAL[(method, path)])
                    self.assertEqual(res.status, 403)
                    self.assertIn("disabled", _payload(res)["error"])

    async def test_every_route_rejects_a_repo_that_is_not_connected(self):
        with mock.patch.object(store, "is_repo_connected", return_value=False):
            for method, path in CREW_ROUTES:
                with self.subTest(route=f"{method} {path}"):
                    res = await self.call(method, path, **_MINIMAL[(method, path)])
                    self.assertEqual(res.status, 404)
                    self.assertEqual(_payload(res)["code"], "repo_not_connected")

    async def test_a_malformed_body_is_400_not_500(self):
        for method, path in CREW_ROUTES:
            if "body" not in _MINIMAL[(method, path)]:
                continue
            with self.subTest(route=f"{method} {path}"):
                res = await self.call(method, path, body=None)
                self.assertEqual(res.status, 400)
                self.assertEqual(_payload(res)["code"], "invalid_json")

    async def test_a_missing_repo_is_400_before_anything_else(self):
        self.assertEqual((await self.call("GET", "/crews")).status, 400)
        res = await self.call("POST", "/crews", body={"name": "Andromeda"})
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "missing_repo")

    async def test_local_crew_routes_do_not_require_write_access(self):
        # The write-permission decision, asserted rather than described: a
        # read-only repo can still hold crews, because nothing on this path
        # reaches the forge and _repo_can_write fails CLOSED on a transient error.
        with mock.patch.object(routes, "_repo_can_write", return_value=None) as gate:
            res = await self.call(
                "POST", "/crews", body={"owner": OWNER, "repo": REPO, "name": "Andromeda"}
            )
        self.assertEqual(res.status, 200)
        gate.assert_not_called()


# ── GET /crews ──────────────────────────────────────────────────────────────


class TestCrewsList(_CrewRouteCase):
    def setUp(self) -> None:
        super().setUp()
        # Stamps come off the crew log's own clock, so recency between two writes
        # is established by letting that clock move (``_tick``) rather than by
        # patching a timestamp source.

    async def test_returns_crews_settings_and_counts(self):
        self.crew("Andromeda")
        res = await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["owner"], OWNER)
        self.assertEqual(page["repo"], REPO)
        self.assertEqual([c["name"] for c in page["crews"]], ["Andromeda"])
        self.assertEqual(page["settings"]["claim_ttl_hours"], 48)
        # A new crew is enabled and holds nothing: on duty, and nothing else.
        self.assertEqual(page["counts"], {"on_duty": 1, "working": 0, "paused": 0})
        self.assertEqual(page["crews"][0]["status"], "idle")

    async def test_working_reads_the_newest_open_item_not_any_of_them(self):
        # The crew holds one item parked on CI and one being implemented. "Any
        # non-parked item" and "the newest item" agree here only because the
        # implement came second — which is the whole point of the rule.
        crew = self.crew("Andromeda")
        self.work(crew["id"], 1, "awaiting-ci")
        _tick()
        self.work(crew["id"], 2, "implementing")
        counts = _payload(
            await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO})
        )["counts"]
        self.assertEqual(counts["working"], 1)

        # Progress on the older parked item makes it newest without creating an item.
        _tick()
        self.progress(crew["id"], 1, next="waiting for checks")
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        self.assertEqual(page["counts"]["working"], 0)
        self.assertEqual(page["crews"][0]["status"], "idle")

    async def test_a_crew_parked_on_its_newest_item_is_not_working(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 1, "implementing")
        _tick()
        self.work(crew["id"], 2, "awaiting-ci")
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        self.assertEqual(page["counts"]["working"], 0)
        self.assertEqual(page["crews"][0]["status"], "idle")

        _tick()
        self.progress(crew["id"], 1, next="implementing the next step")
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        self.assertEqual(page["counts"]["working"], 1)
        self.assertEqual(page["crews"][0]["status"], "working")

    async def test_a_retired_crew_is_neither_listed_nor_on_duty(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        self.assertEqual(page["crews"], [])
        # Retired is NOT paused: the crew is gone, not stopped.
        self.assertEqual(page["counts"], {"on_duty": 0, "working": 0, "paused": 0})

    async def test_the_counts_are_independent_predicates_not_a_partition(self):
        working = self.crew("Andromeda")
        self.work(working["id"], 1, "implementing")
        parked = self.crew("Whirlpool")
        self.work(parked["id"], 2, "awaiting-reply")
        paused = self.crew("Sombrero", enabled=False)
        self.work(paused["id"], 3, "implementing")
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        # The paused crew still holds an in-flight item, so it is counted twice —
        # each chip is its own filter, so the numbers may sum past the crew count.
        self.assertEqual(page["counts"], {"on_duty": 3, "working": 2, "paused": 1})
        by_name = {c["name"]: c["status"] for c in page["crews"]}
        self.assertEqual(by_name["Andromeda"], "working")
        # Parked on a human's reply, so not the actor — but it is not a status of
        # its own either: there is no per-crew "needs you", because a crew that
        # needs a decision hands the issue back instead of holding it.
        self.assertEqual(by_name["Whirlpool"], "idle")
        self.assertEqual(by_name["Sombrero"], "paused")

    async def test_no_crew_status_reports_waiting_on_a_human(self):
        """The status dot has three states and none of them is "needs you".

        Pinned as its own test because the flag it replaces was reachable from every
        phase in ``PARKED_PHASES``: a re-added "waiting on a human" state would make
        the roster show a crew as blocked while the issue it is supposedly blocked on
        has already been labelled and released.
        """
        crew = self.crew("Andromeda")
        for number, phase in enumerate(sorted(crew_routes.PARKED_PHASES), start=1):
            self.work(crew["id"], number, phase)
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        self.assertNotIn("needs_you", page["counts"])
        self.assertIn(page["crews"][0]["status"], {"idle", "working", "paused"})


# ── POST /crews, GET /crews/names ───────────────────────────────────────────


class TestCreateAndNames(_CrewRouteCase):
    async def test_create_returns_the_crew(self):
        res = await self.call(
            "POST", "/crews", body={"owner": OWNER, "repo": REPO, "name": "Andromeda", "max_open": 5}
        )
        self.assertEqual(res.status, 200)
        crew = _payload(res)["crew"]
        self.assertTrue(crew["id"].startswith("c_"))
        self.assertEqual(crew["slot_key"], f"crew-{crew['id']}")
        self.assertEqual(crew["max_open"], 5)

    async def test_create_without_a_name_is_400(self):
        res = await self.call("POST", "/crews", body={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "name_required")

    async def test_a_duplicate_name_is_409_with_the_stores_own_message(self):
        self.crew("Andromeda")
        res = await self.call(
            "POST", "/crews", body={"owner": OWNER, "repo": REPO, "name": "Andromeda"}
        )
        self.assertEqual(res.status, 409)
        self.assertIn("already taken", _payload(res)["error"])
        self.assertEqual(_payload(res)["code"], "crew_conflict")

    async def test_a_retired_crews_name_is_still_taken(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self.call(
            "POST", "/crews", body={"owner": OWNER, "repo": REPO, "name": "Andromeda"}
        )
        self.assertEqual(res.status, 409)

    async def test_names_suggests_unused_pool_names(self):
        self.crew("Andromeda")
        res = await self.call("GET", "/crews/names", query={"owner": OWNER, "repo": REPO})
        suggestions = _payload(res)["suggestions"]
        self.assertEqual(len(suggestions), 6)
        self.assertNotIn("Andromeda", suggestions)

    async def test_the_suggestion_limit_is_clamped(self):
        res = await self.call(
            "GET", "/crews/names", query={"owner": OWNER, "repo": REPO, "limit": "9999"}
        )
        self.assertLessEqual(len(_payload(res)["suggestions"]), crew_routes._MAX_SUGGESTIONS)


# ── GET/PUT/DELETE /crew ────────────────────────────────────────────────────


class TestCrewReadUpdateRetire(_CrewRouteCase):
    async def test_read_returns_the_crew_its_items_events_and_counts(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "implementing")
        self.work(crew["id"], 8, "skipped")
        _tick()
        self.progress(crew["id"], 7, kind="claim", text="took #7")
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": crew["id"]}
        )
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["crew"]["name"], "Andromeda")
        self.assertEqual(sorted(it["number"] for it in page["items"]), [7, 8])
        # Newest first; the two seeding writes left their own lines behind it.
        self.assertEqual([e["text"] for e in page["events"]], ["took #7", "seeded", "seeded"])
        # A pass is terminal, so it frees the slot. Beside `open`, the fold's own
        # eviction counters: they exist so a crew is told once per snapshot that a
        # bounded collection dropped something, and this payload is what
        # `issue_radar_crew_read` serves, so leaving them in the fold value alone
        # meant nothing a crew reads ever said it.
        self.assertEqual(page["counts"], {"open": 1, "evicted_items": 0, "evicted_skips": 0})

    async def test_read_of_an_unknown_crew_is_404_not_409(self):
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": "c_deadbeef"}
        )
        self.assertEqual(res.status, 404)
        self.assertEqual(_payload(res)["code"], "crew_not_found")

    async def test_read_without_an_id_is_400(self):
        res = await self.call("GET", "/crew", query={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "missing_crew_id")

    async def test_a_retired_crews_page_still_opens(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": crew["id"]}
        )
        self.assertEqual(res.status, 200)
        self.assertTrue(_payload(res)["crew"]["retired_at"])

    async def test_update_merges_a_patch_and_keeps_the_face(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "name": "Whirlpool",
                  "auto_merge": False, "not_a_field": "x"},
        )
        self.assertEqual(res.status, 200)
        updated = _payload(res)["crew"]
        self.assertEqual(updated["name"], "Whirlpool")
        self.assertEqual(updated["auto_merge"], False)
        self.assertEqual(updated["avatar_seed"], "Andromeda")
        self.assertNotIn("not_a_field", updated)

    async def test_the_triage_model_is_stored_and_a_bad_id_is_dropped(self):
        crew = self.crew("Andromeda")
        self.assertEqual(crew["triage_model"], "")
        body = {"owner": OWNER, "repo": REPO, "id": crew["id"]}
        res = await self.call("PUT", "/crew", body={**body, "triage_model": "claude-haiku-4.5"})
        self.assertEqual(_payload(res)["crew"]["triage_model"], "claude-haiku-4.5")
        res = await self.call("PUT", "/crew", body={**body, "triage_model": "not a model"})
        self.assertEqual(_payload(res)["crew"]["triage_model"], "claude-haiku-4.5")
        res = await self.call("PUT", "/crew", body={**body, "triage_model": ""})
        self.assertEqual(_payload(res)["crew"]["triage_model"], "")

    async def test_the_crew_page_names_a_triage_model_the_session_withheld(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_model

        crew = self.crew("Andromeda", triage_model="claude-haiku-4.5")
        query = {"owner": OWNER, "repo": REPO, "id": crew["id"]}
        page = _payload(await self.call("GET", "/crew", query=query))
        self.assertEqual(page["triage_model_unavailable"], "")
        crew_model._withheld[crew["id"]] = "claude-haiku-4.5"
        self.addCleanup(crew_model._withheld.clear)
        page = _payload(await self.call("GET", "/crew", query=query))
        self.assertEqual(page["triage_model_unavailable"], "claude-haiku-4.5")

    async def test_the_crew_page_names_a_hand_picked_model_and_resume_releases_it(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_model

        crew = self.crew("Andromeda", model="claude-opus-4.5", triage_model="claude-haiku-4.5")
        query = {"owner": OWNER, "repo": REPO, "id": crew["id"]}
        page = _payload(await self.call("GET", "/crew", query=query))
        self.assertIs(page["model_held"], False)
        self.assertEqual(page["held_model"], "")

        slot = _Slot()
        slot.model = "claude-sonnet-4.5"  # type: ignore[attr-defined]
        self.state._slot = slot
        off_loop(
            crew_store.set_crew_model_held,
            OWNER,
            REPO,
            crew["id"],
            model="claude-opus-4.5",
            triage_model="claude-haiku-4.5",
            seen_releases=crew_store.hold_releases(crew["id"]),
            root=self.root,
        )
        page = _payload(await self.call("GET", "/crew", query=query))
        self.assertIs(page["model_held"], True)
        self.assertEqual(page["held_model"], "claude-sonnet-4.5")

        # "Resume switching": a patch may release the hold, in the record and in
        # process memory, but never set it.
        crew_model._held[crew["id"]] = ("claude-opus-4.5", "claude-haiku-4.5")
        self.addCleanup(crew_model._held.clear)
        body = {"owner": OWNER, "repo": REPO, "id": crew["id"]}
        res = await self.call("PUT", "/crew", body={**body, "model_held": False})
        self.assertIs(_payload(res)["crew"]["model_held"], False)
        self.assertNotIn(crew["id"], crew_model._held)
        page = _payload(await self.call("GET", "/crew", query=query))
        self.assertIs(page["model_held"], False)
        res = await self.call("PUT", "/crew", body={**body, "model_held": True})
        self.assertIs(_payload(res)["crew"]["model_held"], False)

    async def test_a_hold_not_yet_recorded_is_not_shown_and_a_release_still_clears_it(self):
        """The GPT finding: persist a hold before the page offers to release it.

        A hold this process has seen but not yet written is not on the page, and an
        explicit ``model_held: false`` patch clears it from process memory even though
        the record never carried it (a false-to-false record update).
        """
        from kiro_crew.apps.builtins.issue_radar.backend import crew_model

        crew = self.crew("Andromeda", model="claude-opus-4.5", triage_model="claude-haiku-4.5")
        query = {"owner": OWNER, "repo": REPO, "id": crew["id"]}
        crew_model._held[crew["id"]] = ("claude-opus-4.5", "claude-haiku-4.5")
        self.addCleanup(crew_model._held.clear)
        page = _payload(await self.call("GET", "/crew", query=query))
        self.assertIs(page["model_held"], False)
        self.assertEqual(page["held_model"], "")
        body = {"owner": OWNER, "repo": REPO, "id": crew["id"], "model_held": False}
        res = await self.call("PUT", "/crew", body=body)
        self.assertIs(_payload(res)["crew"]["model_held"], False)
        self.assertNotIn(crew["id"], crew_model._held)
        self.assertFalse(crew_model.is_held(_payload(res)["crew"]))

    async def test_a_hold_write_queued_before_switch_back_does_not_undo_it(self):
        """The second GPT finding: a hold write that was queued when the person
        clicked "Switch back" must not land after the release and re-hold the crew.

        Queue the write (capture what it was seen under), release through the page,
        then flush it: the release wins, on the record and on the page.
        """
        crew = self.crew("Andromeda", model="claude-opus-4.5", triage_model="claude-haiku-4.5")
        query = {"owner": OWNER, "repo": REPO, "id": crew["id"]}
        settings = {"model": "claude-opus-4.5", "triage_model": "claude-haiku-4.5"}
        self.assertTrue(
            off_loop(
                crew_store.set_crew_model_held,
                OWNER,
                REPO,
                crew["id"],
                **settings,
                seen_releases=crew_store.hold_releases(crew["id"]),
                root=self.root,
            )
        )
        # A second write of the same hold, queued from a snapshot read before the
        # first one landed.
        queued = crew_store.hold_releases(crew["id"])
        body = {"owner": OWNER, "repo": REPO, "id": crew["id"], "model_held": False}
        res = await self.call("PUT", "/crew", body=body)
        self.assertIs(_payload(res)["crew"]["model_held"], False)
        wrote = off_loop(
            crew_store.set_crew_model_held,
            OWNER,
            REPO,
            crew["id"],
            **settings,
            seen_releases=queued,
            root=self.root,
        )
        self.assertFalse(wrote, "a hold seen before the release must not land after it")
        page = _payload(await self.call("GET", "/crew", query=query))
        self.assertIs(page["model_held"], False)

    async def test_a_rename_onto_a_taken_name_is_409(self):
        first = self.crew("Andromeda")
        self.crew("Whirlpool")
        res = await self.call(
            "PUT", "/crew", body={"owner": OWNER, "repo": REPO, "id": first["id"], "name": "Whirlpool"}
        )
        self.assertEqual(res.status, 409)

    async def test_delete_retires_and_keeps_the_record(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "DELETE", "/crew", body={"owner": OWNER, "repo": REPO, "id": crew["id"]}
        )
        self.assertEqual(res.status, 200)
        retired = _payload(res)["crew"]
        self.assertTrue(retired["retired_at"])
        self.assertIs(retired["enabled"], False)
        # The record survives — the name stays reserved and the work log readable.
        self.assertIsNotNone(crew_store.read_crew(OWNER, REPO, crew["id"], self.root))


# ── identity: an agent is WHO ITS SESSION SAYS ──────────────────────────────


class TestAgentIdentityIsTheSession(_CrewRouteCase):
    """``GET /crew`` and ``PUT /crew/work`` as the CREW itself calls them.

    That call carries the internal secret and an ``X-Session-Key``, and NO owner,
    repo or crew id anywhere — ``issue_radar_crew_read`` takes no arguments at all,
    so a parameterless read is the only shape the tool ever produces.

    These exercise the registered handlers. The MCP-tool tests stub the loopback
    call and assert on a fixture payload, so a route that answers 400 to the only
    request the tool can make passes there and fails here — which is exactly what
    happened: the read tool documented session-resolved identity and the route read
    ``?owner&repo&id``, so both crew tools returned 400 in production while the
    stubbed tests stayed green.
    """

    def agent(self, crew: dict) -> str:
        """The session key a crew's own turn sends: ``dashboard:<slot key>``."""
        return f"dashboard:{crew['slot_key']}"

    async def test_a_parameterless_read_resolves_the_crew_from_the_session(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "implementing")
        res = await self.call("GET", "/crew", internal_auth=True, session=self.agent(crew))
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["crew"]["id"], crew["id"])
        self.assertEqual([it["number"] for it in page["items"]], [7])

    async def test_the_response_satisfies_the_write_tools_identity_contract(self):
        # The bridge between the two files: ``issue_radar_crew_record`` reads
        # identity out of THIS payload and puts it in its PUT body, so the read
        # answering 200 is not enough — the triple has to be extractable. Asserted
        # on a crew with NO work items, because the top-level echo is then the only
        # place owner/repo can come from.
        crew = self.crew("Andromeda")
        res = await self.call("GET", "/crew", internal_auth=True, session=self.agent(crew))
        self.assertEqual(res.status, 200)
        self.assertEqual(
            mcp_core._crew_identity(_payload(res)), (OWNER, REPO, crew["id"])
        )

    async def test_the_read_carries_the_repo_protocol_settings(self):
        # A resuming crew negotiates its claim against the TTL and applies the
        # repo's needs-a-human label when it passes an issue back; it cannot read
        # either anywhere else.
        crew = self.crew("Andromeda")
        res = await self.call("GET", "/crew", internal_auth=True, session=self.agent(crew))
        settings = _payload(res)["settings"]
        self.assertIn("claim_ttl_hours", settings)
        self.assertEqual(
            settings["needs_human_label"],
            crew_store.DEFAULT_SETTINGS["needs_human_label"],
        )

    async def test_a_bare_slot_key_session_resolves_too(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "GET", "/crew", internal_auth=True, session=crew["slot_key"]
        )
        self.assertEqual(res.status, 200)
        self.assertEqual(_payload(res)["crew"]["id"], crew["id"])

    async def test_a_session_key_with_trailing_space_still_resolves(self):
        # Inconsistent normalization in an identity comparison would report a real
        # crew as "not a crew" and strand it with no ledger.
        crew = self.crew("Andromeda")
        res = await self.call(
            "GET", "/crew", internal_auth=True, session=f" {self.agent(crew)} "
        )
        self.assertEqual(res.status, 200)

    async def test_a_supplied_id_cannot_redirect_the_read_to_another_crew(self):
        mine = self.crew("Andromeda")
        theirs = self.crew("Whirlpool")
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": theirs["id"]},
            internal_auth=True, session=self.agent(mine),
        )
        self.assertEqual(res.status, 403)
        self.assertEqual(_payload(res)["code"], "crew_identity_mismatch")

    async def test_a_supplied_repo_cannot_redirect_the_read(self):
        mine = self.crew("Andromeda")
        res = await self.call(
            "GET", "/crew", query={"owner": "other", "repo": "elsewhere", "id": mine["id"]},
            internal_auth=True, session=self.agent(mine),
        )
        self.assertEqual(res.status, 403)
        self.assertEqual(_payload(res)["code"], "crew_identity_mismatch")

    async def test_its_own_identity_in_the_query_is_not_a_mismatch(self):
        mine = self.crew("Andromeda")
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": mine["id"]},
            internal_auth=True, session=self.agent(mine),
        )
        self.assertEqual(res.status, 200)

    async def test_a_session_that_is_not_a_crew_is_refused(self):
        self.crew("Andromeda")
        res = await self.call(
            "GET", "/crew", internal_auth=True, session="dashboard:chat-3-1730000000"
        )
        self.assertEqual(res.status, 403)
        self.assertEqual(_payload(res)["code"], "not_a_crew_session")

    async def test_a_read_with_no_session_key_is_refused(self):
        self.crew("Andromeda")
        res = await self.call("GET", "/crew", internal_auth=True)
        self.assertEqual(res.status, 403)
        self.assertEqual(_payload(res)["code"], "missing_session")

    async def test_the_browser_path_is_unchanged(self):
        # No ``internal_auth``: a session key alone must not buy identity, or any
        # chat session could read any crew's ledger with a cookie and a header.
        crew = self.crew("Andromeda")
        res = await self.call("GET", "/crew", session=self.agent(crew))
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "missing_repo")

    async def test_a_work_write_needs_no_owner_repo_or_crew_id(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"number": 7, "phase": "claimed", "event": "took #7", "event_kind": "claim"},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual(res.status, 200)
        item = off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        self.assertEqual(item["phase"], "claimed")
        self.assertEqual([e["text"] for e in self.ledger(crew["id"])], ["took #7"])

    async def test_a_work_write_naming_another_crew_is_refused_and_writes_nothing(self):
        mine = self.crew("Andromeda")
        theirs = self.crew("Whirlpool")
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": theirs["id"], "number": 7,
                  "phase": "claimed", "event": "stealing #7", "event_kind": "claim"},
            internal_auth=True, session=self.agent(mine),
        )
        self.assertEqual(res.status, 403)
        self.assertEqual(_payload(res)["code"], "crew_identity_mismatch")
        # Refused, not redirected: neither crew's ledger records the attempt.
        self.assertEqual(self.ledger(theirs["id"]), [])
        self.assertEqual(self.ledger(mine["id"]), [])

    async def test_a_work_write_from_a_non_crew_session_is_refused(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"number": 7, "phase": "claimed", "event": "took #7", "event_kind": "claim"},
            internal_auth=True, session="dashboard:chat-3-1730000000",
        )
        self.assertEqual(res.status, 403)
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_a_retired_crews_session_is_told_it_is_retired(self):
        # Identified, then refused for the real reason. "Not a crew" to a crew that
        # plainly is reads as a bug to whoever inspects the transcript later.
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self.call(
            "PUT", "/crew/work",
            body={"number": 7, "phase": "claimed", "event": "took #7", "event_kind": "claim"},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual(res.status, 409)
        self.assertEqual(_payload(res)["code"], "crew_retired")

    async def test_a_malformed_agent_body_is_still_400_not_403(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work", body=None, internal_auth=True, session=self.agent(crew)
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "invalid_json")


# ── a step that belongs to no issue ─────────────────────────────────────────


class TestACrewCanRecordAnEmptyQueue(_CrewRouteCase):
    """``PUT /crew/work`` with NO ``number`` — the empty-queue checkpoint.

    A crew that swept its queue and took nothing had no way to record the cycle:
    the route required a number because the number becomes a work-item filename,
    so the only way to report "checked, nothing to take" was to attribute it to an
    issue the crew never acted on. The ledger therefore either lied or stayed
    silent, and a resumed crew could not tell the two apart.

    The pairing is asserted in BOTH directions. Accepting an item kind with no
    number would only move the fabrication into the store, and accepting the
    crew-level kind WITH a number files a sweep under an issue it never touched —
    the same false attribution written the other way round.
    """

    def agent(self, crew: dict) -> str:
        return f"dashboard:{crew['slot_key']}"

    async def test_a_sweep_needs_no_number_and_writes_no_work_item(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"event": "checked 42 open issues, took none", "event_kind": "sweep"},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual(res.status, 200)
        body = _payload(res)
        # Same envelope as a numbered write, so the MCP tool reads one shape;
        # `coalesced` rides alongside it to say whether a line was written.
        self.assertTrue({"item", "event", "skip"}.issubset(set(body)))
        self.assertIsNone(body["item"])
        self.assertIsNone(body["skip"])
        self.assertFalse(body["coalesced"])
        self.assertEqual(
            [e["text"] for e in self.ledger(crew["id"])],
            ["checked 42 open issues, took none"],
        )
        # Absent, not zero — a `0` would read as a real issue everywhere.
        self.assertNotIn("number", self.ledger(crew["id"])[0])
        self.assertEqual(off_loop(crew_store.list_work_items, OWNER, REPO, crew["id"], self.root), [])

    async def test_an_idle_crew_does_not_bury_its_own_work_log(self):
        """Repeated idle cycles must not run the ledger away.

        Every ledger read is capped and drops the OLDEST line first, so one line
        per nudge would push the crew's real history out of the log this feature
        exists to make honest.
        """
        crew = self.crew("Andromeda")
        body = {"event": "queue empty", "event_kind": "sweep"}
        first = await self.call(
            "PUT", "/crew/work", body=body, internal_auth=True, session=self.agent(crew),
        )
        again = await self.call(
            "PUT", "/crew/work", body={**body, "event": "still empty"},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual((first.status, again.status), (200, 200))
        self.assertFalse(_payload(first)["coalesced"])
        self.assertTrue(_payload(again)["coalesced"])
        # One line, and it is the FIRST one -- its timestamp marks when the idle
        # stretch began, which is what a human opening a quiet crew wants to know.
        self.assertEqual([e["text"] for e in self.ledger(crew["id"])], ["queue empty"])

    async def test_an_item_kind_without_a_number_is_refused(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"event": "took something", "event_kind": "claim"},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "number_required")
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_a_sweep_carrying_a_number_is_refused(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"number": 7, "event": "queue empty", "event_kind": "sweep"},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "unexpected_number")
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_a_present_but_invalid_number_is_not_read_as_a_sweep(self):
        """A typo must stay a 400, not become "this step has no issue"."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"number": 0, "event": "took #0", "event_kind": "claim"},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "invalid_number")
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_work_item_fields_are_refused_rather_than_dropped(self):
        """Honouring the line while discarding the patch would report a phase
        move that was never stored anywhere."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"event": "queue empty", "event_kind": "sweep",
                  "phase": "claimed", "ci_state": {"state": "success"}},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "item_fields_without_number")
        self.assertIn("'ci_state'", _payload(res)["error"])
        self.assertIn("'phase'", _payload(res)["error"])
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_a_sweep_still_needs_a_reason(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work", body={"event_kind": "sweep"},
            internal_auth=True, session=self.agent(crew),
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "event_required")

    async def test_a_sweep_from_a_non_crew_session_is_refused(self):
        """The numberless path must not become a way around the identity gate."""
        self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"event": "queue empty", "event_kind": "sweep"},
            internal_auth=True, session="dashboard:chat-3-1730000000",
        )
        self.assertEqual(res.status, 403)
        self.assertEqual(_payload(res)["code"], "not_a_crew_session")


# ── which routes an agent may reach at all ──────────────────────────────────


class TestAgentReachableRoutes(_CrewRouteCase):
    """The ``full path, never prefix`` claim, enforced where it can be.

    The middleware allowlist in ``dashboard.server`` is matched
    ``path == p or path.startswith(p + "/")`` and carries no method, so its
    ``/crew`` entry admits the whole segment and cannot tell ``GET /crew`` from
    ``PUT``/``DELETE /crew``. Deny-by-default at the handler is therefore the only
    layer that can make the claim true.
    """

    async def test_only_the_two_ledger_routes_are_reachable_by_an_agent(self):
        # The whole table, so a route added later is refused to agents until someone
        # deliberately adds it to _AGENT_REACHABLE.
        for method, path in CREW_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                res = await self.call(
                    method, path, internal_auth=True, session="dashboard:crew-c_absent",
                    **_MINIMAL[(method, path)],
                )
                code = _payload(res).get("code")
                if (method, path) in crew_routes._AGENT_REACHABLE:
                    # Reached the handler. It still refuses this particular caller —
                    # the session names no crew — but not with the route gate.
                    self.assertNotEqual(code, "agent_route_denied")
                else:
                    self.assertEqual(res.status, 403)
                    self.assertEqual(code, "agent_route_denied")

    async def test_pause_and_the_crew_mutations_are_refused(self):
        # These are the three that matter: with the internal secret and the prefix
        # match, an agent could otherwise pause, rename or RETIRE a crew — its own
        # or any other.
        crew = self.crew("Andromeda")
        session = f"dashboard:{crew['slot_key']}"
        for method, path, body in (
            ("POST", "/crew/pause", {"id": crew["id"], "paused": True}),
            ("PUT", "/crew", {"id": crew["id"], "name": "Whirlpool"}),
            ("DELETE", "/crew", {"id": crew["id"]}),
        ):
            with self.subTest(route=f"{method} {path}"):
                res = await self.call(
                    method, path, body={"owner": OWNER, "repo": REPO, **body},
                    internal_auth=True, session=session,
                )
                self.assertEqual(res.status, 403)
                self.assertEqual(_payload(res)["code"], "agent_route_denied")
        # Refused before the handler ran: the record is untouched on all three axes.
        after = crew_store.read_crew(OWNER, REPO, crew["id"], self.root)
        self.assertEqual(after["name"], "Andromeda")
        self.assertIs(after["enabled"], True)
        self.assertIsNone(after["retired_at"])

    async def test_the_browser_still_reaches_every_route(self):
        # The gate keys on ``internal_auth`` alone, so a cookie-authenticated
        # dashboard request must be unaffected — including on the routes an agent
        # cannot touch.
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST", "/crew/pause", body={"owner": OWNER, "repo": REPO, "id": crew["id"],
                                         "paused": True},
        )
        self.assertEqual(res.status, 200)
        self.assertIs(_payload(res)["crew"]["enabled"], False)


if __name__ == "__main__":
    unittest.main()
