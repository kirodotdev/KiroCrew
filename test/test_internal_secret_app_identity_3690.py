"""Issue #3690 -- the internal-secret transport must carry an app identity.

App-ownership checks gate on ``request["app"]``. The app-token branch publishes
it; the internal-secret branch (the managed MCP set) carried no app claim at
all, so every one of those checks became a NO-OP on that transport and an app
agent granted ``@kirocrew-dashboard`` arrived indistinguishable from the
dashboard user.

``token_auth.derive_caller_app`` is the single rule, resolved once in the
middleware and re-used by the one route that also re-derives for
defense-in-depth. Two properties are load-bearing and each has its own class
below:

* it RESOLVES an app for a caller whose session an app owns -- through any of the
  three registries a caller can be placed by (the slot it names, the slot running
  under its key, the cron job that created it), and
* it is NARROWING-ONLY -- a person's call must leave the claim ABSENT, because
  several sites read a PRESENT empty claim as positive proof of the dashboard
  user, so publishing ``""`` would turn their refusal of this transport into an
  admission.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.token_auth import (
    caller_names_a_missing_slot,
    caller_record_is_missing,
    derive_caller_app,
    token_auth_middleware,
)

SECRET = "internal-secret-for-3690"
INTERNAL = frozenset({"/api/chat"})


class _Slot:
    def __init__(self, app: str = "") -> None:
        self._app = app
        # Real slots default this to "" and set it only when bound to a channel
        # or cron session (``DashboardState``); the derivation must not read a
        # missing attribute as a match.
        self.linked_session_key = ""


class _Job:
    def __init__(self, job_id: str, created_by: str = "") -> None:
        self.id = job_id
        self.created_by = created_by


class _Sub:
    def __init__(self, app: str = "") -> None:
        self.app = app


def _request(
    session_key: str | None,
    slots: dict | None,
    jobs: list | None = None,
    subagents: dict | None = None,
):
    """An internal-secret request on a loopback internal path.

    ``request["app"]`` is modelled with a real dict so a test can assert on
    ABSENCE, which is the whole point of the narrowing-only property -- a
    ``MagicMock.__setitem__`` spy can prove a write happened but not that the
    claim is genuinely unset.
    """
    req = MagicMock(spec=web.Request)
    req.path = "/api/chat"
    req.method = "POST"
    req.query = {}
    req.cookies = {}
    req.remote = "127.0.0.1"
    headers = {"X-Internal-Secret": SECRET}
    if session_key is not None:
        headers["X-Session-Key"] = session_key
    req.headers = headers
    store: dict = {}
    req.__setitem__.side_effect = store.__setitem__
    req.__getitem__.side_effect = store.__getitem__
    req.__contains__.side_effect = store.__contains__
    req.get.side_effect = store.get
    state = MagicMock()
    state._slots = slots
    state.crons._jobs = jobs
    state.subagents._agents = subagents
    req.app = {"state": state}
    return req, store


async def _ok(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _grant(
    session_key: str | None,
    slots: dict | None,
    jobs: list | None = None,
    subagents: dict | None = None,
) -> dict:
    mw = token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _request(session_key, slots, jobs, subagents)
    resp = await mw(req, _ok)
    assert resp.status == 200, "the internal-secret grant itself must be unaffected"
    return store


class TestAnAppOwnedCallerIsResolved:
    """The escalation the issue reports: the guard must now have something to bite on."""

    @pytest.mark.asyncio
    async def test_a_slot_created_by_an_app_publishes_that_app(self) -> None:
        store = await _grant("dashboard:slot-1", {"slot-1": _Slot("file-explorer")})
        assert store.get("app") == "file-explorer", (
            "an app agent reaching a dashboard route over the internal secret was "
            "published as the dashboard user; every request['app'] ownership check "
            "is a no-op for it (issue #3690)"
        )

    @pytest.mark.asyncio
    async def test_the_resolved_app_is_marked_not_the_dashboard_user(self) -> None:
        """The WS scope gate must never infer trust from a falsy claim (CWE-269)."""
        store = await _grant("dashboard:slot-1", {"slot-1": _Slot("file-explorer")})
        assert store.get("is_dashboard_user") is False

    @pytest.mark.asyncio
    async def test_an_unprefixed_session_key_resolves_too(self) -> None:
        store = await _grant("slot-1", {"slot-1": _Slot("mochi")})
        assert store.get("app") == "mochi"


class TestPublishingIsNarrowingOnly:
    """A person's call must leave the claim ABSENT, not empty.

    ``handlers/source_providers`` (``"app" not in request or request["app"] != ""``)
    and ``handlers/kiro_prerequisite`` treat a PRESENT empty claim as positive
    proof of the dashboard user, and today refuse this transport because the key
    is missing. Writing ``""`` here would silently convert those refusals into
    admissions -- a widening hidden inside a narrowing fix.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "session_key, slots, why",
        [
            (None, {}, "no session key at all: the gateway's own calls, the CLI, curl"),
            ("dashboard:ui", {}, "the dashboard UI itself"),
            ("dashboard:slot-1", {"slot-1": _Slot("")}, "a session the person started"),
            ("slack:C123:169.1", None, "a Slack thread, which never had a dashboard slot"),
            ("subagent:abc", {}, "a subagent on a surface with no registry wired"),
            ("dashboard:closed-tab", {}, "a key naming a slot that is gone"),
        ],
    )
    async def test_a_caller_with_no_app_leaves_the_claim_absent(
        self, session_key: str | None, slots: dict | None, why: str
    ) -> None:
        store = await _grant(session_key, slots)
        assert "app" not in store, (
            f"an empty app claim was PUBLISHED for {why} -- that flips the "
            f"fail-closed sites which read a present empty claim as proof of the "
            f"dashboard user from refusing this transport to admitting it"
        )

    @pytest.mark.asyncio
    async def test_a_person_is_not_marked_as_a_non_dashboard_user(self) -> None:
        store = await _grant("dashboard:slot-1", {"slot-1": _Slot("")})
        assert "is_dashboard_user" not in store


