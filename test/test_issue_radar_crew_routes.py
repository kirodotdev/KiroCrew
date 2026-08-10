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
"""

import asyncio
import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import mcp_core
from kiro_crew.apps.builtins.issue_radar.backend import (
    crew_routes,
    crew_runtime,
    crew_store,
    provider,
    routes,
    store,
)

BASE = "/api/apps/issue-radar"
OWNER, REPO = "kirodotdev", "KiroCrew"  # brand-ok: the repository name

#: The contract, as a table. Also the inventory the registrar is checked against.
CREW_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/crews"),
    ("POST", "/crews"),
    ("GET", "/crews/names"),
    ("GET", "/crews/settings"),
    ("PUT", "/crews/settings"),
    ("GET", "/crew"),
    ("PUT", "/crew"),
    ("DELETE", "/crew"),
    ("PUT", "/crew/work"),
    ("POST", "/crew/pause"),
    ("POST", "/issue/comment"),
)

#: Endpoints this app used to expose to hold an issue for a human, and must not
#: expose again. Kept as an explicit table rather than deleted with their tests: the
#: registrar is checked against ``CREW_ROUTES`` by name, so a re-added handler under
#: a path nobody enumerates would pass every other test in this file silently.
RETIRED_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/crew/guidance"),
    ("GET", "/crews/escalations"),
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


def _registered() -> dict[tuple[str, str], object]:
    """Every crew route the registrar installs, keyed by (method, sub-path)."""
    app = web.Application()
    crew_routes.register_crew_routes(app)
    return {
        (route.method, str(route.resource.canonical)[len(BASE):]): route.handler
        for route in app.router.routes()
    }


def _request(
    method: str, path: str, *, query: dict | None = None, body: object = "",
    app: web.Application | None = None, headers: dict | None = None,
    internal_auth: bool = False,
) -> web.Request:
    """A real (mocked) aiohttp request for a handler under test.

    aiohttp's own ``make_mocked_request``, not a duck-typed stub: the handlers are
    annotated ``(web.Request) -> web.Response`` and a stand-in fails the mypy gate.
    ``body=None`` models a malformed payload — ``request.json()`` raising is exactly
    what the preamble's ``except -> 400`` branch is written for.

    ``internal_auth`` sets the key ``token_auth_middleware`` sets after it validates
    ``X-Internal-Secret``, which is how the routes tell an agent from the browser. A
    real request object matters here and not just for mypy: on a ``MagicMock``,
    ``request.get("internal_auth")`` is truthy for every request, so every test
    would take the agent branch and the browser branch would never be exercised.
    """
    full = f"{BASE}{path}"
    if query:
        full = full + "?" + "&".join(f"{k}={v}" for k, v in query.items())
    kwargs: dict = {"app": app} if app is not None else {}
    if headers:
        kwargs["headers"] = headers
    req = make_mocked_request(method, full, **kwargs)  # type: ignore[arg-type]
    if internal_auth:
        req["internal_auth"] = True
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    elif body != "":
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _payload(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


class _Slot:
    """A crew's chat slot, stubbed at the one method the route uses.

    ``enqueue_or_run_prompt`` returns True when it STARTED a turn and False when it
    queued — the route maps that onto ``queued``, so the stub honours the same
    contract instead of always returning True.

    ``_trust`` is the in-memory grant that makes an unattended crew's tool calls
    auto-approve. It is a field on the real slot object, which is why stopping a crew
    has to reach the slot and not only the record.
    """

    def __init__(self, running: bool = False, trust: bool = False) -> None:
        self.running = running
        self._trust = trust
        self.prompts: list[str] = []
        self.runners: list[object] = []

    def enqueue_or_run_prompt(self, prompt: str, run_chat, state) -> bool:
        self.prompts.append(prompt)
        self.runners.append(run_chat)
        return not self.running


class _Loop:
    """One armed autonudge loop — the fields the revocation reads."""

    def __init__(self, slot_key: str) -> None:
        self.id = "nl_0"
        self.slot_key = slot_key
        self.active = True


class _Nudge:
    """AutoNudgeService stubbed at the two calls a revocation makes."""

    def __init__(self, slot_key: str) -> None:
        self.loop = _Loop(slot_key)

    def get_by_slot(self, slot_key: str) -> _Loop | None:
        return self.loop if slot_key == self.loop.slot_key else None

    async def update(self, loop_id: str, **kw) -> None:
        if "active" in kw:
            self.loop.active = bool(kw["active"])


class _State:
    """The dashboard state, stubbed at the two members the route touches."""

    def __init__(self, slot: _Slot | None = None) -> None:
        self._slot = slot
        self.pushes = 0

    def get_slot(self, key: str) -> _Slot | None:
        return self._slot

    def push_slots_update(self) -> None:
        self.pushes += 1


class _CrewRouteCase(unittest.IsolatedAsyncioTestCase):
    """Base: a temp data root, an enabled app, and a connected repo."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # _scope is the single place that decides which data root a request's store
        # calls land in, so this one patch isolates every write in the module.
        for patcher in (
            mock.patch.object(routes, "_scope", return_value=self.root),
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch.object(store, "is_repo_connected", return_value=True),
            # Session -> crew resolution searches the connected repos, and the real
            # reader parses the developer's own config.json — which is empty in CI
            # and arbitrary locally. Pinned to the one repo these tests use so the
            # search is deterministic on both.
            mock.patch.object(
                store, "list_connected_repos",
                return_value=[
                    {"owner": OWNER, "repo": REPO, "provider": "github", "host": "github.com"}
                ],
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(
        self, method: str, path: str, *, query: dict | None = None, body: object = "",
        app: web.Application | None = None, session: str | None = None,
        internal_auth: bool = False,
    ) -> web.Response:
        handler = _registered()[(method, path)]
        headers = {"X-Session-Key": session} if session is not None else None
        return await handler(  # type: ignore[operator]
            _request(
                method, path, query=query, body=body, app=app, headers=headers,
                internal_auth=internal_auth,
            )
        )

    # ── fixtures written straight through the store ──────────────────────────

    def crew(self, name: str = "Andromeda", **spec) -> dict:
        return crew_store.create_crew(OWNER, REPO, {"name": name, **spec}, self.root)

    def work(self, crew_id: str, number: int, phase: str) -> dict:
        return crew_store.upsert_work_item(
            OWNER, REPO, crew_id, number, {"phase": phase}, self.root
        )

    def ledger(self, crew_id: str = "") -> list[dict]:
        return crew_store.read_events(OWNER, REPO, self.root, crew_id=crew_id)


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
        self.work(crew["id"], 2, "implementing")
        counts = _payload(
            await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO})
        )["counts"]
        self.assertEqual(counts["working"], 1)

    async def test_a_crew_parked_on_its_newest_item_is_not_working(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 1, "implementing")
        self.work(crew["id"], 2, "awaiting-ci")
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        self.assertEqual(page["counts"]["working"], 0)
        self.assertEqual(page["crews"][0]["status"], "idle")

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
        crew_store.append_event(OWNER, REPO, crew["id"], 7, "claim", "took #7", self.root)
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": crew["id"]}
        )
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["crew"]["name"], "Andromeda")
        self.assertEqual(sorted(it["number"] for it in page["items"]), [7, 8])
        self.assertEqual([e["text"] for e in page["events"]], ["took #7"])
        # A pass is terminal, so it frees the slot. `open` is the only count: there
        # is no second bucket for work held on a human, because none is held.
        self.assertEqual(page["counts"], {"open": 1})

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
        item = crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root)
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


