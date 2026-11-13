"""The agent's read/write path into its own Issue Radar crew ledger.

The two tools are declared in the app's ``app.json`` (``agentTools``, ``strictSession``)
and served by its ``backend/agent_tools.py``; ``issue_radar_agent_tools_helpers`` runs a
call through the real ``kirocrew-core`` path with the app's route legs faked. The ledger
is the crew's memory across compaction and restarts, so:

  * it is reachable only through the forwarded tool call; a raw HTTP call earns 403;
  * a partial write never erases what an earlier write stored;
  * a phase never moves without a logged reason (upsert and append are ONE tool);
  * public strings carry no machine identity: the event log feeds a github.com comment.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from issue_radar_agent_tools_helpers import (
    CREW_SESSION,
    READ_TOOL,
    RECORD_TOOL,
    call,
    clean,
    descriptor,
    forwarded,
    listed,
    shipped,
)
from turn_harness import SlotSpec, TurnScript, run_turn

from kiro_crew import mcp_core
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.apps.builtins.issue_radar.backend import agent_tools
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.validation import ValidationError

READ_PATH = "/api/apps/issue-radar/crew"
WORK_PATH = "/api/apps/issue-radar/crew/work"

#: What the read route answers with. Identity lives here and ONLY here — no tool
#: argument can select a crew — so the fixture doubles as the contract.
#:
#: The top-level ``owner``/``repo`` are the route's own echo, and they are the pair
#: that is ALWAYS present: a crew with no work items yet has no other source. The
#: copies on the crew record and the item mirror what the store holds, so this
#: fixture also covers ``_crew_identity``'s tolerance of all three locations.
#: ``test_issue_radar_crew_routes.TestAgentIdentityIsTheSession`` asserts the real
#: handler produces this shape, so the stub cannot drift from the route.
CREW_PAYLOAD = {
    "owner": "acme",
    "repo": "widget",
    "provider": "github",
    "host": "github.com",
    "crew": {"id": "c_7f3a", "name": "Whirlpool", "owner": "acme", "repo": "widget"},
    "settings": {"claim_ttl_hours": 48, "needs_human_label": "crew: needs human"},
    "items": [{"crew_id": "c_7f3a", "owner": "acme", "repo": "widget", "number": 12}],
    "events": [],
    "skipped_numbers": [4, 91],
    "recent_skips": [{"number": 91, "reason": "needs a design decision", "scope": "needs-design"}],
    "counts": {"open": 1},
}

_ANSWER = AcpEvent(kind=EVENT_TEXT_CHUNK, text="swept the queue")
_LANDED = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _clean(**over):
    args = {"number": 12}
    args.update(over)
    return clean(RECORD_TOOL, args)


def _record(payload: dict | None = None, put_result: dict | None = None, **over):
    """Invoke the record tool through the core with the app's route legs stubbed.

    Returns ``(captured, output)`` where ``captured`` holds what each leg saw and the
    write leg's body. Module-level rather than a base-class method: a class that
    inherited it would also inherit and re-run that class's tests.
    """
    cleaned = _clean(**over)
    # The tool is declared ``strictSession``: the core's strict gate resolves the
    # session, so stubbing only the legs would leave identity unexercised.
    with forwarded(read=CREW_PAYLOAD if payload is None else payload, work=put_result) as captured:
        out = call(RECORD_TOOL, cleaned)
    return captured, out


class TestGatewayPathsAreReachableWithTheInternalSecret(unittest.TestCase):
    def test_an_explicit_session_key_beats_the_lenient_resolver(self):
        """``_get``/``_put`` send the passed session key, not one they resolve.

        The lenient resolver is stubbed to a DIFFERENT session, the only way to tell
        "honoured" from "coincidentally equal".
        """
        import urllib.request

        for helper, kwargs in ((mcp_core._get, {}), (mcp_core._put, {"body": {}})):
            with self.subTest(helper=helper.__name__):
                seen: dict = {}

                class _Resp:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def read(self):
                        return b"{}"

                def _capture(req: urllib.request.Request, timeout=None, unix_socket_path=None):
                    seen["key"] = req.headers.get("X-session-key")
                    return _Resp()

                with patch.object(mcp_core, "_resolve_session_key", return_value="OTHER"):
                    with patch.object(mcp_core, "_internal_secret", return_value="s"):
                        # Patch `_api_urlopen`, the transport the helpers call; the
                        # module also exports `loopback_urlopen`, which they do not use.
                        # Any transport failure is swallowed into `{"error": ...}`, so
                        # the fake must also accept `unix_socket_path`.
                        with patch.object(mcp_core, "_api_urlopen", _capture):
                            got = helper("/api/x", session_key="MINE", **kwargs)

                # Fail on the real reason, not on a KeyError three lines later.
                assert "error" not in got, f"{helper.__name__} never sent a request: {got}"
                assert (
                    seen["key"] == "MINE"
                ), f"{helper.__name__} ignored the verified key and re-resolved one"

    def test_no_issue_radar_path_is_admitted_to_the_internal_secret(self):
        """The tools reach the crew routes in-process, through the app's own route.

        ``token_auth`` matches ``path == p or path.startswith(p + "/")``, so any entry
        under ``/api/apps/issue-radar`` would hand the app's routes, the forge-write ones
        (``/labels/apply``, ``/issue/close``, the comment routes) among them, to anything
        holding the internal secret. The forwarded call enters at
        ``/api/app-tools/call`` instead.
        """
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        for entry in _MIXED_INTERNAL_API_PATHS | _STRICT_INTERNAL_API_PATHS:
            assert entry != "/api/apps"
            assert not (entry + "/").startswith("/api/apps/issue-radar/"), entry
        assert "/api/app-tools/call" in _STRICT_INTERNAL_API_PATHS

    def test_both_tools_are_declared_strict_session(self):
        # An unattended crew has no human watching its turn, so the identity gate is
        # the only thing standing between a subagent and its parent crew's ledger.
        assert shipped(READ_TOOL).decl.strict_session is True
        assert shipped(RECORD_TOOL).decl.strict_session is True
        assert shipped(READ_TOOL).route == "agent-tools"


def _listed_spec(name: str) -> dict:
    with listed():
        return next(t for t in mcp_core._list_tools() if t["name"] == name)


def _validated_fields(name: str) -> dict:
    from kiro_crew.apps import app_agent_tools

    return {f.name: f for f in app_agent_tools.build_tool_schema(shipped(name)).fields}


class TestToolRegistration(unittest.TestCase):
    def test_both_tools_are_advertised(self):
        with listed():
            names = {t["name"] for t in mcp_core._list_tools()}
        assert READ_TOOL in names
        assert RECORD_TOOL in names

    def test_read_takes_no_arguments_at_all(self):
        # Identity is resolved from the session; if the model could name a crew
        # it could read, then write against, another crew's ledger.
        spec = _listed_spec(READ_TOOL)
        assert spec["inputSchema"]["properties"] == {}
        assert _validated_fields(READ_TOOL) == {}

    def test_record_requires_nothing_unconditionally(self):
        # Requiring `number` would leave a crew that swept an empty
        # queue no way to record the cycle without inventing an issue number. The
        # coupling that replaced the requirement (a missing number is valid only
        # with `sweep`, and `sweep` only without one) is a relation between two
        # fields, which the schema cannot express — it lives on the write route
        # and in the store, so the declaration requires nothing.
        assert {n for n, f in _validated_fields(RECORD_TOOL).items() if f.required} == set()
        assert _listed_spec(RECORD_TOOL)["inputSchema"]["required"] == []

    def test_record_advertises_the_crew_level_sweep_kind(self):
        # Advertised, or the model cannot discover the one kind that lets it
        # report a cycle it did no work in.
        spec = _listed_spec(RECORD_TOOL)
        assert "sweep" in spec["inputSchema"]["properties"]["event_kind"]["enum"]

    def test_record_advertises_no_identity_arguments(self):
        props = _listed_spec(RECORD_TOOL)["inputSchema"]["properties"]
        for forbidden in ("owner", "repo", "crew_id", "id"):
            assert forbidden not in props

    def test_advertised_schema_matches_the_validated_schema(self):
        # A property the model is told about but the validator rejects is a
        # guaranteed error mid-turn; the reverse is an undiscoverable field.
        spec = _listed_spec(RECORD_TOOL)
        assert set(spec["inputSchema"]["properties"]) == set(_validated_fields(RECORD_TOOL))

    def test_advertised_enums_are_the_validated_enums(self):
        props = _listed_spec(RECORD_TOOL)["inputSchema"]["properties"]
        by_name = _validated_fields(RECORD_TOOL)
        assert set(props["phase"]["enum"]) == set(by_name["phase"].allowed)
        assert set(props["event_kind"]["enum"]) == set(by_name["event_kind"].allowed)

    def test_descriptions_warn_that_the_progress_line_is_public(self):
        spec = descriptor(RECORD_TOOL)
        assert "PUBLIC" in spec["description"]
        assert "403" in spec["description"]

    def test_the_read_description_tells_the_crew_the_skip_list_is_shared(self):
        # The index is only worth having if a crew CONSULTS it before
        # investigating. Nothing enforces that, so the tool description is the
        # only place the instruction can live.
        spec = descriptor(READ_TOOL)
        assert "skipped_numbers" in spec["description"]
        assert "SHARED" in spec["description"]
        assert "BEFORE you investigate" in spec["description"]

    def test_the_record_description_names_skip_scope(self):
        spec = descriptor(RECORD_TOOL)
        assert "skip_scope" in spec["description"]
        assert "skip_scope" in spec["inputSchema"]["properties"]

    def test_the_skip_scope_vocabulary_is_named_and_mirrors_the_store(self):
        # Named so the model picks a real one, but NOT enforced: a declared enum is
        # one the core enforces, and refusing an odd label would fail the write
        # that indexes the skip, and an unindexed skip is the waste the index
        # exists to remove. The store coerces to ``other`` instead.
        from kiro_crew.apps.builtins.issue_radar.backend import crew_store

        prop = descriptor(RECORD_TOOL)["inputSchema"]["properties"]["skip_scope"]
        assert "enum" not in prop
        named = prop["description"].rsplit("One of: ", 1)[1].split(", ")
        assert set(named) == set(crew_store.SKIP_SCOPES)
        assert _validated_fields(RECORD_TOOL)["skip_scope"].allowed is None

    def test_the_phase_vocabulary_mirrors_the_store_exactly(self):
        # The manifest is data and cannot import the store, so it spells these.
        # Drift would silently reject a legitimate phase at the tool boundary.
        from kiro_crew.apps.builtins.issue_radar.backend import crew_store

        by_name = _validated_fields(RECORD_TOOL)
        assert set(by_name["phase"].allowed) == set(crew_store.PHASES)
        assert set(by_name["event_kind"].allowed) == set(crew_store.EVENT_KINDS)


class TestArgValidation(unittest.TestCase):
    def test_rejects_an_unknown_arg(self):
        # Fail closed on a hallucinated field rather than dropping it silently:
        # a crew that thinks it recorded `blocked_on` has recorded nothing.
        with pytest.raises(ValidationError):
            _clean(blocked_on="review")

    def test_rejects_identity_args(self):
        # Not merely absent from the schema — actively refused, so an attempt to
        # aim a write at another repo or crew errors instead of being ignored.
        for arg, value in (("owner", "other"), ("repo", "other"), ("crew_id", "c_dead")):
            with pytest.raises(ValidationError):
                _clean(**{arg: value})

    def test_read_rejects_any_arg(self):
        with pytest.raises(ValidationError):
            clean(READ_TOOL, {"crew_id": "c_7f3a"})

    def test_rejects_an_unknown_phase(self):
        with pytest.raises(ValidationError):
            _clean(phase="thinking", event="x", event_kind="ci")

    def test_rejects_an_unknown_event_kind(self):
        with pytest.raises(ValidationError):
            _clean(event="did a thing", event_kind="musing")

    def test_a_phase_write_must_carry_its_reason(self):
        # The point of merging upsert+append into one tool: two tools are what
        # let a phase move with nothing logged. Refused before either leg is reached.
        captured, out = _record(phase="implementing")
        assert out.startswith("Error: event: required when 'phase' changes"), out
        assert "reads" not in captured and "body" not in captured

    def test_event_and_kind_travel_together(self):
        captured, out = _record(event="CI round 3 — 41/47 green")
        assert out == "Error: event_kind: required when 'event' is given"
        assert "body" not in captured
        captured, out = _record(event_kind="ci")
        assert out == "Error: event: required when 'event_kind' is given"
        assert "body" not in captured

    def test_a_complete_progress_step_validates(self):
        cleaned = _clean(phase="awaiting-ci", event="pushed round 3", event_kind="ci")
        assert cleaned["phase"] == "awaiting-ci"
        assert cleaned["event_kind"] == "ci"

    def test_rejects_a_number_that_would_blow_up_the_filename(self):
        # The number becomes crews/<crew_id>/<n>.json.
        with pytest.raises(ValidationError):
            _clean(number=10**12)

    def test_rejects_a_boolean_number(self):
        # bool is an int subclass; True would otherwise record against #1.
        with pytest.raises(ValidationError):
            _clean(number=True)

    def test_rejects_a_base_sha_that_is_not_a_git_object_name(self):
        captured, out = _record(base_sha="origin/main")
        assert out == "Error: base_sha: invalid format"
        assert "body" not in captured

    def test_accepts_an_abbreviated_sha(self):
        assert _clean(base_sha="f2aa4c8")["base_sha"] == "f2aa4c8"


class TestIdentityComesFromTheSession(unittest.TestCase):
    def test_the_put_body_names_owner_repo_and_crew_explicitly(self):
        # Explicit identity is what stops a same-numbered issue in another repo
        # being the record that gets written.
        captured, _ = _record()
        assert captured["reads"] == 1
        assert captured["body"]["owner"] == "acme"
        assert captured["body"]["repo"] == "widget"
        assert captured["body"]["crew_id"] == "c_7f3a"
        assert captured["body"]["number"] == 12

    def test_identity_is_read_from_a_work_item_when_the_crew_record_omits_it(self):
        payload = {
            "crew": {"id": "c_7f3a", "name": "Whirlpool"},
            "items": [{"crew_id": "c_7f3a", "owner": "acme", "repo": "widget", "number": 12}],
        }
        captured, _ = _record(payload=payload)
        assert (captured["body"]["owner"], captured["body"]["repo"]) == ("acme", "widget")

    def test_a_subagent_resolved_identity_cannot_read_or_write_the_crew(self):
        """A subagent must not inherit its parent crew's authority.

        The lenient resolver's /proc ancestor walk maps a subagent to its PARENT slot,
        so both tools must refuse BEFORE the call is forwarded, or the request already
        leaks the crew's identity, worktree path and claim state.
        """
        for tool, args in ((READ_TOOL, {}), (RECORD_TOOL, _clean())):
            with forwarded(session_key="", read=CREW_PAYLOAD) as captured:
                out = call(tool, args)

            assert out.startswith("Error:"), (tool, out)
            assert "directly-identified" in out, (tool, out)
            assert "post" not in captured, tool

    def test_a_non_crew_session_is_refused_before_any_write(self):
        captured, out = _record(payload={"crew": {}, "items": []})
        assert out.startswith("Error:")
        assert "not bound to an Issue Radar crew" in out
        assert "body" not in captured

    def test_a_read_error_is_surfaced_instead_of_writing_blind(self):
        captured, out = _record(payload={"error": "not connected"})
        assert out.startswith("Error:")
        assert "body" not in captured

    def test_a_write_error_is_surfaced_instead_of_claiming_success(self):
        _, out = _record(next="rebase onto main", put_result={"error": "unknown crew"})
        assert out.startswith("Error:")
        assert "unknown crew" in out


class TestAnAutoNudgeTurnResolvesTheSameIdentityAsADirectTurn:
    """The positive twin of ``test_a_subagent_resolved_identity_cannot_read_or_write_the_crew``.

    An auto-nudge turn is NOT a subagent: it is the crew's own slot re-entered on a
    timer, so it resolves the SAME identity as a direct turn and passes the strict gate.
    Both go through ``_run_chat``, which hands ``effective_session_key(slot)`` to
    ``messaging.identity.publish_turn_identity``, the mapping the gate reads back.
    """

    @staticmethod
    def _fire_env():
        """The dashboard fire harness, mirrored from test_autonudge_dashboard_fire.

        Copied rather than imported so a change to either harness breaks only its file.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.autonudge import NudgeLoop
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.slack import gateway as gw

        slot_key = "crew-c_7f3a"
        loop = NudgeLoop(
            id="loop-crew",
            slot_key=slot_key,
            message="sweep the queue",
            idle_secs=300,
            max_cycles=24,
            cycle_count=2,
        )
        slot = MagicMock()
        slot.key = slot_key
        slot.running = False
        slot._closing = False
        slot.mode = ""
        slot.memory_mode = "persistent"
        # A bare crew slot has no linked_session_key; a truthy MagicMock attribute
        # would make it look channel-linked.
        slot.linked_session_key = ""

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
            orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
        orch.dashboard_state = SimpleNamespace(
            get_slot=MagicMock(return_value=slot),
            push_slots_update=MagicMock(),
            _background_tasks=set(),
            run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
            sessions=MagicMock(),
        )
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.remove = AsyncMock()
        orch._session_tasks = {}
        return orch, loop, slot

    @pytest.mark.asyncio
    async def test_the_nudge_turn_publishes_the_slots_own_identity(self):
        """A nudge fire drives _run_chat on the loop's own slot -> that identity.

        The expected value is ``effective_session_key(slot)``, not a literal, so the
        test pins nudge == direct rather than the key's spelling.
        """
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.slack import gateway as gw

        orch, loop, slot = self._fire_env()
        expected = effective_session_key(slot)
        published: list[str] = []

        async def _fake_run_chat(state, target_slot, message, **kwargs):
            # The real _run_chat hands effective_session_key(slot) to
            # publish_turn_identity (chat_turn/turn_admission.py).
            published.append(effective_session_key(target_slot))

        def _spawn(_state, _slot, coro, **kwargs):
            # Run the runner coroutine to completion so the publish happens, the
            # same way the neighbour harness drives a nudge turn.
            import asyncio

            return asyncio.ensure_future(coro)

        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=_fake_run_chat),
        ):
            result = await orch._fire_dashboard_nudge(loop)
            # Drain the spawned turn so the publish recorded above has run.
            import asyncio

            await asyncio.sleep(0)
            for task in list(orch._session_tasks.values()):
                if asyncio.isfuture(task):
                    await task

        assert result is True, "the nudge did not dispatch a turn on the crew slot"
        assert published == [expected], (
            "the nudge turn published an identity other than the crew slot's own "
            f"-- got {published!r}, expected [{expected!r}]; an auto-nudge crew "
            "would then be refused by the strict gate exactly as #5905 reported"
        )

    def test_a_direct_turn_on_the_slot_would_publish_the_identical_key(self):
        """The control: the SAME slot, driven directly, resolves the same key.

        A direct turn's identity must be non-empty and slot-derived, not the empty
        string the strict gate refuses.
        """
        from kiro_crew.dashboard.chat_utils import effective_session_key

        _, _, slot = self._fire_env()
        direct_identity = effective_session_key(slot)
        assert direct_identity, (
            "a direct turn on the crew slot resolves to no identity at all; the "
            "strict gate would refuse it and every crew tool would fail closed"
        )
        assert slot.key in direct_identity, (
            "a direct turn on the crew slot no longer derives its identity from "
            "the slot's own key; the nudge/direct identity equality this feature "
            "relies on is broken at the source"
        )

    @pytest.mark.asyncio
    async def test_a_dashboard_turn_publishes_the_slots_own_identity(self):
        """The real ``_run_chat`` publishes ``effective_session_key(slot)``.

        It goes through the session manager's pid mapping; the bare slot key would
        name a session nothing resolves.
        """
        record = await run_turn(
            TurnScript(events=[_ANSWER, _LANDED]), slot=SlotSpec(key="crew-c_7f3a")
        )
        published = [call.args[0] for call in record.session_calls if call.name == "get_pid"]
        assert published == ["dashboard:crew-c_7f3a"]
        assert record.stop_reason == STOP_REASON_END_TURN

    def test_the_shared_writer_maps_the_key_it_is_handed(self):
        """The writer keys off the session key it is handed, not a re-resolve."""
        import inspect

        from kiro_crew.messaging import identity

        writer_src = inspect.getsource(identity.publish_turn_identity)
        assert "session_key" in inspect.signature(identity.publish_turn_identity).parameters
        assert "get_pid" in writer_src, (
            "the shared writer no longer maps the passed session key to its pid "
            "sidecar; the identity channel the strict gate reads would be gone"
        )