class TestASlotRunningUnderALinkedKeyStillYieldsItsApp:
    """A channel- or cron-bound slot runs its turns under ``linked_session_key``
    ("when set, _run_chat uses this as session key" -- ``DashboardState``), so the
    caller presents THAT key and a lookup by slot name misses it.

    Without the second lookup the slot's ``_app`` is invisible and the caller
    reads as the person, which is the exact shape this fix exists to end: the
    slot IS in the registry and may carry an owner.
    """

    @pytest.mark.asyncio
    async def test_a_channel_linked_app_slot_is_resolved(self) -> None:
        slot = _Slot("file-explorer")
        slot.linked_session_key = "slack:C123:169.1"
        store = await _grant("slack:C123:169.1", {"channel-C123": slot})
        assert store.get("app") == "file-explorer", (
            "a slot bound to a channel session was read as the dashboard user "
            "because the lookup only tried the slot name, so every ownership "
            "check stayed a no-op for it"
        )

    @pytest.mark.asyncio
    async def test_a_linked_slot_the_person_owns_stays_unscoped(self) -> None:
        """Narrowing-only holds here too: locating the slot must not invent an app."""
        slot = _Slot("")
        slot.linked_session_key = "slack:C123:169.1"
        store = await _grant("slack:C123:169.1", {"channel-C123": slot})
        assert "app" not in store

    def test_a_non_matching_linked_key_does_not_bleed(self) -> None:
        slot = _Slot("file-explorer")
        slot.linked_session_key = "slack:C999:1.0"
        assert derive_caller_app({"channel-C999": slot}, "slack:C123:169.1") == ""

    def test_the_direct_lookup_still_wins(self) -> None:
        """A slot addressed by its own name must not be overridden by a scan."""
        named = _Slot("named-app")
        linked = _Slot("linked-app")
        linked.linked_session_key = "dashboard:slot-1"
        assert derive_caller_app({"slot-1": named, "other": linked}, "dashboard:slot-1") == (
            "named-app"
        )

    def test_a_registry_without_values_does_not_raise(self) -> None:
        """A raise here would turn an authenticated request into a 500."""

        class _Hostile:
            def get(self, _k):  # noqa: ANN001, ANN202
                return None

            def values(self):  # noqa: ANN202
                raise RuntimeError("partial registry")

        assert derive_caller_app(_Hostile(), "slack:C1:2") == ""