# ── PUT /crew/work ──────────────────────────────────────────────────────────


class TestWorkItems(_CrewRouteCase):
    async def test_it_upserts_the_item_and_logs_one_event_in_one_call(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={
                "owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                "phase": "implementing", "next": "write the failing test",
                "event": "reproduced the crash", "event_kind": "implement",
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
                "PUT", "/crew/work",
                body={
                    "owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                    "phase": "claimed", "event": "took it", "event_kind": "claim",
                },
            )
        )["item"]
        self.assertNotIn("event", item)
        self.assertNotIn("event_kind", item)

    async def test_a_write_without_an_event_is_refused(self):
        # The reason this route exists: a phase must not change without a logged
        # reason, so the log line is not optional.
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "implementing", "event_kind": "implement"},
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "event_required")
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root))

    async def test_an_unknown_event_kind_is_400_and_writes_nothing(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "implementing", "event": "x", "event_kind": "vibes"},
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "invalid_event_kind")
        # Validated BEFORE the upsert, so the item was never written.
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root))

    async def test_a_bad_number_is_400(self):
        crew = self.crew("Andromeda")
        for number in (0, -1, True, "7"):
            with self.subTest(number=number):
                res = await self.call(
                    "PUT", "/crew/work",
                    body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"],
                          "number": number, "event": "x", "event_kind": "claim"},
                )
                self.assertEqual(res.status, 400)

    async def test_a_second_editing_item_is_409(self):
        crew = self.crew("Andromeda")
        await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "implementing", "event": "started #7", "event_kind": "implement"},
        )
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 8,
                  "phase": "implementing", "event": "started #8", "event_kind": "implement"},
        )
        self.assertEqual(res.status, 409)
        self.assertIn("already editing #7", _payload(res)["error"])

    async def test_a_refused_write_leaves_no_ledger_line(self):
        # Ordering proof. The event is appended only AFTER the item write returns,
        # so the 409 above cannot leave the append-only log asserting a phase
        # change that never happened.
        crew = self.crew("Andromeda")
        await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "implementing", "event": "started #7", "event_kind": "implement"},
        )
        await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 8,
                  "phase": "implementing", "event": "started #8", "event_kind": "implement"},
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
                "PUT", "/crew/work",
                body={"owner": OWNER, "repo": REPO, "crew_id": bad, "number": 7,
                      "event": "x", "event_kind": "claim"},
            )
            self.assertEqual(res.status, 400, f"{bad!r} should be a bad request")

    async def test_an_unknown_crew_is_404(self):
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": "c_deadbeef", "number": 7,
                  "event": "x", "event_kind": "claim"},
        )
        self.assertEqual(res.status, 404)

    async def test_a_retired_crew_cannot_take_new_work(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "claimed", "event": "took it", "event_kind": "claim"},
        )
        self.assertEqual(res.status, 409)
        self.assertEqual(_payload(res)["code"], "crew_retired")


# ── the shared skip index ───────────────────────────────────────────────────


class TestSharedSkipIndex(_CrewRouteCase):
    """The index's whole value is that it crosses crews, so every assertion here
    writes as one crew and reads as another."""

    async def _skip(self, crew_id: str, number: int, **extra) -> web.Response:
        return await self.call(
            "PUT", "/crew/work",
            body={
                "owner": OWNER, "repo": REPO, "crew_id": crew_id, "number": number,
                "phase": "skipped", "event": f"passing on #{number}", "event_kind": "skip",
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
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, reader["id"], 42, self.root))

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
        self.assertEqual(crew_store.open_slot_count(OWNER, REPO, author["id"], self.root), 0)

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
        self.assertEqual(
            page["recent_skips"][0]["reason"], "the fix needs a schema migration"
        )

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
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 42,
                  "phase": "resolved", "event": "merged", "event_kind": "merge"},
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


# ── one work write is all-or-nothing ────────────────────────────────────────


