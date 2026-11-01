"""Issue Radar's crew control routes, and non-finite numbers in a crew route's body.

``POST /crew/pause``; stopping a crew, which revokes its execution before the route
answers; an update that withdraws auto-approval; ``/crews/settings``; the endpoints that
would hold work for a human, which the registrar must not install;
``POST /issue/comment``, the one forge write; and a non-finite number in the body of each
local body-taking route (``_NON_FINITE_BODIES``), which is a 400 and never a 500. Every
test class extends ``_CrewRouteCase`` from ``issue_radar_crew_routes_helpers.py``.
"""

import json
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp import web
from issue_radar_crew_routes_helpers import (
    OWNER,
    REPO,
    _CrewRouteCase,
    _payload,
    _registered,
    _request,
    _Slot,
    _State,
)
from off_loop_helpers import off_loop

from kiro_crew.apps.builtins.issue_radar.backend import (
    crew_routes,
    crew_runtime,
    crew_store,
    provider,
    routes,
    store,
)

#: Endpoints that would hold an issue for a human, which this app must not
#: expose again. Kept as an explicit table rather than deleted with their tests: the
#: registrar is checked against ``CREW_ROUTES`` by name, so a re-added handler under
#: a path nobody enumerates would pass every other test in this file silently.
RETIRED_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/crew/guidance"),
    ("GET", "/crews/escalations"),
)


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
        self.messages: list[str] = []

    def get_by_slot(self, slot_key: str) -> _Loop | None:
        return self.loop if slot_key == self.loop.slot_key else None

    async def update(self, loop_id: str, **kw) -> None:
        if "active" in kw:
            self.loop.active = bool(kw["active"])
        if "message" in kw:
            self.messages.append(kw["message"])


# ── POST /crew/pause ────────────────────────────────────────────────────────