class TestAnAppCreatedCronIsResolvedFromTheCronRegistry:
    """A cron created by an app is tagged ``created_by="app:<name>"`` (``CronSDK``),
    so its owner is a matter of record -- and a cron usually has no dashboard slot,
    which is why the slot lookups cannot see it.

    Resolving it positively is what lets an app-owned cron be confined while the
    person's own crons keep working. Refusing every unplaceable delegated caller
    instead would take cron and subagent tools from the person too.
    """

    @pytest.mark.asyncio
    async def test_an_app_created_cron_publishes_its_owning_app(self) -> None:
        store = await _grant("cron:job-9", {}, jobs=[_Job("job-9", "app:file-explorer")])
        assert store.get("app") == "file-explorer", (
            "an app-owned cron with no slot was published as the dashboard user, "
            "so it could reach the agent notification publish path (which refuses "
            "app callers) and impersonate a system notification"
        )

    @pytest.mark.asyncio
    async def test_the_persons_own_cron_keeps_working_unscoped(self) -> None:
        """Cron notifications are a first-class use; this must not be confined."""
        store = await _grant("cron:job-9", {}, jobs=[_Job("job-9", "user")])
        assert "app" not in store

    def test_an_unknown_cron_job_yields_no_app(self) -> None:
        """The derivation invents nothing for an unknown job. The REQUEST for such
        a caller is refused -- see
        ``TestADelegatedCallerWhoseRecordIsGoneIsRefused`` -- so this asserts at
        the pure-function level, where no refusal exists."""
        assert derive_caller_app({}, "cron:ghost", [_Job("job-9", "app:mochi")]) == ""

    def test_a_subagent_key_is_not_resolved_from_the_cron_registry(self) -> None:
        """Only a cron key may be looked up there; a subagent id is a different
        namespace and a collision must not hand it an app."""
        assert derive_caller_app({}, "subagent:job-9", [_Job("job-9", "app:x")]) == ""

    def test_a_hostile_job_registry_does_not_raise(self) -> None:
        class _Boom:
            def __iter__(self):  # noqa: ANN204
                raise RuntimeError("cron store mid-swap")

        assert derive_caller_app({}, "cron:j", _Boom()) == ""

    def test_no_cron_registry_yields_no_app(self) -> None:
        assert derive_caller_app({}, "cron:j") == ""


class TestAKeyNamingAMissingSlotIsNotProofOfThePerson:
    """``caller_names_a_missing_slot`` -- the distinction the plain ``""`` cannot carry.

    ``derive_caller_app`` returning ``""`` covers two different situations: a
    caller that never had a slot (a Slack thread, the person's own cron) and a
    ``dashboard:`` key whose slot is GONE. The first is proof no app owns the
    caller; the second is a failure to attribute, because the ``_app`` that would
    have confined it is exactly what got popped when the tab closed.

    One writer, one reader: the route that publishes ``source="system"``.
    Deliberately not applied in the middleware -- a popped slot no longer says
    whose tab it was, so a central refusal would also refuse the person's own
    in-flight calls on every internal route.
    """

    def test_a_dashboard_key_with_no_slot_is_flagged(self) -> None:
        assert caller_names_a_missing_slot({}, "dashboard:closed-tab") is True

    def test_a_live_slot_is_not_flagged(self) -> None:
        assert caller_names_a_missing_slot({"slot-1": _Slot("x")}, "dashboard:slot-1") is False

    def test_a_slot_found_by_its_linked_key_is_not_flagged(self) -> None:
        slot = _Slot("x")
        slot.linked_session_key = "dashboard:slot-1"
        assert caller_names_a_missing_slot({"other": slot}, "dashboard:slot-1") is False

    @pytest.mark.parametrize(
        "session_key",
        ["dashboard:ui", "slack:C1:2", "cron:job-9", "subagent:abc", "", "slot-1"],
    )
    def test_a_caller_that_never_named_a_slot_is_not_flagged(self, session_key: str) -> None:
        """Only a ``dashboard:`` key names one; flagging the others would refuse
        callers that legitimately have no slot -- cron notifications above all."""
        assert caller_names_a_missing_slot({}, session_key) is False