class TestEmptyFieldsAreDropped(unittest.TestCase):
    """A partial patch must preserve what an earlier write stored."""

    def test_a_minimal_call_sends_identity_and_nothing_else(self):
        captured, _ = _record()
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number"}

    def test_an_explicitly_empty_field_is_not_sent(self):
        # "" would still be a merge key on the way through; dropping keeps the
        # request honest about what this call is actually asserting.
        captured, _ = _record(next="", decision="", worktree="", branch="")
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number"}

    def test_only_the_supplied_fields_are_sent(self):
        captured, _ = _record(next="add the Windows branch to _safe_chmod")
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number", "next"}
        assert captured["body"]["next"] == "add the Windows branch to _safe_chmod"

    def test_a_skip_carries_its_scope_to_the_route(self):
        captured, _ = _record(
            phase="skipped",
            event="passing — needs a design call",
            event_kind="skip",
            why="the fix changes the on-disk shape",
            skip_scope="needs-design",
        )
        assert captured["body"]["phase"] == "skipped"
        assert captured["body"]["skip_scope"] == "needs-design"
        # `why` is what the shared index stores as the reason, so it must arrive.
        assert captured["body"]["why"] == "the fix changes the on-disk shape"

    def test_a_skip_without_a_scope_sends_none_and_lets_the_route_default_it(self):
        captured, _ = _record(phase="skipped", event="passing", event_kind="skip")
        assert "skip_scope" not in captured["body"]

    def test_a_rejected_approach_carries_its_reason(self):
        captured, _ = _record(
            tried_approach="patch os.fchmod directly",
            tried_rejected_because="Windows has no fchmod",
        )
        body = captured["body"]
        assert body["tried_approach"] == "patch os.fchmod directly"
        assert body["tried_rejected_because"] == "Windows has no fchmod"

    def test_a_reason_with_no_approach_is_not_sent_alone(self):
        # The store only appends a `tried` entry when it sees an approach; a
        # lone reason would be silently discarded there.
        captured, _ = _record(tried_rejected_because="Windows has no fchmod")
        assert "tried_rejected_because" not in captured["body"]

    def test_the_local_resume_fields_survive_verbatim(self):
        captured, _ = _record(
            worktree="/home/user/src/project",
            branch="fix/os-fchmod-windows",
            base_sha="f2aa4c8bb",
        )
        body = captured["body"]
        # NOT scrubbed: an absolute path is the point of this field, it stays
        # local, and a scrubbed one is a resume that cannot find its worktree.
        assert body["worktree"] == "/home/user/src/project"
        assert body["branch"] == "fix/os-fchmod-windows"
        assert body["base_sha"] == "f2aa4c8bb"