class TestPause(_CrewRouteCase):
    async def test_pausing_stores_the_reason(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST",
            "/crew/pause",
            body={
                "owner": OWNER,
                "repo": REPO,
                "id": crew["id"],
                "paused": True,
                "reason": "waiting on the release",
            },
        )
        self.assertEqual(res.status, 200)
        paused = _payload(res)["crew"]
        self.assertIs(paused["enabled"], False)
        self.assertEqual(paused["paused_reason"], "waiting on the release")

    async def test_resuming_clears_the_reason(self):
        crew = self.crew("Andromeda")
        await self.call(
            "POST",
            "/crew/pause",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": True, "reason": "hold"},
        )
        resumed = _payload(
            await self.call(
                "POST",
                "/crew/pause",
                body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": False},
            )
        )["crew"]
        self.assertIs(resumed["enabled"], True)
        # A stale reason on a running crew would explain why it is stopped.
        self.assertEqual(resumed["paused_reason"], "")

    async def test_paused_must_be_a_boolean(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST",
            "/crew/pause",
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
            crew_runtime,
            "revoke_crew_execution",
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
            "POST",
            "/crew/pause",
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
        res = await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], unattended=False)
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
        res = await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], unattended=True)
        self.assertEqual(res.status, 200)
        self.assertIs(_payload(res)["crew"]["unattended"], True)
        self.assertTrue(slot._trust)
        self.assertTrue(svc.loop.active)

    async def test_a_patch_without_unattended_leaves_an_armed_crew_armed(self):
        """A rename is not a stop. Revoking on every patch would make editing a live
        crew's model or name cost it its next turn."""
        crew = self.crew("Andromeda")
        slot, app, svc = self._armed(crew)
        res = await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], name="Whirlpool")
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
        res = await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], unattended=True)
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
        res = await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], unattended=False)
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
        res = await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], enabled=False)
        self.assertEqual(res.status, 200)
        self.assertIs(_payload(res)["crew"]["unattended"], True)
        self.assertFalse(slot._trust)
        self.assertFalse(svc.loop.active)

    async def test_editing_the_extra_prompt_refreshes_the_armed_loop_message(self):
        """An idle-timer turn sends the loop's cached prompt, so an edit must reach it."""
        crew = self.crew("Andromeda")
        slot, app, svc = self._armed(crew)
        text = "Ask the requester before passing a product decision."
        res = await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], extra_prompt=text)
        self.assertEqual(res.status, 200)
        self.assertEqual(len(svc.messages), 1)
        self.assertIn(text, svc.messages[0])
        # Clearing it refreshes too, and the cleared text is gone.
        await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], extra_prompt="")
        self.assertEqual(len(svc.messages), 2)
        self.assertNotIn(text, svc.messages[1])
        # An unrelated edit leaves the cached message alone.
        await self._patch(app, svc, owner=OWNER, repo=REPO, id=crew["id"], name="Whirlpool")
        self.assertEqual(len(svc.messages), 2)
        self.assertTrue(svc.loop.active)

    async def test_a_withdrawal_without_a_dashboard_is_not_a_500(self):
        """The gateway can serve this route with no dashboard state attached, and the
        record is the durable truth — an unreachable grant is not a failed save."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew",
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
                "PUT",
                "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"claim_ttl_hours": 12}},
            )
        )["settings"]
        self.assertEqual(first["claim_ttl_hours"], 12)

        second = _payload(
            await self.call(
                "PUT",
                "/crews/settings",
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
                "PUT",
                "/crews/settings",
                body={
                    "owner": OWNER,
                    "repo": REPO,
                    "settings": {"needs_human_label": " needs: maintainer "},
                },
            )
        )["settings"]
        self.assertEqual(stored["needs_human_label"], "needs: maintainer")

        blanked = _payload(
            await self.call(
                "PUT",
                "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"needs_human_label": "  "}},
            )
        )["settings"]
        self.assertEqual(blanked["needs_human_label"], "needs: maintainer")

    async def test_put_requires_a_settings_object(self):
        for value in (None, "12", 12, ["a"]):
            with self.subTest(settings=value):
                res = await self.call(
                    "PUT",
                    "/crews/settings",
                    body={"owner": OWNER, "repo": REPO, "settings": value},
                )
                self.assertEqual(res.status, 400)
                self.assertEqual(_payload(res)["code"], "invalid_settings")


# ── the endpoints that would hold work for a human ──────────────────────


class TestNothingWaitsForAHuman(_CrewRouteCase):
    """A crew never parks an issue on a person, so neither endpoint that would
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
                    method,
                    path,
                    query={"owner": OWNER, "repo": REPO},
                    body={
                        "owner": OWNER,
                        "repo": REPO,
                        "id": crew["id"],
                        "number": 7,
                        "text": "do X",
                    },
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
        store.write_issue_detail_cache(OWNER, REPO, 7, {"number": 7}, [], root=self.root)
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
    (
        "PUT",
        "/crews/settings",
        '{"owner": "%s", "repo": "%s", ' '"settings": {"claim_ttl_hours": 1e309}}',
    ),
    ("POST", "/crews", '{"owner": "%s", "repo": "%s", "name": "Bode", "max_open": 1e309}'),
    ("PUT", "/crew", '{"owner": "%s", "repo": "%s", "id": "c_dead", "max_open": NaN}'),
    ("DELETE", "/crew", '{"owner": "%s", "repo": "%s", "id": "c_dead", "junk": -Infinity}'),
    (
        "POST",
        "/crew/pause",
        '{"owner": "%s", "repo": "%s", "id": "c_dead", ' '"paused": true, "junk": Infinity}',
    ),
    (
        "PUT",
        "/crew/work",
        '{"owner": "%s", "repo": "%s", "crew_id": "c_dead", "number": 7, '
        '"phase": "claimed", "event": "took it", "event_kind": "claim", '
        '"pr_number": 1e309}',
    ),
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
        body = json.loads(
            '{"owner": "%s", "repo": "%s", '
            '"settings": {"claim_ttl_hours": 1e309}}' % (OWNER, REPO)
        )
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
        self.assertIsNone(
            off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        )

    async def test_the_agent_leg_is_refused_too(self):
        """The internal-secret caller reads its body through the same helper, so the
        two legs cannot disagree about which payload is acceptable. That body carries
        no owner/repo/crew id — identity comes from the session — so the refusal has
        to happen before identity is resolved, and it does."""
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT",
            "/crew/work",
            body=json.loads(
                '{"number": 7, "phase": "claimed", "event": "took it", '
                '"event_kind": "claim", "pr_number": NaN}'
            ),
            internal_auth=True,
            session=f"dashboard:{crew['slot_key']}",
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "non_finite_number")
        self.assertIsNone(
            off_loop(crew_store.read_work_item, OWNER, REPO, crew["id"], 7, self.root)
        )

    async def test_a_legitimate_number_is_unaffected(self):
        """The guard must not cost a valid body — the same fields, with numbers a
        real caller sends."""
        crew = self.crew("Andromeda", max_open=2)
        settings = _payload(
            await self.call(
                "PUT",
                "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"claim_ttl_hours": 12}},
            )
        )["settings"]
        self.assertEqual(settings["claim_ttl_hours"], 12)

        updated = _payload(
            await self.call(
                "PUT",
                "/crew",
                body={"owner": OWNER, "repo": REPO, "id": crew["id"], "max_open": 5},
            )
        )["crew"]
        self.assertEqual(updated["max_open"], 5)

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
                    "pr_number": 2271,
                    "ci_state": {"runs": 1},
                },
            )
        )["item"]
        self.assertEqual(item["pr_number"], 2271)