class TestTheAgentNotificationRouteRefusesAnUnattributableCaller:
    """The route publishes ``source="system"`` on the system.agent channel, so it
    already refuses app tokens by name. A tab closed mid-call takes the ``_app``
    that check reads with it, so an app-owned session in that race would publish
    as though it were the person.
    """

    @pytest.mark.asyncio
    async def test_a_caller_whose_slot_is_gone_is_refused(self) -> None:
        from kiro_crew.dashboard.handlers import messaging

        req = MagicMock(spec=web.Request)
        req.headers = {"X-Session-Key": "dashboard:closed-tab"}
        store = {"internal_auth": True}
        req.get.side_effect = store.get
        state = MagicMock()
        state._slots = {}
        req.app = {"state": state}

        resp = await messaging.api_notification_agent_push(req)
        assert resp.status == 403, (
            "a caller whose own slot is gone published a system notification; the "
            "app-token check cannot see the app because the slot carrying it was "
            "popped when the tab closed"
        )

    @pytest.mark.asyncio
    async def test_a_cron_caller_is_not_refused_by_that_guard(self) -> None:
        """Cron notifications are a first-class use and a cron has no slot by
        nature, so the guard must not reach them.

        Proven by getting PAST it: with no body plumbing on the mock, the handler
        fails downstream in body reading. The guard returns a 403 response rather
        than raising, so an exception from deeper in the handler is positive
        evidence the cron caller was not refused here.
        """
        from kiro_crew.dashboard.handlers import messaging

        req = MagicMock(spec=web.Request)
        req.headers = {"X-Session-Key": "cron:job-9"}
        store = {"internal_auth": True}
        req.get.side_effect = store.get
        state = MagicMock()
        state._slots = {}
        req.app = {"state": state}

        with pytest.raises(TypeError):
            await messaging.api_notification_agent_push(req)


class TestAnAppSpawnedSubagentIsResolvedFromTheSubagentRegistry:
    """A child spawned by an app carries it in ``SubagentInfo.app``, persisted for
    exactly this purpose -- the field's own note says it is kept so "the child's
    per-tool-call gate can resolve the app's Level-2 profile", because otherwise
    "the child's ongoing tool calls run unconstrained by the app scope".

    A tool call arriving over the internal secret IS one of those ongoing calls,
    so reading the field here is the field doing its declared job.
    """

    @pytest.mark.asyncio
    async def test_an_app_spawned_subagent_publishes_its_spawning_app(self) -> None:
        store = await _grant("subagent:abc123", {}, subagents={"abc123": _Sub("file-explorer")})
        assert store.get("app") == "file-explorer", (
            "an app-spawned child was published as the dashboard user, so it "
            "could reach the agent notification publish path and impersonate a "
            "system notification"
        )

    @pytest.mark.asyncio
    async def test_a_person_spawned_subagent_stays_unscoped(self) -> None:
        store = await _grant("subagent:abc123", {}, subagents={"abc123": _Sub("")})
        assert "app" not in store

    def test_an_unknown_subagent_yields_no_app(self) -> None:
        """As with cron: the derivation invents nothing, and the request itself is
        refused (see ``TestADelegatedCallerWhoseRecordIsGoneIsRefused``)."""
        assert derive_caller_app({}, "subagent:ghost", None, {"abc123": _Sub("mochi")}) == ""

    def test_a_cron_key_is_not_resolved_from_the_subagent_registry(self) -> None:
        """Separate namespaces; an id collision must not hand one the other's app."""
        assert derive_caller_app({}, "cron:abc123", None, {"abc123": _Sub("x")}) == ""

    def test_a_hostile_subagent_registry_does_not_raise(self) -> None:
        class _Boom:
            def get(self, _k):  # noqa: ANN001, ANN202
                raise RuntimeError("registry mid-mutation")

        assert derive_caller_app({}, "subagent:a", None, _Boom()) == ""

    def test_no_subagent_registry_yields_no_app(self) -> None:
        assert derive_caller_app({}, "subagent:a") == ""