class TestACrewCanReportAnEmptyQueue(unittest.TestCase):
    """The numberless call — `event_kind: sweep` with no `number`.

    The absence travels as an ABSENCE: the route reads a missing key as "no issue",
    while a `0` would be rejected as a malformed issue number.
    """

    @staticmethod
    def _sweep(put_result: dict | None = None, **over):
        args = {"event": "checked 42 open issues, took none", "event_kind": "sweep"}
        args.update(over)
        cleaned = clean(RECORD_TOOL, args)

        def fake_work(body):
            return (
                put_result
                if put_result is not None
                else {
                    "item": None,
                    "skip": None,
                    "event": {"text": body.get("event") or ""},
                }
            )

        with forwarded(read=CREW_PAYLOAD, work=fake_work) as captured:
            out = call(RECORD_TOOL, cleaned)
        return captured, out

    def test_a_sweep_validates_without_a_number(self):
        cleaned = clean(RECORD_TOOL, {"event": "queue empty", "event_kind": "sweep"})
        assert "number" not in cleaned

    def test_the_number_key_is_omitted_from_the_request(self):
        captured, _ = self._sweep()
        # Omitted, not zero: the route reads a present-but-invalid number as a
        # 400, so sending 0 would fail the write rather than record the sweep.
        assert "number" not in captured["body"]
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "event", "event_kind"}

    def test_the_summary_names_the_crew_rather_than_a_bare_hash(self):
        _, out = self._sweep()
        assert "#" not in out.splitlines()[0]
        assert "no issue" in out
        assert "checked 42 open issues, took none" in out

    def test_a_numbered_call_still_sends_and_reports_its_number(self):
        # The guard is on presence, so the ordinary path must be untouched.
        captured, out = _record(number=12, event="took it", event_kind="claim")
        assert captured["body"]["number"] == 12
        assert "#12" in out