class TestWorkWriteRollback(_CrewRouteCase):
    """``PUT /crew/work`` commits three files or none of them.

    The route writes the work item, then the repo-wide skip index, then the ledger
    line — three files, so no ordering makes it atomic. Every test here fails the
    LAST write and asserts the earlier ones were rolled back, because the
    half-written states are each worse than the failure: an item whose phase moved
    with no event explaining it, or — the expensive one — an issue sitting in the
    shared index that every other crew filters against, recorded as passed with
    nothing in the log saying who passed on it or why.

    The rollback is only correct because ONE lock — the crew's — is held from before
    the snapshot to after the restore. The tests that pin that are the ones about a
    competing write: a rollback target another writer can move is not a rollback, and
    no comparison detects it, because in the interleaving that loses an update the
    file DOES still hold what the failing transaction wrote.

    ``append_event`` is failed with ``OSError`` rather than ``CrewStoreError``: the
    kind is validated before any write, so the reachable failure is I/O, and an
    ``OSError`` also proves the compensation is not accidentally riding on the
    ``CrewStoreError -> 409`` decorator.
    """

    def _body(self, crew_id: str, number: int, **extra) -> dict:
        return {
            "owner": OWNER, "repo": REPO, "crew_id": crew_id, "number": number,
            "event": f"progress on #{number}", "event_kind": "claim", **extra,
        }

    async def _page(self, crew_id: str) -> dict:
        return _payload(
            await self.call("GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": crew_id})
        )

    async def test_a_failed_event_append_leaves_no_newly_created_item(self):
        crew = self.crew("Andromeda")
        path = crew_store.work_item_path(OWNER, REPO, crew["id"], 7, self.root)
        with mock.patch.object(
            crew_store, "append_event", side_effect=OSError("no space left on device")
        ):
            with self.assertRaises(OSError):
                await self.call(
                    "PUT", "/crew/work",
                    body=self._body(crew["id"], 7, phase="claimed"),
                )
        # Created by the failed request, so there is no earlier value to return to:
        # the file must be GONE, not left holding a phase nothing logged. A stub
        # would also occupy one of the crew's open slots for an issue it never took.
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root))
        self.assertFalse(path.exists())
        self.assertEqual(self.ledger(crew["id"]), [])
        self.assertEqual((await self._page(crew["id"]))["items"], [])

    async def test_a_rollback_restores_the_value_a_committed_write_left(self):
        """The rollback target must not be able to go stale before it is used.

        THE INTERLEAVING: another request writes #7 and fully succeeds; this request
        then writes it, and its ledger append fails. If this request's snapshot was
        taken before that write, restoring it erases an update that committed — and
        no comparison can catch it, because the file DOES still hold exactly what
        this request wrote: this request wrote last. Only holding one lock from
        before the snapshot to after the rollback closes it, which is what makes the
        snapshot below the other request's value rather than the seeded one.

        Deterministic, not raced: the competing write is committed from the seam
        where this transaction resolves its lock path, so it lands strictly before
        the lock is taken — which in the broken shape is strictly AFTER the snapshot.
        """
        crew = self.crew("Andromeda")
        await self.call(
            "PUT", "/crew/work",
            body=self._body(
                crew["id"], 7, phase="claimed", next="read the traceback",
                event="took it",
            ),
        )
        with self._a_competing_write_commits_before_the_lock(crew["id"], 7):
            with self.assertRaises(OSError):
                await self.call(
                    "PUT", "/crew/work",
                    body=self._body(
                        crew["id"], 7, phase="implementing", event="starting",
                        event_kind="implement",
                    ),
                )

        item = crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root)
        assert item is not None
        self.assertEqual(item["next"], "written by the other request")
        # And this request's own phase change is gone: the restore happened, it just
        # restored the value that was actually there.
        self.assertEqual(item["phase"], "claimed")
        self.assertEqual([e["text"] for e in self.ledger(crew["id"])], ["took it"])

    async def test_a_rollback_does_not_delete_an_item_committed_before_it_locked(self):
        """The same staleness on a NEW item, where the damage is worse.

        A snapshot of ``None`` means "there was no item", so the compensation is a
        DELETE. Snapshot it before another request CREATES the item and the rollback
        removes a work item that committed — and with it that crew's claim on the
        issue, which is the state this whole route exists to keep honest.
        """
        crew = self.crew("Whirlpool")
        with self._a_competing_write_commits_before_the_lock(crew["id"], 8):
            with self.assertRaises(OSError):
                await self.call(
                    "PUT", "/crew/work",
                    body=self._body(
                        crew["id"], 8, phase="investigating", event="starting",
                        event_kind="investigate",
                    ),
                )

        item = crew_store.read_work_item(OWNER, REPO, crew["id"], 8, self.root)
        self.assertIsNotNone(item, "the rollback deleted an item another request created")
        assert item is not None
        self.assertEqual(item["next"], "written by the other request")

    @contextlib.contextmanager
    def _a_competing_write_commits_before_the_lock(self, crew_id: str, number: int):
        """One competing work write, committed at the last moment before this
        transaction can take the crew lock, with this request's ledger append failing.

        The seam is ``_crew_lock_path``: whoever is about to lock has to resolve the
        path first, and no lock is held at that point, so the competing write can
        take it, commit and release. Placing it there is what makes the test
        independent of where the snapshot is taken — it lands before the lock either
        way, so a transaction that snapshots UNDER the lock sees it and one that
        snapshots outside does not.
        """
        real_lock_path = crew_store._crew_lock_path
        done: list[bool] = []

        def _commit_the_other_request(owner, repo, cid, root=None):
            if not done:
                done.append(True)   # before the recursive call below re-enters here
                crew_store.upsert_work_item(
                    owner, repo, cid, number,
                    {"next": "written by the other request"}, root,
                )
            return real_lock_path(owner, repo, cid, root)

        with mock.patch.object(
            crew_store, "_crew_lock_path", side_effect=_commit_the_other_request
        ), mock.patch.object(
            crew_store, "append_event", side_effect=OSError("no space left on device")
        ):
            yield
        self.assertTrue(done, "the competing write never ran — the seam moved")

    async def test_a_competing_write_waits_for_the_whole_transaction(self):
        """Nothing may land between this transaction's write and its rollback.

        The competing write is started while the transaction is at its LAST step, and
        is asserted not to have landed — that is the exclusion the single lock buys,
        and it is what makes the snapshot still valid when the rollback uses it. Once
        the transaction unwinds, the competing write goes through: serialised after,
        never lost.

        Bounded: every wait here has a timeout, so a lock this test cannot get is a
        failure rather than a hang.
        """
        crew = self.crew("Andromeda")
        await self.call(
            "PUT", "/crew/work",
            body=self._body(crew["id"], 7, phase="claimed", event="took it"),
        )
        blocked = threading.Thread(
            target=crew_store.upsert_work_item,
            args=(OWNER, REPO, crew["id"], 7, {"next": "the later write"}, self.root),
            daemon=True,
        )
        held_while_running: list[str | None] = []

        def _start_a_competing_write_then_fail(*a, **k):
            blocked.start()
            # Long enough that an unlocked writer would certainly have landed, short
            # enough to keep the suite fast. This asserts a NEGATIVE, so the wait is
            # the whole test: with the lock held the value can never appear.
            blocked.join(timeout=0.5)
            item = crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root)
            held_while_running.append((item or {}).get("next"))
            raise OSError("no space left on device")

        with mock.patch.object(
            crew_store, "append_event", side_effect=_start_a_competing_write_then_fail
        ):
            with self.assertRaises(OSError):
                await asyncio.wait_for(
                    self.call(
                        "PUT", "/crew/work",
                        body=self._body(
                            crew["id"], 7, phase="implementing", event="starting",
                            event_kind="implement",
                        ),
                    ),
                    timeout=30,
                )

        blocked.join(timeout=10)
        self.assertFalse(blocked.is_alive(), "the competing write never got the lock")
        self.assertEqual(
            held_while_running, [""],
            "a competing write landed while the transaction held the crew lock",
        )
        # It was not lost either — it applied to the value the rollback restored.
        item = crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root)
        assert item is not None
        self.assertEqual(item["next"], "the later write")
        self.assertEqual(item["phase"], "claimed")

    async def test_a_work_write_does_not_deadlock_on_another_crews_skip_write(self):
        """The lock held across the transaction must be the CREW's, not the shared one.

        A crew's progress write holds its crew lock for all three writes and takes the
        repo-wide index lock from inside it, for one read-modify-write. So while one
        crew is mid-transaction, another crew's own pass — its crew lock, then the
        shared index — must still go through.

        Driven from OUTSIDE the request: this parks the request at its last write and
        runs the other crew's pass from the test's own thread, so the assertion does
        not depend on the parked request making progress. Every wait is bounded, so a
        lock that cannot be got FAILS this test instead of hanging it. Holding the
        repo-wide lock across the whole transaction instead — one lock, but the wrong
        one — would park every other crew in the repo behind it, and the join below
        would time out.
        """
        author = self.crew("Andromeda")
        other = self.crew("Whirlpool")
        real_append = crew_store.append_event
        inside = threading.Event()
        release = threading.Event()
        finished: list[bool] = []

        def _park_at_the_last_write(*a, **k):
            inside.set()
            # Bounded so a failure of this test cannot leave the worker wedged:
            # the request finishes either way, the assertions just come first.
            release.wait(timeout=15)
            return real_append(*a, **k)

        def _the_other_crews_pass() -> None:
            # Its own crew lock, then the shared index — the same order the parked
            # request is in the middle of, from a different crew.
            crew_store.upsert_work_item(
                OWNER, REPO, other["id"], 99, {"phase": "skipped"}, self.root
            )
            crew_store.record_skip(
                OWNER, REPO, 99, "already fixed upstream", "already-fixed",
                other["id"], self.root,
            )
            finished.append(True)

        with mock.patch.object(crew_store, "append_event", side_effect=_park_at_the_last_write):
            pending = asyncio.ensure_future(
                self.call(
                    "PUT", "/crew/work",
                    body=self._body(
                        author["id"], 42, phase="skipped", why="needs an owner decision",
                        skip_scope="needs-design", event="passing on #42",
                        event_kind="skip",
                    ),
                )
            )
            try:
                self.assertTrue(
                    await asyncio.to_thread(inside.wait, 15),
                    "the request never reached its last write — it is blocked earlier",
                )
                thread = threading.Thread(target=_the_other_crews_pass, daemon=True)
                thread.start()
                await asyncio.to_thread(thread.join, 5)
                self.assertTrue(
                    finished,
                    "another crew's pass could not complete while this request was "
                    "mid-transaction — the shared lock is held too widely",
                )
            finally:
                release.set()
            res = await asyncio.wait_for(pending, timeout=30)

        self.assertEqual(res.status, 200)
        # Both passes are indexed: neither crew's write was dropped or rolled back.
        self.assertEqual(
            sorted(crew_store.read_skips(OWNER, REPO, self.root)), ["42", "99"]
        )
        item = crew_store.read_work_item(OWNER, REPO, other["id"], 99, self.root)
        assert item is not None
        self.assertEqual(item["phase"], "skipped")

    async def test_a_rollback_never_removes_a_skip_another_request_committed_first(self):
        """Two IDENTICAL passes: only the one that indexed it may un-index it.

        Both requests see #42 unindexed before they write, and the entry that stands
        afterwards names the same crew, reason and scope for either of them — so
        neither a pre-read of the index nor a field match can tell them apart, and
        the loser of that race would remove the winner's committed decision on its
        own rollback. ``record_skip`` reports creation from inside the repo-wide lock
        instead, which is the only place it is knowable.

        Deterministic: the competing pass is committed at the front of this request's
        own ``record_skip`` call, so this request's write is strictly the second one.
        """
        author = self.crew("Andromeda")
        reader = self.crew("Whirlpool")
        real_record_skip = crew_store.record_skip

        def _land_the_other_pass_first(*a, **k):
            real_record_skip(*a, **k)      # the overlapping request commits
            return real_record_skip(*a, **k)  # this request's own call, now a re-skip

        with mock.patch.object(
            crew_store, "record_skip", side_effect=_land_the_other_pass_first
        ), mock.patch.object(
            crew_store, "append_event", side_effect=OSError("no space left on device")
        ):
            with self.assertRaises(OSError):
                await self.call(
                    "PUT", "/crew/work",
                    body=self._body(
                        author["id"], 42, phase="skipped", why="needs an owner decision",
                        skip_scope="needs-design", event="passing on #42",
                        event_kind="skip",
                    ),
                )

        # The committed pass is still indexed, and still visible to the whole fleet —
        # un-indexing it would put #42 back in front of every crew with nothing in
        # the log saying it had been decided.
        standing = crew_store.read_skips(OWNER, REPO, self.root)
        self.assertEqual(list(standing), ["42"])
        self.assertEqual(standing["42"]["reason"], "needs an owner decision")
        self.assertEqual((await self._page(reader["id"]))["skipped_numbers"], [42])
        # The item this request created is still rolled back: only the skip was
        # somebody else's to keep.
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, author["id"], 42, self.root))

    async def test_a_failed_event_append_leaves_an_existing_item_byte_identical(self):
        crew = self.crew("Andromeda")
        await self.call(
            "PUT", "/crew/work",
            body=self._body(
                crew["id"], 7, phase="claimed", next="read the traceback",
                why="looks reproducible", event="took it",
            ),
        )
        path = crew_store.work_item_path(OWNER, REPO, crew["id"], 7, self.root)
        before = path.read_bytes()

        with mock.patch.object(
            crew_store, "append_event", side_effect=OSError("no space left on device")
        ):
            with self.assertRaises(OSError):
                await self.call(
                    "PUT", "/crew/work",
                    body=self._body(
                        crew["id"], 7, phase="implementing", next="write the failing test",
                        why="fix is one line", tried_approach="reverting the guard",
                        event="starting on it", event_kind="implement",
                    ),
                )

        # The WHOLE record, not the phase alone: the refused patch also moved
        # `next`, `why`, `tried` and `last_progress_at`, and `last_progress_at` is
        # what the claim TTL is measured from — a rollback that restored the phase
        # while leaving the clock renewed would hold the claim on evidence of
        # progress the crew never made.
        self.assertEqual(
            crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root),
            json.loads(before),
        )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([e["text"] for e in self.ledger(crew["id"])], ["took it"])

    async def test_a_failed_event_append_leaves_the_shared_skip_index_unchanged(self):
        author = self.crew("Andromeda")
        reader = self.crew("Whirlpool")
        with mock.patch.object(
            crew_store, "append_event", side_effect=OSError("no space left on device")
        ):
            with self.assertRaises(OSError):
                await self.call(
                    "PUT", "/crew/work",
                    body=self._body(
                        author["id"], 42, phase="skipped", why="needs an owner decision",
                        skip_scope="needs-design", event="passing on #42",
                        event_kind="skip",
                    ),
                )
        self.assertEqual(crew_store.read_skips(OWNER, REPO, self.root), {})
        # Asserted from the OTHER crew's page, because visible-to-the-fleet is the
        # property: an entry left here is one every other crew filters #42 out on.
        page = await self._page(reader["id"])
        self.assertEqual(page["skipped_numbers"], [])
        self.assertEqual(page["recent_skips"], [])
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, author["id"], 42, self.root))

    async def test_a_rollback_never_removes_a_skip_this_request_did_not_add(self):
        """The compensation must not undo a decision it merely found.

        ``record_skip`` keeps the FIRST crew's reason, so a re-skip writes nothing
        and has nothing to undo. Removing by key would erase the standing decision —
        the earlier crew's audit trail — and put the issue back in front of every
        crew in the fleet. Both owners are covered: another crew's entry, and this
        crew's own from an earlier request, which a ``crew_id``-only check would get
        wrong.
        """
        first = self.crew("Andromeda")
        second = self.crew("Whirlpool")
        for author, retrier in ((first, second), (first, first)):
            with self.subTest(same_crew=author["id"] == retrier["id"]):
                await self.call(
                    "PUT", "/crew/work",
                    body=self._body(
                        author["id"], 42, phase="skipped", why="first reason",
                        skip_scope="architecture", event="passing on #42",
                        event_kind="skip",
                    ),
                )
                standing = crew_store.read_skips(OWNER, REPO, self.root)
                self.assertEqual(list(standing), ["42"])

                with mock.patch.object(
                    crew_store, "append_event", side_effect=OSError("no space left")
                ):
                    with self.assertRaises(OSError):
                        await self.call(
                            "PUT", "/crew/work",
                            body=self._body(
                                retrier["id"], 42, phase="skipped", why="second reason",
                                skip_scope="duplicate", event="also passing",
                                event_kind="skip",
                            ),
                        )
                self.assertEqual(crew_store.read_skips(OWNER, REPO, self.root), standing)

    async def test_the_happy_path_still_returns_the_item_the_event_and_the_skip(self):
        # The regression guard on the compensation: it must run on the failure path
        # ONLY. Nothing about the rollback can reach a request that succeeded, so
        # all three writes still stand and all three are still answered.
        crew = self.crew("Andromeda")
        reader = self.crew("Whirlpool")
        res = await self.call(
            "PUT", "/crew/work",
            body=self._body(
                crew["id"], 42, phase="skipped", why="needs an owner decision",
                skip_scope="needs-design", event="passing on #42", event_kind="skip",
            ),
        )
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["item"]["phase"], "skipped")
        self.assertEqual(page["event"]["kind"], "skip")
        self.assertEqual(page["event"]["text"], "passing on #42")
        self.assertEqual(page["skip"]["reason"], "needs an owner decision")
        self.assertEqual(page["skip"]["scope"], "needs-design")
        # Durable, not just echoed — all three files.
        self.assertEqual(
            crew_store.read_work_item(OWNER, REPO, crew["id"], 42, self.root)["phase"],
            "skipped",
        )
        self.assertEqual([e["text"] for e in self.ledger(crew["id"])], ["passing on #42"])
        self.assertEqual((await self._page(reader["id"]))["skipped_numbers"], [42])