class TestAnOwnerlessRecordDoesNotEndTheSearch:
    """Each registry answers only "yes, THIS app owns it"; an ownerless answer
    falls through.

    A session can be recorded in more than one place and only some records carry
    the owner. The forcing case: an app-created cron with a cron-born tab is in
    the SLOT registry with no ``_app`` (a channel/cron-born slot is created
    without one) and in the CRON registry WITH its ``created_by`` owner. Stopping
    at the slot would hand that app the dashboard user's reach.
    """

    @pytest.mark.asyncio
    async def test_an_ownerless_linked_slot_does_not_mask_the_cron_owner(self) -> None:
        tab = _Slot("")  # cron-born tabs carry no _app
        tab.linked_session_key = "cron:job-9"
        store = await _grant(
            "cron:job-9", {"cron-tab": tab}, jobs=[_Job("job-9", "app:file-explorer")]
        )
        assert store.get("app") == "file-explorer", (
            "the ownerless cron-born tab ended the search before the cron "
            "registry was asked, so an app-owned cron was published as the "
            "dashboard user"
        )

    @pytest.mark.asyncio
    async def test_an_ownerless_named_slot_does_not_mask_the_subagent_owner(self) -> None:
        store = await _grant(
            "subagent:abc123",
            {"abc123": _Slot("")},
            subagents={"abc123": _Sub("mochi")},
        )
        assert store.get("app") == "mochi"

    def test_the_search_still_ends_on_a_positive_owner(self) -> None:
        """Falling through must not let a later registry override an earlier
        positive answer."""
        named = _Slot("named-app")
        assert (
            derive_caller_app({"job-9": named}, "cron:job-9", [_Job("job-9", "app:other")])
            == "named-app"
        )


class TestAStatelessCronKeyResolvesToItsJob:
    """A cron session key has two forms: ``cron:<job_id>`` for a persistent job
    and ``cron:<job_id>:<run_id>`` for a stateless one (``cron._build_prompt``).

    Taking the whole remainder after the first colon yields ``<job_id>:<run_id>``
    for the stateless form, which matches no job -- so an app-owned stateless
    cron would resolve to no owner and be handed the dashboard user's reach.
    """

    @pytest.mark.asyncio
    async def test_a_stateless_run_key_still_finds_its_owner(self) -> None:
        store = await _grant("cron:job-9:a1b2c3d4", {}, jobs=[_Job("job-9", "app:file-explorer")])
        assert store.get("app") == "file-explorer", (
            "the stateless cron key's run-id segment was folded into the job id, "
            "so the lookup missed and an app-owned cron read as the person"
        )

    @pytest.mark.asyncio
    async def test_a_persistent_run_key_is_unaffected(self) -> None:
        store = await _grant("cron:job-9", {}, jobs=[_Job("job-9", "app:mochi")])
        assert store.get("app") == "mochi"

    def test_a_stateless_key_for_a_person_owned_job_stays_unscoped(self) -> None:
        assert derive_caller_app({}, "cron:job-9:run", [_Job("job-9", "user")]) == ""

    def test_a_bare_prefix_with_no_id_resolves_to_nothing(self) -> None:
        assert derive_caller_app({}, "cron:", [_Job("", "app:x")]) == ""
        assert derive_caller_app({}, "subagent:", None, {"": _Sub("x")}) == ""