class TestCiStateAssembly(unittest.TestCase):
    """The flat ci_* args become the store's ``ci_state`` dict."""

    def test_assembles_every_ci_field(self):
        captured, _ = _record(
            phase="awaiting-ci",
            event="CI round 3 — 41/47 green, 6 inherited",
            event_kind="ci",
            ci_state="failure",
            ci_passed=41,
            ci_total=47,
            ci_round=3,
            ci_inherited_reds=6,
        )
        assert captured["body"]["ci_state"] == {
            "state": "failure",
            "passed": 41,
            "total": 47,
            "round": 3,
            "inherited_reds": 6,
        }

    def test_a_partial_ci_reading_sends_only_what_was_read(self):
        captured, _ = _record(ci_state="pending")
        assert captured["body"]["ci_state"] == {"state": "pending"}

    def test_no_ci_key_at_all_when_nothing_was_read(self):
        captured, _ = _record(next="waiting")
        assert "ci_state" not in captured["body"]

    def test_a_zero_reading_is_kept_not_dropped(self):
        # 0/47 green and 0 inherited reds are both real, load-bearing readings:
        # dropping them on falsiness would leave the previous round's numbers in
        # place and a crew would read them as still-green.
        captured, _ = _record(ci_passed=0, ci_total=47, ci_inherited_reds=0)
        assert captured["body"]["ci_state"] == {
            "passed": 0,
            "total": 47,
            "inherited_reds": 0,
        }

    def test_the_flat_ci_args_never_leak_through_as_themselves(self):
        captured, _ = _record(ci_state="success", ci_passed=47, ci_total=47)
        for leaked in ("ci_passed", "ci_total", "ci_round", "ci_inherited_reds"):
            assert leaked not in captured["body"]