# ── POST /crew/pause ────────────────────────────────────────────────────────


class TestPause(_CrewRouteCase):
    async def test_pausing_stores_the_reason(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST", "/crew/pause",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": True,
                  "reason": "waiting on the release"},
        )
        self.assertEqual(res.status, 200)
        paused = _payload(res)["crew"]
        self.assertIs(paused["enabled"], False)
        self.assertEqual(paused["paused_reason"], "waiting on the release")

    async def test_resuming_clears_the_reason(self):
        crew = self.crew("Andromeda")
        await self.call(
            "POST", "/crew/pause",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": True, "reason": "hold"},
        )
        resumed = _payload(
            await self.call(
                "POST", "/crew/pause",
                body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": False},
            )
        )["crew"]
        self.assertIs(resumed["enabled"], True)
        # A stale reason on a running crew would explain why it is stopped.
        self.assertEqual(resumed["paused_reason"], "")

    async def test_paused_must_be_a_boolean(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST", "/crew/pause",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": "yes"},
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "invalid_paused")


# ── stopping a crew revokes its execution BEFORE the route answers ──────────


class TestStopRevokesExecution(_CrewRouteCase):
    """Pause and retire must un-arm the crew by the time they respond.

    Writing the record is not stopping the crew. What gives a crew a turn is its
    autonudge loop — a live timer in another service — and what makes that turn
    auto-approve is ``slot._trust``, an in-memory field on the slot. Neither is
    reachable through ``enabled``/``paused_reason``/``retired_at``. The watchdog does
    revoke both, but it runs on the app's poll interval, so a route that returned
    straight after the store write left that whole interval open for a timer to fire
    one more unattended, auto-approved turn on a crew a human had just stopped.

    No watchdog cycle runs in any of these tests, which is what makes them read the
    ROUTE's own behaviour: every assertion below is the state of the two grants at
    the moment the response was produced.
    """

    def _armed(self, crew: dict) -> tuple[_Slot, web.Application, _Nudge]:
        """A crew that is genuinely running: trusted slot, active loop."""
        slot = _Slot(trust=True)
        app = web.Application()
        app["state"] = _State(slot)
        return slot, app, _Nudge(crew["slot_key"])

    async def _stop(self, app: web.Application, svc: _Nudge, **body) -> web.Response:
        with mock.patch.object(crew_runtime, "_autonudge_instance", lambda: svc):
            method, path = ("POST", "/crew/pause") if "paused" in body else ("DELETE", "/crew")
            return await self.call(method, path, body=body, app=app)

    async def test_pausing_clears_trust_and_deactivates_the_loop(self):
        crew = self.crew("Andromeda")
        slot, app, svc = self._armed(crew)
        res = await self._stop(
            app, svc, owner=OWNER, repo=REPO, id=crew["id"], paused=True, reason="hold"
        )
        self.assertEqual(res.status, 200)
        self.assertFalse(slot._trust)
        self.assertFalse(svc.loop.active)

    async def test_retiring_clears_trust_and_deactivates_the_loop(self):
        crew = self.crew("Andromeda")
        slot, app, svc = self._armed(crew)
        res = await self._stop(app, svc, owner=OWNER, repo=REPO, id=crew["id"])
        self.assertEqual(res.status, 200)
        self.assertTrue(_payload(res)["crew"]["retired_at"])
        self.assertFalse(slot._trust)
        self.assertFalse(svc.loop.active)

    async def test_resuming_leaves_the_crew_armed(self):
        """The inverse must not fire: resuming is not stopping, and the watchdog is
        what brings a live crew back — re-arming here would give one slot two loops."""
        crew = self.crew("Andromeda")
        slot, app, svc = self._armed(crew)
        res = await self._stop(app, svc, owner=OWNER, repo=REPO, id=crew["id"], paused=False)
        self.assertEqual(res.status, 200)
        self.assertTrue(slot._trust)
        self.assertTrue(svc.loop.active)

    async def test_a_revocation_failure_still_answers_200(self):
        """The record is already written and correct, so an unreachable in-memory
        grant is not a failed pause — the watchdog re-revokes on its next cycle."""
        crew = self.crew("Andromeda")
        _slot, app, svc = self._armed(crew)
        with mock.patch.object(
            crew_runtime, "revoke_crew_execution",
            new=AsyncMock(side_effect=RuntimeError("registry busy")),
        ):
            res = await self._stop(
                app, svc, owner=OWNER, repo=REPO, id=crew["id"], paused=True, reason="hold"
            )
        self.assertEqual(res.status, 200)
        after = crew_store.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertIs(after["enabled"], False)

    async def test_a_stop_without_a_dashboard_is_not_a_500(self):
        """The gateway can serve this route with no dashboard state attached."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST", "/crew/pause",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": True},
        )
        self.assertEqual(res.status, 200)


class TestUpdateRevokesWhenAutoApprovalIsWithdrawn(_CrewRouteCase):
    """``PUT /crew`` is the OTHER way a crew stops being auto-approved.

    Pause and retire are not the only routes that can withdraw the grant: the patch
    route persists ``unattended`` (and ``enabled``/``paused_reason``), and persisting
    the flag reaches neither the ``SafetyOverride`` scope nor the loop. Left to the
    watchdog, an already-armed nudge that fires inside the poll interval takes a
    whole auto-approved, unattended turn under a setting the human had ALREADY
    switched off — which is the exact failure the switch exists to prevent, and the
    one place it is least visible, because the record on the page reads correctly
    the whole time.

    The transition is read off the crew RECORD either side of the write, not off the
    patch, so the four cases below are asserted as behaviour rather than as a body
    shape. No watchdog cycle runs in any of them: every assertion is the state of the
    two grants at the moment the response was produced.
    """

    def _armed(self, crew: dict) -> tuple[_Slot, web.Application, _Nudge]:
        """A crew that is genuinely running: trusted slot, active loop."""
        slot = _Slot(trust=True)
        app = web.Application()
        app["state"] = _State(slot)
        return slot, app, _Nudge(crew["slot_key"])

    async def _patch(self, app: web.Application, svc: _Nudge, **body) -> web.Response:
        with mock.patch.object(crew_runtime, "_autonudge_instance", lambda: svc):
            return await self.call("PUT", "/crew", body=body, app=app)

    async def test_turning_unattended_off_revokes_before_the_route_answers(self):
        crew = self.crew("Andromeda")  # unattended defaults to True
        slot, app, svc = self._armed(crew)
        res = await self._patch(
            app, svc, owner=OWNER, repo=REPO, id=crew["id"], unattended=False
        )
        self.assertEqual(res.status, 200)
        self.assertIs(_payload(res)["crew"]["unattended"], False)
        # Both grants gone by the time the response object exists — not on the
        # watchdog's next cycle.
        self.assertFalse(slot._trust)
        self.assertFalse(svc.loop.active)

    async def test_turning_unattended_on_does_not_revoke(self):
        """The inverse must not fire. Arming is the watchdog's job — revoking here
        would disarm the crew the same request just switched on."""
        crew = self.crew("Andromeda", unattended=False)
        slot, app, svc = self._armed(crew)
        res = await self._patch(
            app, svc, owner=OWNER, repo=REPO, id=crew["id"], unattended=True
        )
        self.assertEqual(res.status, 200)
        self.assertIs(_payload(res)["crew"]["unattended"], True)
        self.assertTrue(slot._trust)
        self.assertTrue(svc.loop.active)

    async def test_a_patch_without_unattended_leaves_an_armed_crew_armed(self):
        """A rename is not a stop. Revoking on every patch would make editing a live
        crew's model or name cost it its next turn."""
        crew = self.crew("Andromeda")
        slot, app, svc = self._armed(crew)
        res = await self._patch(
            app, svc, owner=OWNER, repo=REPO, id=crew["id"], name="Whirlpool"
        )
        self.assertEqual(res.status, 200)
        self.assertEqual(_payload(res)["crew"]["name"], "Whirlpool")
        self.assertTrue(slot._trust)
        self.assertTrue(svc.loop.active)

    async def test_repeating_unattended_true_is_not_a_transition(self):
        """``unattended: true`` on an already-unattended crew changes nothing, so it
        must not revoke — the editor submits the whole form, so the field arrives on
        every save whether the user touched it or not."""
        crew = self.crew("Andromeda")
        slot, app, svc = self._armed(crew)
        res = await self._patch(
            app, svc, owner=OWNER, repo=REPO, id=crew["id"], unattended=True
        )
        self.assertEqual(res.status, 200)
        self.assertTrue(slot._trust)
        self.assertTrue(svc.loop.active)

    async def test_a_patch_to_an_attended_crew_does_not_take_a_humans_trust(self):
        """``unattended: false`` on a crew that was ALREADY attended is not a
        withdrawal. It matters because ``revoke_crew_execution`` also clears
        ``slot._trust`` — the interactive grant a human clicked — so revoking without
        a transition would silently undo that click on an unrelated save."""
        crew = self.crew("Andromeda", unattended=False)
        slot, app, svc = self._armed(crew)
        res = await self._patch(
            app, svc, owner=OWNER, repo=REPO, id=crew["id"], unattended=False
        )
        self.assertEqual(res.status, 200)
        self.assertTrue(slot._trust)
        self.assertTrue(svc.loop.active)

    async def test_disabling_through_the_patch_route_revokes_too(self):
        """``enabled`` is patchable here as well, and an unattended crew that stops
        being live stops earning the grant for exactly the same reason. Asserted
        because the guard is written against the whole grant condition, not against
        ``unattended`` alone — a check on one field would leave this door open."""
        crew = self.crew("Andromeda")
        slot, app, svc = self._armed(crew)
        res = await self._patch(
            app, svc, owner=OWNER, repo=REPO, id=crew["id"], enabled=False
        )
        self.assertEqual(res.status, 200)
        self.assertIs(_payload(res)["crew"]["unattended"], True)
        self.assertFalse(slot._trust)
        self.assertFalse(svc.loop.active)

    async def test_a_withdrawal_without_a_dashboard_is_not_a_500(self):
        """The gateway can serve this route with no dashboard state attached, and the
        record is the durable truth — an unreachable grant is not a failed save."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "unattended": False},
        )
        self.assertEqual(res.status, 200)
        after = crew_store.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertIs(after["unattended"], False)


# ── /crews/settings ─────────────────────────────────────────────────────────


class TestSettings(_CrewRouteCase):
    async def test_get_returns_defaults_for_a_never_configured_repo(self):
        res = await self.call("GET", "/crews/settings", query={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 200)
        settings = _payload(res)["settings"]
        self.assertEqual(settings["claim_ttl_hours"], 48)
        self.assertEqual(
            settings["needs_human_label"],
            crew_store.DEFAULT_SETTINGS["needs_human_label"],
        )
        self.assertNotIn("escalation_handback_days", settings)

    async def test_put_merges_and_leaves_untouched_fields_alone(self):
        first = _payload(
            await self.call(
                "PUT", "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"claim_ttl_hours": 12}},
            )
        )["settings"]
        self.assertEqual(first["claim_ttl_hours"], 12)

        second = _payload(
            await self.call(
                "PUT", "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"commit_trailer": "Crew: {name}"}},
            )
        )["settings"]
        # A partial patch MERGES — which is why this route needs no revision
        # precondition: it cannot discard a field it did not send.
        self.assertEqual(second["claim_ttl_hours"], 12)
        self.assertEqual(second["commit_trailer"], "Crew: {name}")
        self.assertEqual(
            second["needs_human_label"],
            crew_store.DEFAULT_SETTINGS["needs_human_label"],
        )
        self.assertEqual(
            _payload(
                await self.call("GET", "/crews/settings", query={"owner": OWNER, "repo": REPO})
            )["settings"],
            second,
        )

    async def test_the_needs_human_label_is_writable_through_the_route(self):
        """This is the surface a Settings UI writes, and the value reaches a forge
        label write, so a blank one must not be storable through it either."""
        stored = _payload(
            await self.call(
                "PUT", "/crews/settings",
                body={
                    "owner": OWNER, "repo": REPO,
                    "settings": {"needs_human_label": " needs: maintainer "},
                },
            )
        )["settings"]
        self.assertEqual(stored["needs_human_label"], "needs: maintainer")

        blanked = _payload(
            await self.call(
                "PUT", "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"needs_human_label": "  "}},
            )
        )["settings"]
        self.assertEqual(blanked["needs_human_label"], "needs: maintainer")

    async def test_put_requires_a_settings_object(self):
        for value in (None, "12", 12, ["a"]):
            with self.subTest(settings=value):
                res = await self.call(
                    "PUT", "/crews/settings",
                    body={"owner": OWNER, "repo": REPO, "settings": value},
                )
                self.assertEqual(res.status, 400)
                self.assertEqual(_payload(res)["code"], "invalid_settings")


# ── the endpoints that used to hold work for a human ──────────────────────


class TestNothingWaitsForAHuman(_CrewRouteCase):
    """A crew never parks an issue on a person, so neither endpoint that used to
    express that exists: no queue of held items, and no channel for a human to
    answer a crew mid-turn.

    Asserted as ABSENCE at both layers, because either one alone passes on a
    half-revert. The registrar check catches a handler wired back in; the request
    check catches one reachable by some other registration path. And the status has
    to be 404/405 rather than 500 — an app route that raises is indistinguishable
    from a crash, so a stale client would report a broken backend instead of a
    removed feature.
    """

    def test_the_registrar_installs_neither(self):
        registered = _registered()
        for method, path in RETIRED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                self.assertNotIn((method, path), registered)

    def test_no_handler_for_either_survives_in_the_module(self):
        # The route table is data; a handler left behind is what a later edit
        # re-registers by accident.
        for name in ("_handle_crew_guidance", "_handle_crew_escalations", "_escalations"):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(crew_routes, name))

    async def test_a_request_to_either_is_not_served(self):
        app = web.Application()
        crew_routes.register_crew_routes(app)
        crew = self.crew("Andromeda")
        for method, path in RETIRED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                request = _request(
                    method, path,
                    query={"owner": OWNER, "repo": REPO},
                    body={"owner": OWNER, "repo": REPO, "id": crew["id"],
                          "number": 7, "text": "do X"},
                    app=app,
                )
                match = await app.router.resolve(request)
                # A miss resolves to a MatchInfoError carrying the HTTP exception
                # aiohttp would raise; a hit carries none. Reading the exception is
                # what distinguishes "no such route" from "route ran and 404'd",
                # which is the difference this test is about.
                failure = match.http_exception
                self.assertIsNotNone(failure, f"{method} {path} still resolves")
                self.assertIn(failure.status, (404, 405))


# ── POST /issue/comment (the one forge write) ───────────────────────────────


class _StubClient:
    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.result = result or {"id": 4242, "url": "https://example.invalid/c/4242"}
        self.error = error
        self.issue_calls: list[tuple] = []
        self.pr_calls: list[tuple] = []

    def add_issue_comment(self, owner, repo, number, body, **kwargs):
        self.issue_calls.append((owner, repo, number, body, kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def add_pr_comment(self, owner, repo, number, body, **kwargs):
        self.pr_calls.append((owner, repo, number, body, kwargs))
        return self.result


class TestIssueComment(_CrewRouteCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = _StubClient()
        for patcher in (
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(provider, "client_for", return_value=self.client),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _comment(self, **overrides):
        body = {"owner": OWNER, "repo": REPO, "number": 7, "body": "claiming this"}
        body.update(overrides)
        return await self.call("POST", "/issue/comment", body=body)

    async def test_it_posts_and_returns_the_comment_id(self):
        res = await self._comment()
        self.assertEqual(res.status, 200)
        payload = _payload(res)
        # The id is what the claim protocol stores as claim_comment_id and EDITS on
        # later check-ins instead of posting again.
        self.assertEqual(payload["comment_id"], 4242)
        self.assertEqual(payload["number"], 7)
        self.assertEqual(payload["owner"], OWNER)

    async def test_it_calls_the_issue_function_not_the_pull_request_one(self):
        # On GitLab issues and merge requests are separate collections with
        # independent numbering, so add_pr_comment here would comment on an
        # unrelated merge request that happens to share the number.
        await self._comment()
        self.assertEqual(len(self.client.issue_calls), 1)
        self.assertEqual(self.client.pr_calls, [])
        owner, repo, number, body, _kwargs = self.client.issue_calls[0]
        self.assertEqual((owner, repo, number, body), (OWNER, REPO, 7, "claiming this"))

    async def test_a_read_only_repo_is_403(self):
        with mock.patch.object(routes, "_repo_can_write", return_value=False):
            res = await self._comment()
        self.assertEqual(res.status, 403)
        self.assertEqual(_payload(res)["code"], "repo_read_only")
        self.assertEqual(self.client.issue_calls, [])

    async def test_an_undeterminable_permission_fails_closed(self):
        with mock.patch.object(routes, "_repo_can_write", return_value=None):
            res = await self._comment()
        self.assertEqual(res.status, 403)

    async def test_a_provider_refusal_is_403_and_other_failures_are_502(self):
        cases = ((routes.GhPermissionError("nope"), 403), (routes.GhCliError("timeout"), 502))
        for error, status in cases:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    provider, "client_for", return_value=_StubClient(error=error)
                ):
                    res = await self._comment()
                self.assertEqual(res.status, status)

    async def test_an_empty_body_is_400(self):
        res = await self._comment(body="   ")
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "body_required")

    async def test_the_cached_timeline_is_dropped_so_the_claim_reads_back(self):
        # Without this the crew's next timeline read is served the pre-comment
        # cache, concludes its claim never posted, and posts a second one.
        store.write_issue_detail_cache(
            OWNER, REPO, 7, {"number": 7}, [], root=self.root
        )
        path = store.issue_detail_cache_path(OWNER, REPO, 7, self.root)
        self.assertTrue(path.is_file())
        await self._comment()
        self.assertFalse(path.is_file())


# ── non-finite numbers in a request body ────────────────────────────────────
#
# `json` decodes `Infinity`/`-Infinity`/`NaN` by default, and `1e309` overflows to
# `inf` on the way in without saying so. Every numeric field on these records is
# coerced with `int()`, which raises on all of them — an `OverflowError` out of a
# handler is a 500, i.e. the app reporting its own bug for what is a bad request.


#: Every body-taking crew route, with the field a non-finite number is smuggled in
#: through. A table rather than one case, because the refusal is at the ONE place
#: every body passes through: a route added later that skipped it would be visible
#: here rather than only in whichever field it happened to coerce.
_NON_FINITE_BODIES: tuple[tuple[str, str, str], ...] = (
    ("PUT", "/crews/settings", '{"owner": "%s", "repo": "%s", '
                               '"settings": {"claim_ttl_hours": 1e309}}'),
    ("POST", "/crews", '{"owner": "%s", "repo": "%s", "name": "Bode", "max_open": 1e309}'),
    ("PUT", "/crew", '{"owner": "%s", "repo": "%s", "id": "c_dead", "max_open": NaN}'),
    ("DELETE", "/crew", '{"owner": "%s", "repo": "%s", "id": "c_dead", "junk": -Infinity}'),
    ("POST", "/crew/pause", '{"owner": "%s", "repo": "%s", "id": "c_dead", '
                            '"paused": true, "junk": Infinity}'),
    ("PUT", "/crew/work", '{"owner": "%s", "repo": "%s", "crew_id": "c_dead", "number": 7, '
                          '"phase": "claimed", "event": "took it", "event_kind": "claim", '
                          '"pr_number": 1e309}'),
)


class TestNonFiniteNumbersAreRefused(_CrewRouteCase):
    """A bad number in a request body is a 400 — never a 500, and never stored."""

    async def test_every_body_taking_route_refuses_a_non_finite_number(self):
        for method, path, template in _NON_FINITE_BODIES:
            with self.subTest(route=f"{method} {path}"):
                body = json.loads(template % (OWNER, REPO))
                res = await self.call(method, path, body=body)
                self.assertEqual(res.status, 400)
                self.assertEqual(_payload(res)["code"], "non_finite_number")

    async def test_the_reviewed_settings_field_is_the_400_and_nothing_is_written(self):
        """The cited line: ``claim_ttl_hours: 1e309`` decodes to ``inf``, and
        ``int(inf)`` raised out of the handler as a 500 with the settings file
        already read."""
        body = json.loads('{"owner": "%s", "repo": "%s", '
                          '"settings": {"claim_ttl_hours": 1e309}}' % (OWNER, REPO))
        self.assertEqual(body["settings"]["claim_ttl_hours"], float("inf"))

        res = await self.call("PUT", "/crews/settings", body=body)

        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "non_finite_number")
        # The refusal is BEFORE the store, so the repo keeps its default and no
        # settings file was created.
        self.assertFalse(crew_store.settings_path(OWNER, REPO, self.root).is_file())
        self.assertEqual(
            crew_store.read_settings(OWNER, REPO, self.root)["claim_ttl_hours"],
            crew_store.DEFAULT_SETTINGS["claim_ttl_hours"],
        )

    async def test_a_nested_non_finite_number_is_found(self):
        """The walk is not one level deep: ``ci_state`` is a free-form object the
        route forwards whole, so an unwalked body would store a value that makes the
        work item unreadable by any strict parser."""
        crew = self.crew("Andromeda")
        body = json.loads(
            '{"owner": "%s", "repo": "%s", "crew_id": "%s", "number": 7, '
            '"phase": "claimed", "event": "took it", "event_kind": "claim", '
            '"ci_state": {"runs": [{"attempts": 1e309}]}}' % (OWNER, REPO, crew["id"])
        )

        res = await self.call("PUT", "/crew/work", body=body)

        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "non_finite_number")
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root))

    async def test_the_agent_leg_is_refused_too(self):
        """The internal-secret caller reads its body through the same helper, so the
        two legs cannot disagree about which payload is acceptable. That body carries
        no owner/repo/crew id — identity comes from the session — so the refusal has
        to happen before identity is resolved, and it does."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body=json.loads('{"number": 7, "phase": "claimed", "event": "took it", '
                            '"event_kind": "claim", "pr_number": NaN}'),
            internal_auth=True,
            session=f"dashboard:{crew['slot_key']}",
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "non_finite_number")
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root))

    async def test_a_legitimate_number_is_unaffected(self):
        """The guard must not cost a valid body — the same fields, with numbers a
        real caller sends."""
        crew = self.crew("Andromeda", max_open=2)
        settings = _payload(
            await self.call(
                "PUT", "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"claim_ttl_hours": 12}},
            )
        )["settings"]
        self.assertEqual(settings["claim_ttl_hours"], 12)

        updated = _payload(
            await self.call(
                "PUT", "/crew",
                body={"owner": OWNER, "repo": REPO, "id": crew["id"], "max_open": 5},
            )
        )["crew"]
        self.assertEqual(updated["max_open"], 5)

        item = _payload(
            await self.call(
                "PUT", "/crew/work",
                body={
                    "owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                    "phase": "claimed", "event": "took it", "event_kind": "claim",
                    "pr_number": 2271, "ci_state": {"runs": 1},
                },
            )
        )["item"]
        self.assertEqual(item["pr_number"], 2271)


if __name__ == "__main__":
    unittest.main()