class TestADelegatedCallerWhoseRecordIsGoneIsRefused:
    """A ``cron:``/``subagent:`` key names recorded work, and that record is where
    its owner lives -- so absence is "the proof of who this runs for is gone",
    not "nothing to confine".

    Narrow by construction: a live subagent is always registered (only ``done``
    records are evicted, by ``evict_completed_agents``) and a cron job stays until
    removed, so this fires for a job DELETED while its run still makes calls. A
    Slack thread has no record to be missing and is untouched.
    """

    @pytest.mark.asyncio
    async def test_a_cron_deleted_mid_run_is_refused(self) -> None:
        mw = token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
        req, _ = _request("cron:job-9", {}, jobs=[])  # job removed while running
        resp = await mw(req, _ok)
        assert resp.status == 403, (
            "a cron whose job was deleted mid-run kept the dashboard user's "
            "reach; the deleted record was the only proof of its owner"
        )

    @pytest.mark.asyncio
    async def test_a_subagent_missing_from_the_registry_is_refused(self) -> None:
        mw = token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
        req, _ = _request("subagent:gone", {}, subagents={})
        resp = await mw(req, _ok)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_live_record_is_admitted(self) -> None:
        store = await _grant("cron:job-9", {}, jobs=[_Job("job-9", "user")])
        assert "app" not in store  # the person's cron, unconfined and working

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "session_key",
        ["slack:C123:169.1", "dashboard:slot-1", "dashboard:ui", None],
    )
    async def test_a_caller_with_no_record_to_lose_is_admitted(
        self, session_key: str | None
    ) -> None:
        """Only delegated keys name a record. Refusing anything else would deny
        Slack threads, channel sessions and the dashboard itself."""
        mw = token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
        req, _ = _request(session_key, {"slot-1": _Slot("")}, jobs=[], subagents={})
        resp = await mw(req, _ok)
        assert resp.status == 200

    def test_an_absent_registry_is_not_a_refusal(self) -> None:
        """A surface wired without the registry (the --slack-only API server)
        must not refuse every delegated caller over a missing dependency."""
        assert caller_record_is_missing("cron:job-9", None, None) is False
        assert caller_record_is_missing("subagent:abc", None, None) is False

    def test_an_unreadable_present_registry_fails_closed(self) -> None:
        """A registry that IS present but throws on read must fail CLOSED (CWE-636).

        This is distinct from ``test_an_absent_registry_is_not_a_refusal``: there
        the surface is wired without a registry ON PURPOSE (the ``--slack-only``
        API server), and ``caller_record_is_missing`` short-circuits on ``None``
        before ever reaching the existence helpers. Here the registry object is
        present but torn / mid-swap, so the read raises. Admitting the delegated
        caller on that error escalates it to the unscoped dashboard user with its
        app-scope silently dropped, which SAX-04 outcome_3 forbids: an auth
        decision must fail closed when the source it depends on is unavailable.
        A present-but-unreadable registry therefore reports the record as MISSING
        (``caller_record_is_missing`` -> True), so the caller is refused."""

        class _Boom:
            def __iter__(self):  # noqa: ANN204
                raise RuntimeError("mid-swap")

            def get(self, _k):  # noqa: ANN001, ANN202
                raise RuntimeError("mid-mutation")

        assert caller_record_is_missing("cron:j", _Boom(), None) is True
        assert caller_record_is_missing("subagent:a", None, _Boom()) is True

    def test_a_subagents_registry_without_get_fails_closed(self) -> None:
        """A subagent registry object missing a ``get`` (unreadable shape) fails
        closed too: the record reads as MISSING, so the delegated caller is
        refused rather than escalated (CWE-636 / SAX-04 outcome_3)."""

        class _NoGet:
            pass

        assert caller_record_is_missing("subagent:a", None, _NoGet()) is True

    def test_a_resolvable_app_owner_is_never_refused(self) -> None:
        """The deny is the else-branch of resolution, so a found owner wins."""
        assert caller_record_is_missing("cron:job-9", [_Job("job-9", "app:x")], None) is False