class TestPublicStringsAreSanitizedOnTheWayIn(unittest.TestCase):
    """``event`` reaches a comment on the forge.

    The investigation tool redacts because its prose is re-rendered on a card;
    here the same prose is published, so credentials AND this machine's identity
    must both be gone before the write, not before the render.
    """

    def test_a_credential_quoted_into_a_progress_line_is_not_published(self):
        captured, _ = _record(
            event="log had AKIAIOSFODNN7EXAMPLE in it",
            event_kind="investigate",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["event"]

    def test_a_credential_in_the_pass_reason_is_not_stored(self):
        """``why`` is what the shared skip index keeps as the reason.

        Every crew in the repository reads it. It stays on this machine, so it takes
        ``redact`` rather than the public sanitizer, but credentials still go.
        """
        captured, _ = _record(
            phase="skipped",
            skip_scope="needs-decision",
            why="the failing config had AKIAIOSFODNN7EXAMPLE hardcoded",
            event="passed: needs a decision on the credential",
            event_kind="skip",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["why"]

    def test_the_home_directory_is_scrubbed_from_a_progress_line(self):
        # Redaction alone covers credentials and exfil URLs, NOT paths — and a
        # path is the thing this line must not publish.
        from pathlib import Path

        home = str(Path.home())
        captured, _ = _record(
            event=f"ran the gate in {home}/src/project",
            event_kind="implement",
        )
        assert home not in captured["body"]["event"]
        assert "<home>" in captured["body"]["event"]

    def test_the_kirocrew_home_collapses_before_the_user_home(self):
        # Longest-first ordering: scrubbing the home first would leave
        # "<home>/.kiro/crew/..." — still this machine's directory layout.
        from kiro_crew.config.loader import config_dir

        captured, _ = _record(
            event=f"read the ledger under {config_dir()}/workspace",
            event_kind="investigate",
        )
        assert str(config_dir()) not in captured["body"]["event"]

    def test_the_hostname_is_scrubbed_from_a_progress_line(self):
        with patch.object(agent_tools.socket, "gethostname", return_value="dev-dsk-example-1a"):
            captured, _ = _record(
                event="the build only fails on dev-dsk-example-1a",
                event_kind="ci",
            )
        assert "dev-dsk-example-1a" not in captured["body"]["event"]
        assert "<host>" in captured["body"]["event"]

    def test_a_short_hostname_is_left_alone(self):
        # Substring-replacing a short hostname corrupts ordinary prose, which is
        # a worse outcome than a name the brief already forbids writing.
        with patch.object(agent_tools.socket, "gethostname", return_value="dev"):
            captured, _ = _record(event="dev server restarted", event_kind="implement")
        assert captured["body"]["event"] == "dev server restarted"

    def test_ordinary_prose_is_left_alone(self):
        captured, _ = _record(
            phase="awaiting-ci",
            event="CI round 3 — 41/47 green, 6 inherited from main",
            event_kind="ci",
            next="rerun the flaky Windows shard",
        )
        body = captured["body"]
        assert body["event"] == "CI round 3 — 41/47 green, 6 inherited from main"
        assert body["next"] == "rerun the flaky Windows shard"

    def test_the_local_worktree_path_is_not_scrubbed(self):
        # The pass that removes the home from a public line must not touch the
        # field whose whole purpose is to carry it.
        from pathlib import Path

        worktree = f"{Path.home()}/src/project"
        captured, _ = _record(worktree=worktree, event="checked out", event_kind="implement")
        assert captured["body"]["worktree"] == worktree

    def test_a_label_carrying_a_credential_is_not_persisted(self):
        captured, _ = _record(labels_applied=["crew: in progress", "AKIAIOSFODNN7EXAMPLE"])
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["labels_applied"][1]

    def test_removing_the_last_label_is_forwarded_as_an_empty_list(self):
        """`labels_applied: []` means "I now hold none", not "I did not say".

        The store reads absence as "leave as-is", so dropping `[]` would keep claiming
        labels other crews read to decide the issue is free.
        """
        captured, _ = _record(labels_applied=[])
        assert (
            "labels_applied" in captured["body"]
        ), "an emptied label set was dropped, so the store keeps the stale one"
        assert captured["body"]["labels_applied"] == []

    def test_omitting_labels_entirely_still_leaves_the_field_alone(self):
        """The other half of the distinction: silence must stay silence.

        Forwarding `[]` for a call that never mentioned labels would erase the
        crew's real labels on every unrelated progress update.
        """
        captured, _ = _record(event="pushed", event_kind="implement")
        assert "labels_applied" not in captured["body"]

    def test_a_clear_list_is_forwarded_by_name(self):
        """Every scalar field above is gated on truthiness, so a null can never
        reach the route through them; `clear` is the one way to empty a field and
        must arrive as the names that were sent, nothing dropped, nothing added."""
        captured, _ = _record(clear=["pr_number", "next"])
        assert captured["body"]["clear"] == ["pr_number", "next"]
        captured, _ = _record(event="pushed", event_kind="implement")
        assert "clear" not in captured["body"], "silence must stay silence"

    def test_the_clear_names_are_the_store_s_list(self):
        # Named in the argument's description; the route refuses any other name.
        from kiro_crew.apps.builtins.issue_radar.backend import crew_store

        prop = descriptor(RECORD_TOOL)["inputSchema"]["properties"]["clear"]
        named = prop["description"].rsplit("One of: ", 1)[1].split(", ")
        assert sorted(named) == sorted(crew_store.CLEARABLE_FIELDS)

    def test_the_verified_identity_is_the_one_sent_on_the_wire(self):
        """The gate's key must be the request's key — not a second resolution.

        A lenient re-resolve after the strict check is a check-then-use split that could
        attribute the read and WRITE to another crew. Both legs are asserted.
        """
        with patch.object(mcp_core, "_resolve_session_key", return_value="OTHER"):
            captured, _ = _record()
        assert captured["post"]["session_key"] == CREW_SESSION, "the post re-resolved"
        assert (
            captured["get_session_key"] == CREW_SESSION
        ), "the precondition read re-resolved its own identity"
        assert (
            captured["put_session_key"] == CREW_SESSION
        ), "the ledger write re-resolved its own identity"


class TestRecordOutput(unittest.TestCase):
    def test_a_skip_is_confirmed_back_with_the_scope_that_was_STORED(self):
        """The crew must see that the shared index took its pass.

        The STORED scope is echoed, not the argument: an unrecognised scope is filed
        as ``other``.
        """
        captured, out = _record(
            phase="skipped",
            skip_scope="architecture",
            # A phase write must carry its reason (schema invariant).
            event="passed: needs the retry loop moved, which changes three callers",
            event_kind="skip",
            put_result={
                "item": {"phase": "skipped"},
                # The store coerced the scope — the reply must show that.
                "skip": {"number": 12, "scope": "other"},
            },
        )
        assert "Shared skip index: #12" in out
        assert "`other`" in out
        assert "architecture" not in out.split("Shared skip index")[1]

    def test_no_skip_line_when_the_write_did_not_index_anything(self):
        _, out = _record(phase="implementing", event="starting the fix", event_kind="implement")
        assert "Shared skip index" not in out

    def test_echoes_the_stored_event_not_the_argument(self):
        # If a sanitizer pass changed the line, the crew must see what actually
        # became public — otherwise it believes it published something else.
        _, out = _record(
            event="pushed round 3",
            event_kind="ci",
            put_result={"item": {"phase": "awaiting-ci"}, "event": {"text": "stored line"}},
        )
        assert "stored line" in out
        assert "awaiting-ci" in out

    def test_reports_the_next_step_back_so_a_resume_can_be_trusted(self):
        _, out = _record(next="add the Windows branch to _safe_chmod")
        assert "add the Windows branch to _safe_chmod" in out


class TestReadOutput(unittest.TestCase):
    def _read(self, payload):
        with forwarded(read=payload):
            return call(READ_TOOL, {})

    def test_returns_the_crew_settings_and_open_items(self):
        out = json.loads(self._read(CREW_PAYLOAD))
        assert out["crew"]["name"] == "Whirlpool"
        assert out["settings"]["claim_ttl_hours"] == 48
        assert out["open_items"][0]["number"] == 12
        assert out["counts"]["open"] == 1

    def test_bounds_the_event_log_to_the_NEWEST_events_chronologically(self):
        """The window must contain the newest events, oldest-first inside it.

        The fixture is newest-first, as ``crew_store.read_events`` returns; an
        oldest-first fixture would let ``events[-N:]`` pass while keeping the OLDEST.
        """
        payload = dict(CREW_PAYLOAD)
        # Newest first, exactly as the route hands them over.
        payload["events"] = [{"text": f"line {i}"} for i in range(59, -1, -1)]
        out = json.loads(self._read(payload))
        window = out["recent_events"]

        assert len(window) == agent_tools._CREW_MAX_EVENTS
        # The newest event is present and last: the window is chronological.
        assert window[-1]["text"] == "line 59"
        assert window[0]["text"] == f"line {60 - agent_tools._CREW_MAX_EVENTS}"
        # And the oldest events are the ones dropped, not the newest.
        assert not any(e["text"] == "line 0" for e in window)
        assert "60" in out["recent_events_note"]

    def test_a_non_crew_session_is_told_so(self):
        out = self._read({"crew": {}, "items": []})
        assert out.startswith("Error:")

    def test_a_gateway_error_is_surfaced(self):
        out = self._read({"error": "not connected"})
        assert out.startswith("Error:")
        assert "not connected" in out

    def test_the_shared_skip_index_reaches_the_crew_intact(self):
        # The membership list must survive the projection COMPLETE: trimming it
        # here would answer "not skipped" for an issue that is, which is the
        # duplicated investigation the index removes.
        out = json.loads(self._read(CREW_PAYLOAD))
        assert out["skipped_numbers"] == [4, 91]
        assert out["recent_skips"][0]["scope"] == "needs-design"

    def test_an_older_payload_without_the_skip_fields_still_reads(self):
        # A gateway that predates the index answers without these keys; the
        # projection must degrade to "nothing known skipped", not KeyError.
        payload = {
            k: v for k, v in CREW_PAYLOAD.items() if k not in ("skipped_numbers", "recent_skips")
        }
        out = json.loads(self._read(payload))
        assert out["skipped_numbers"] == []
        assert out["recent_skips"] == []
        assert out["recent_answers"] == []

    def test_recent_answers_reach_the_crew_read(self):
        # The human-answered wake is dropped mid-turn; the read carries who answered.
        answers = [{"number": 4, "actor": "alice", "at": "2026-01-01T00:00:00.000000Z"}]
        out = json.loads(self._read({**CREW_PAYLOAD, "recent_answers": answers}))
        assert out["recent_answers"] == answers

    def test_the_worktree_path_survives_the_read(self):
        # The resume needs it; output redaction removes credentials, not paths.
        payload = dict(CREW_PAYLOAD)
        payload["items"] = [
            {
                "crew_id": "c_7f3a",
                "owner": "acme",
                "repo": "widget",
                "number": 12,
                "worktree": "/home/user/src/project",
            }
        ]
        out = json.loads(self._read(payload))
        assert out["open_items"][0]["worktree"] == "/home/user/src/project"


#: Item prose at the declared 5,000-character bound, in words the redactor leaves alone.
_PROSE = ("tried the obvious fix and it broke the windows branch " * 100)[:5000]


def _item(number: int, phase: str) -> dict:
    return {"crew_id": "c_7f3a", "number": number, "phase": phase, "why": _PROSE}


class TestReadFitsTheCap(unittest.TestCase):
    """A ledger bigger than the tool's cap still answers whole JSON with its skip index.

    The core cuts an answer at ``maxResultChars``; a cut through the JSON loses every
    field after the cut point, the skip index among them. These go through the core's
    cap (:func:`call`), so a read that relied on the cut would fail to parse here.
    """

    def _read(self, **over):
        with forwarded(read={**CREW_PAYLOAD, **over}):
            return call(READ_TOOL, {})

    def test_the_fitted_budget_is_under_the_declared_cap(self):
        assert shipped(READ_TOOL).decl.max_result_chars == agent_tools._CREW_READ_CAP
        assert agent_tools._CREW_READ_BUDGET < agent_tools._CREW_READ_CAP

    def test_finished_items_go_first_and_the_skip_index_stays_whole(self):
        # Newest progress first, as ``list_work_items`` returns: two unfinished items,
        # then forty finished ones that alone are over the cap.
        items = [_item(1, "implementing"), _item(2, "awaiting-ci")]
        items += [_item(100 + n, "resolved") for n in range(40)]
        skipped = list(range(1000, 4000))
        text = self._read(items=items, skipped_numbers=skipped)

        assert len(text) <= agent_tools._CREW_READ_CAP
        out = json.loads(text)
        assert out["skipped_numbers"] == skipped
        numbers = [it["number"] for it in out["open_items"]]
        assert numbers[:2] == [1, 2]
        # The newest finished items are kept and the oldest are the ones dropped.
        assert numbers[2:] == list(range(100, 100 + len(numbers) - 2))
        assert out["omitted"] == {"finished_items": 42 - len(numbers)}
        assert "crew page" in out["omitted_note"]

    def test_unfinished_items_go_last_and_oldest_first(self):
        # Only unfinished items, each about 46,000 characters with its tried rows: every
        # history list goes before any of them, and the newest item is the last to go.
        tried = [{"approach": _PROSE[:500], "rejected_because": _PROSE[:500]}] * 40
        items = [{**_item(n, "implementing"), "tried": tried} for n in (7, 8, 9)]
        events = [{"text": _PROSE[:500]} for _ in range(20)]
        text = self._read(items=items, events=events)

        assert len(text) <= agent_tools._CREW_READ_CAP
        out = json.loads(text)
        assert [it["number"] for it in out["open_items"]] == [7]
        assert out["omitted"] == {"recent_events": 20, "recent_skips": 1, "open_items": 2}
        assert out["recent_events"] == []
        assert out["skipped_numbers"] == [4, 91]

    def test_a_ledger_that_fits_is_answered_as_built(self):
        out = json.loads(self._read())
        assert "omitted" not in out
        assert list(out)[-1] == "open_items"

    def test_a_skip_index_over_the_cap_is_refused_not_cut(self):
        text = self._read(skipped_numbers=list(range(10**8, 10**8 + 7000)))
        assert text.startswith("Error:")
        assert "crew page" in text


class TestMiddlewareDecision:
    """Drive the real auth middleware over the crew paths.

    Set membership alone would still pass if the prefix matcher changed, so
    assert the decision: a credential-less call is refused, the internal secret
    reaches none of the app's routes, and the forge-write routes stay closed.
    """

    @staticmethod
    def _request(path: str, method: str, headers: dict | None = None, remote: str = "127.0.0.1"):
        req = MagicMock(spec=web.Request)
        req.path = path
        req.query = {}
        req.cookies = {}
        req.remote = remote
        req.headers = headers or {}
        req.method = method
        return req

    def _mw(self, secret: str = "s3cret"):
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        return token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
            internal_secret=secret,
        )

    @pytest.mark.asyncio
    async def test_a_credentialless_read_is_refused(self):
        resp = await self._mw()(self._request(READ_PATH, "GET"), _ok_handler)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_credentialless_write_is_refused(self):
        resp = await self._mw()(self._request(WORK_PATH, "PUT"), _ok_handler)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_secret_reaches_neither_crew_route_directly(self):
        # The tools reach these routes in-process from the app's ``agentTools`` route,
        # so the secret alone opens neither.
        for path, method in ((READ_PATH, "GET"), (WORK_PATH, "PUT")):
            resp = await self._mw()(
                self._request(path, method, headers={"X-Internal-Secret": "s3cret"}),
                _ok_handler,
            )
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_tool_forwarder_is_granted(self):
        resp = await self._mw()(
            self._request("/api/app-tools/call", "POST", headers={"X-Internal-Secret": "s3cret"}),
            _ok_handler,
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_loopback_with_the_secret_is_refused(self):
        # The secret is a machine-local handshake and a forwarder can make
        # remote traffic look loopback, so a genuinely remote peer never gets in.
        resp = await self._mw()(
            self._request(
                WORK_PATH, "PUT", headers={"X-Internal-Secret": "s3cret"}, remote="10.0.0.1"
            ),
            _ok_handler,
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_forge_write_routes_are_not_reachable_with_the_secret(self):
        for path, method in (
            ("/api/apps/issue-radar/labels/apply", "POST"),
            ("/api/apps/issue-radar/issue/close", "POST"),
            ("/api/apps/issue-radar/comment", "POST"),
        ):
            resp = await self._mw()(
                self._request(path, method, headers={"X-Internal-Secret": "s3cret"}),
                _ok_handler,
            )
            assert resp.status == 403

    @staticmethod
    def _real_request(path: str, method: str, headers: dict):
        """A genuine request object, for the chained middleware + handler test.

        On a ``MagicMock``, ``request.get("internal_auth")`` is truthy for EVERY request.
        The transport gives a loopback ``remote``; ``None`` is denied as non-loopback.
        """
        transport = MagicMock()
        transport.get_extra_info.side_effect = lambda name, default=None: (
            ("127.0.0.1", 51234) if name == "peername" else default
        )
        return make_mocked_request(method, path, headers=headers, transport=transport)

    @pytest.mark.asyncio
    async def test_the_crew_sibling_routes_refuse_an_agent_caller(self):
        """``/crew/pause`` and ``PUT``/``DELETE /crew`` refuse an agent caller.

        The middleware admits no internal-secret caller here at all; the handler's own
        gate is the one that tells methods apart, so it is driven with the mark the
        middleware sets on an agent call (``internal_auth``), with no middleware.
        ``/crew/guidance`` is absent, not closed; that is asserted separately.
        """
        from kiro_crew.apps.builtins.issue_radar.backend import crew_routes, routes

        app = web.Application()
        crew_routes.register_crew_routes(app)
        handlers = {
            (route.method, str(route.resource.canonical)): route.handler
            for route in app.router.routes()
        }
        closed = (
            ("/api/apps/issue-radar/crew/pause", "POST"),
            ("/api/apps/issue-radar/crew", "PUT"),
            ("/api/apps/issue-radar/crew", "DELETE"),
        )
        # is_app_enabled patched True so a 403 can only be the agent gate's — the
        # app-disabled gate answers 403 as well and would fake every assertion here.
        with patch.object(routes, "is_app_enabled", return_value=True):
            for path, method in closed:
                req = self._real_request(path, method, {})
                req["internal_auth"] = True
                resp = await handlers[(method, path)](req)
                assert resp.status == 403, f"{method} {path} was reachable"
                assert json.loads(resp.body)["code"] == "agent_route_denied"

            # Control: the read leg IS reachable, so the gate is discriminating by
            # method and path rather than refusing every agent call.
            req = self._real_request(READ_PATH, "GET", {})
            req["internal_auth"] = True
            resp = await handlers[("GET", READ_PATH)](req)
            assert json.loads(resp.body).get("code") != "agent_route_denied"

    @pytest.mark.asyncio
    async def test_the_guidance_route_no_longer_exists_at_all(self):
        """Not closed — absent: a crew never waits for a person to inject guidance.

        Checked by registration, since a route that exists and refuses also gives 403/404.
        """
        from kiro_crew.apps.builtins.issue_radar.backend import crew_routes

        app = web.Application()
        crew_routes.register_crew_routes(app)
        paths = {str(route.resource.canonical) for route in app.router.routes()}
        assert not any(p.endswith("/crew/guidance") for p in paths), sorted(paths)


# ── the app's scrubbing and identity helpers ──


class TestCrewMachineMarkers:
    def test_a_root_temp_dir_is_never_used_as_a_marker(self) -> None:
        # A marker of "/" would rewrite every slash in a public comment.
        with patch.object(agent_tools, "tempfile", SimpleNamespace(gettempdir=lambda: "/")):
            values = [value for value, _ in agent_tools._crew_machine_markers()]
        assert "/" not in values

    def test_markers_are_longest_first(self) -> None:
        markers = agent_tools._crew_machine_markers()
        lengths = [len(value) for value, _ in markers]
        assert lengths == sorted(lengths, reverse=True)

    def test_a_short_hostname_is_not_scrubbed(self) -> None:
        with patch.object(agent_tools.socket, "gethostname", return_value="dev"):
            assert "dev" not in [value for value, _ in agent_tools._crew_machine_markers()]

    def test_a_distinctive_hostname_is_scrubbed(self) -> None:
        with patch.object(agent_tools.socket, "gethostname", return_value="dev-dsk-example-2b"):
            assert ("dev-dsk-example-2b", "<host>") in agent_tools._crew_machine_markers()

    def test_an_unavailable_hostname_is_tolerated(self) -> None:
        with patch.object(agent_tools.socket, "gethostname", side_effect=OSError("no dns")):
            assert all(
                placeholder != "<host>" for _, placeholder in agent_tools._crew_machine_markers()
            )


class TestCrewPublicText:
    def test_a_windows_marker_is_scrubbed_in_both_slash_forms(self) -> None:
        markers = [("C:\\Users\\alice", "<home>")]
        with patch.object(agent_tools, "_crew_machine_markers", return_value=markers):
            with patch.object(agent_tools, "redact", lambda s: s):
                out = agent_tools._crew_public_text("saw C:\\Users\\alice and C:/Users/alice")
        assert out == "saw <home> and <home>"
        assert "alice" not in out

    def test_a_posix_marker_is_scrubbed_once(self) -> None:
        with patch.object(
            agent_tools, "_crew_machine_markers", return_value=[("/home/bob", "<home>")]
        ):
            with patch.object(agent_tools, "redact", lambda s: s):
                assert agent_tools._crew_public_text("cwd=/home/bob/x") == "cwd=<home>/x"


class TestCrewIdentity:
    def test_identity_is_taken_from_the_top_level_when_present(self) -> None:
        payload = {"owner": "o", "repo": "r", "crew": {"id": "c1"}}
        assert agent_tools._crew_identity(payload) == ("o", "r", "c1")

    def test_identity_falls_back_to_the_crew_record_then_a_work_item(self) -> None:
        assert agent_tools._crew_identity({"crew": {"owner": "o", "repo": "r", "id": "c2"}}) == (
            "o",
            "r",
            "c2",
        )
        payload = {
            "crew": {},
            "items": ["not-a-dict", {"owner": "o", "repo": "r", "crew_id": "c3"}],
        }
        assert agent_tools._crew_identity(payload) == ("o", "r", "c3")

    def test_missing_owner_repo_or_crew_id_yields_none(self) -> None:
        assert agent_tools._crew_identity({"crew": {"id": "c1"}}) is None
        # owner/repo present but no crew id anywhere: a write cannot be addressed.
        assert (
            agent_tools._crew_identity({"owner": "o", "repo": "r", "crew": {}, "items": []}) is None
        )
        # Malformed shapes must not raise.
        assert agent_tools._crew_identity({"crew": "nope", "items": "nope"}) is None