class TestTheRuleIsPureAndShared:
    """``derive_caller_app`` takes registries and the key -- never a request.

    The folder route re-derives for defense-in-depth by calling THIS function,
    so the middleware and the route cannot drift into two different rules (the
    drift that made the per-route patch necessary in the first place).
    """

    def test_no_slot_registry_still_resolves_a_cron_owner(self) -> None:
        """The ``--slack-only`` API server has no slots, but a cron still has an owner."""
        assert derive_caller_app(None, "subagent:x") == ""
        assert derive_caller_app(None, "cron:j", [_Job("j", "app:mochi")]) == "mochi"

    def test_the_folder_route_reads_the_same_rule(self) -> None:
        from kiro_crew.dashboard import chat_folders

        state = MagicMock()
        state._slots = {"slot-1": _Slot("file-explorer")}
        req = MagicMock(spec=web.Request)
        req.headers = {"X-Session-Key": "dashboard:slot-1"}
        req.get.return_value = ""  # middleware claim absent
        assert chat_folders._effective_request_app(state, req) == "file-explorer"

    def test_the_folder_route_prefers_a_published_claim(self) -> None:
        from kiro_crew.dashboard import chat_folders

        state = MagicMock()
        state._slots = {}
        req = MagicMock(spec=web.Request)
        req.headers = {}
        req.get.return_value = "already-derived"
        assert chat_folders._effective_request_app(state, req) == "already-derived"

    def test_scope_is_never_taken_from_a_body_or_argument(self) -> None:
        """Signature guard: the rule reads server-side registries and a key only.

        A caller that could name its own scope could name someone else's, so the
        function must have no way to see a request, a body, or tool arguments.
        Growing another REGISTRY parameter is fine; growing a request-shaped one
        is the thing this pins.
        """
        import inspect

        params = list(inspect.signature(derive_caller_app).parameters)
        assert params == ["slots", "session_key", "jobs", "subagents"], (
            f"derive_caller_app's signature changed ({params}); every parameter "
            f"must be a server-side registry or the caller's own key -- anything "
            f"request- or argument-shaped lets a caller name its own scope"
        )


class TestTheMcpToolLayerResolvesLinkedKeysToo:
    """``mcp_dashboard._caller_app_scope`` is the same rule for the MCP tool set.

    It matched rows by ``key`` only, though the rows serialize
    ``linked_session_key`` (``DashboardState``), so a channel-linked app slot read
    as unscoped there even after the middleware learned to resolve it. Leaving
    that in place would keep one instance of the exact bug class this change
    exists to end.
    """

    def test_a_channel_linked_app_row_is_resolved(self) -> None:
        from kiro_crew.mcp_dashboard import _caller_app_scope

        rows = [
            {
                "key": "channel-C123",
                "app": "file-explorer",
                "linked_session_key": "slack:C123:169.1",
            }
        ]
        assert _caller_app_scope("slack:C123:169.1", rows) == "file-explorer"

    def test_the_direct_key_match_still_wins(self) -> None:
        from kiro_crew.mcp_dashboard import _caller_app_scope

        rows = [
            {"key": "slot-1", "app": "named-app", "linked_session_key": ""},
            {"key": "other", "app": "linked-app", "linked_session_key": "dashboard:slot-1"},
        ]
        assert _caller_app_scope("dashboard:slot-1", rows) == "named-app"

    def test_an_unplaceable_delegated_caller_is_still_refused(self) -> None:
        """The MCP layer's own fail-closed rule must survive the new lookup."""
        from kiro_crew.mcp_dashboard import _caller_app_scope

        assert _caller_app_scope("subagent:abc", []) is None
        assert _caller_app_scope("cron:job-9", []) is None
        assert _caller_app_scope("dashboard:gone", []) is None

    def test_an_ownerless_linked_row_does_not_bypass_the_refusal(self) -> None:
        """A cron-born row carries no ``app``, so returning on that match would
        answer "" (the person) for a delegated caller this layer fails closed on
        -- weakening it instead of extending it."""
        from kiro_crew.mcp_dashboard import _caller_app_scope

        rows = [{"key": "cron-tab", "app": "", "linked_session_key": "cron:job-9"}]
        assert _caller_app_scope("cron:job-9", rows) is None

    def test_an_app_owned_linked_row_is_still_resolved(self) -> None:
        from kiro_crew.mcp_dashboard import _caller_app_scope

        rows = [{"key": "cron-tab", "app": "mochi", "linked_session_key": "cron:job-9"}]
        assert _caller_app_scope("cron:job-9", rows) == "mochi"

    def test_a_slotless_caller_is_still_unscoped(self) -> None:
        from kiro_crew.mcp_dashboard import _caller_app_scope

        assert _caller_app_scope("slack:C1:2", []) == ""
