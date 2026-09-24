"""The read-only ACP hooks surface: what Kiro Crew answers, and what it withholds.

Pins four things the surface is only correct if it keeps doing:

* the trigger spellings on the wire are the ACP seven, not the agent-profile
  aliases;
* a list answer is filtered by trigger and by tool identity through Crew's own
  matcher, and a disabled hook is withheld unless the request asked for it;
* the handshake does not announce the capability, so the backend never asks;
* the method that would spawn a command is not served at all.

No server is stood up: the store is driven directly against a tmp directory and
the handlers are called as functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.acp import kas_wire, session_handle
from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_HOOKS_LIST,
    JSONRPC_METHOD_NOT_FOUND,
    KAS_CLIENT_CAPABILITIES,
    JsonRpcMessage,
)
from kiro_crew.hooks import HOOK_EVENTS, ScriptHookStore, set_global_hook_store


class _FakeHandle:
    """The two attributes the answer path reads off its handle."""

    def __init__(self, session_id: str = "handle-session") -> None:
        self._session_id = session_id
        self._runtime = _CapturingRuntime()


class _CapturingRuntime:
    """The runtime surface the answer path reads: one method, one identity."""

    def __init__(self) -> None:
        self.responses: list[tuple[Any, dict]] = []
        self.acp_backend = ACP_BACKEND_KAS

    async def send_response(self, request_id: Any, result: dict) -> None:
        self.responses.append((request_id, result))


#: The method Crew must not serve. Spelled here rather than in the product, so
#: the product carries no constant for a method it refuses.
METHOD_HOOKS_EXECUTE = "_kiro/hooks/executeHook"


@pytest.fixture(autouse=True)
def _no_global_store():
    """Leave the process-wide store as the suite found it."""
    yield
    set_global_hook_store(None)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> ScriptHookStore:
    return ScriptHookStore(tmp_path)


def _hook(**over) -> kas_wire.NormalizedHook:
    fields: dict = {
        "id": kas_wire.wire_hook_id("abcd1234"),
        "name": "audit",
        "event": "PreToolUse",
        "command": "/bin/true",
    }
    fields.update(over)
    return kas_wire.NormalizedHook(**fields)


class TestTriggerSpellings:
    """The seven this channel uses, and the aliases it does not accept."""

    def test_the_seven_are_exactly_the_acp_set(self):
        assert kas_wire.ACP_HOOK_TRIGGERS == (
            "preToolUse",
            "postToolUse",
            "promptSubmit",
            "agentStop",
            "preTaskExecution",
            "postTaskExecution",
            "sessionStart",
        )

    @pytest.mark.parametrize(
        ("event", "trigger"),
        [
            ("AgentSpawn", "sessionStart"),
            ("UserPromptSubmit", "promptSubmit"),
            ("PreToolUse", "preToolUse"),
            ("PostToolUse", "postToolUse"),
            ("Stop", "agentStop"),
        ],
    )
    def test_crew_event_resolves_to_the_acp_spelling(self, event: str, trigger: str):
        assert _hook(event=event).acp_trigger == trigger

    def test_the_map_holds_exactly_the_stores_event_vocabulary(self):
        # The store refuses any other event on create and skips it on load, so a
        # key for another spelling could not be reached by a stored hook.
        #
        # Exact-set against the PARTITION, not a subset: a store event this
        # protocol cannot spell must be named in the exclusion set, so an event
        # that is merely forgotten appears in neither and still fails here.
        mapped = set(kas_wire._CREW_EVENT_TO_ACP_TRIGGER)
        unmappable = set(kas_wire._CREW_EVENTS_WITHOUT_ACP_TRIGGER)
        assert mapped & unmappable == set()
        assert mapped | unmappable == set(HOOK_EVENTS)

    def test_an_unmappable_store_event_is_withheld_from_this_surface(self):
        """The exclusion is the behaviour, not just bookkeeping."""
        for event in kas_wire._CREW_EVENTS_WITHOUT_ACP_TRIGGER:
            assert _hook(event=event).acp_trigger is None

    @pytest.mark.parametrize("trigger", ["preTaskExecution", "postTaskExecution"])
    def test_a_trigger_with_no_crew_event_answers_empty(self, trigger: str):
        # Accepted as a request, because the backend may ask for it; no Crew hook
        # can carry it, so the answer is an empty list and not an error.
        assert trigger in kas_wire.ACP_HOOK_TRIGGERS
        assert (
            kas_wire.select_hooks([_hook(event=event) for event in HOOK_EVENTS], trigger=trigger)
            == []
        )

    @pytest.mark.parametrize("event", ["userPromptSubmit", "stop", "preTaskExecution"])
    def test_an_event_outside_the_store_vocabulary_is_not_a_crew_event(self, event: str):
        assert _hook(event=event).acp_trigger is None

    @pytest.mark.parametrize("alias", ["userPromptSubmit", "stop", "agentSpawn"])
    def test_profile_alias_is_not_a_trigger_on_this_channel(self, alias: str):
        # The alias is a valid event to author a hook UNDER; it is not a value
        # the agent can ask for here, so a request naming it selects nothing.
        assert alias not in kas_wire.ACP_HOOK_TRIGGERS
        assert kas_wire.select_hooks([_hook(event="UserPromptSubmit")], trigger=alias) == []

    @pytest.mark.parametrize("event", ["fileCreated", "fileEdited", "fileDeleted", "userTriggered"])
    def test_a_trigger_absent_from_the_seven_serves_nothing(self, event: str):
        assert _hook(event=event).acp_trigger is None
        assert kas_wire.select_hooks([_hook(event=event)], trigger="preToolUse") == []


class TestSelection:
    def test_returns_only_the_requested_trigger(self):
        pre = _hook(id="a", event="PreToolUse")
        post = _hook(id="b", event="PostToolUse")
        assert kas_wire.select_hooks([pre, post], trigger="postToolUse") == [post]

    def test_tool_id_filters_through_crews_matcher(self):
        bash = _hook(id="a", matcher="Bash*")
        read = _hook(id="b", matcher="Read")
        star = _hook(id="c", matcher="*")
        unmatched = _hook(id="d", matcher="")
        pool = [bash, read, star, unmatched]
        selected = kas_wire.select_hooks(pool, trigger="preToolUse", tool_id="Bash")
        assert selected == [bash, star, unmatched]

    def test_a_tool_matcher_is_withheld_when_no_tool_is_named(self):
        # Returning it would let the hook run for a tool its author excluded.
        bash = _hook(matcher="Bash*")
        assert kas_wire.select_hooks([bash], trigger="preToolUse") == []

    def test_an_unmatched_hook_is_still_returned_when_no_tool_is_named(self):
        # No matcher means no restriction, so nothing is undecidable about it.
        every = _hook(matcher="")
        assert kas_wire.select_hooks([every], trigger="preToolUse") == [every]

    def test_a_context_matcher_is_withheld_on_a_non_tool_trigger(self):
        # promptSubmit carries no context, so this matcher cannot be decided here
        # at all -- and a tool id in the request is not its subject.
        hook = _hook(event="UserPromptSubmit", matcher="deploy*")
        assert kas_wire.select_hooks([hook], trigger="promptSubmit") == []
        assert kas_wire.select_hooks([hook], trigger="promptSubmit", tool_id="Read") == []

    def test_a_matcherless_hook_answers_a_non_tool_trigger(self):
        hook = _hook(event="UserPromptSubmit")
        assert kas_wire.select_hooks([hook], trigger="promptSubmit") == [hook]

    def test_disabled_is_excluded_by_default(self):
        on = _hook(id="a")
        off = _hook(id="b", enabled=False)
        assert kas_wire.select_hooks([on, off], trigger="preToolUse") == [on]

    def test_disabled_is_included_when_asked_for(self):
        on = _hook(id="a")
        off = _hook(id="b", enabled=False)
        selected = kas_wire.select_hooks([on, off], trigger="preToolUse", include_disabled=True)
        assert selected == [on, off]

    def test_an_unknown_trigger_selects_nothing(self):
        assert kas_wire.select_hooks([_hook()], trigger="somethingNewer") == []
        assert kas_wire.select_hooks([_hook()], trigger="") == []

    def test_a_truncated_answer_says_so(self, caplog):
        pool = [_hook(id=f"crew:script:{i}") for i in range(kas_wire.HOOKS_LIST_MAX + 3)]
        with caplog.at_level("WARNING", logger="kiro_crew.acp.kas_wire"):
            selected = kas_wire.select_hooks(pool, trigger="preToolUse")
        assert len(selected) == kas_wire.HOOKS_LIST_MAX
        assert "3 further hook(s) withheld" in caplog.text

    def test_the_answer_is_bounded(self):
        pool = [_hook(id=str(i)) for i in range(kas_wire.HOOKS_LIST_MAX + 25)]
        assert len(kas_wire.select_hooks(pool, trigger="preToolUse")) == kas_wire.HOOKS_LIST_MAX


class TestProjection:
    def test_the_wire_shape_is_id_name_action(self):
        assert kas_wire.project_hook(_hook(timeout=45)) == {
            "id": "crew:script:abcd1234",
            "name": "audit",
            "action": {"type": "runCommand", "command": "/bin/true", "timeout": 45},
        }

    def test_no_timeout_means_the_field_is_absent(self):
        assert "timeout" not in kas_wire.project_hook(_hook())["action"]

    def test_approved_is_never_sent(self):
        # Sending it would tell the agent the command already carries an
        # approval, which is the one claim this surface must not make.
        assert "approved" not in kas_wire.project_hook(_hook(timeout=30))


class TestNormalizeScriptHook:
    def test_a_stored_hook_becomes_a_wire_hook(self, store: ScriptHookStore):
        created = store.create(
            {
                "name": "audit",
                "event": "PreToolUse",
                "matcher": "Bash*",
                "command": "echo hi",
                "timeout": 20,
            }
        )
        normalized = kas_wire.normalize_script_hook(created)
        assert normalized is not None
        assert normalized.id == f"crew:script:{created.id}"
        assert normalized.name == "audit"
        assert normalized.event == "PreToolUse"
        assert normalized.command == "echo hi"
        assert normalized.matcher == "Bash*"
        assert normalized.timeout == 20
        assert normalized.enabled is True

    def test_a_commandless_hook_is_not_served(self, store: ScriptHookStore):
        created = store.create(
            {
                "name": "skills only",
                "event": "UserPromptSubmit",
                "command": "",
                "skills": ["kirocrew-dev/prepare-pr"],
            }
        )
        assert kas_wire.normalize_script_hook(created) is None

    def test_an_id_less_hook_is_not_served(self):
        class _Bare:
            id = ""
            name = "x"
            event = "PreToolUse"
            command = "echo hi"
            matcher = ""
            timeout = 30
            enabled = True

        assert kas_wire.normalize_script_hook(_Bare()) is None

    def test_an_unmapped_event_is_not_served(self):
        class _Filed:
            id = "abcd1234"
            name = "x"
            event = "fileCreated"  # real hook event, absent from the ACP seven
            command = "echo hi"
            matcher = ""
            timeout = 30
            enabled = True

        assert kas_wire.normalize_script_hook(_Filed()) is None

    def test_a_non_string_field_is_not_served(self):
        class _Wrong:
            id = 7
            name = "x"
            event = "PreToolUse"
            command = "echo hi"
            matcher = ""
            timeout = 30
            enabled = True

        assert kas_wire.normalize_script_hook(_Wrong()) is None

    @pytest.mark.parametrize("value", ["false", "", 0, 1, None, "true"])
    def test_enabled_must_be_the_literal_boolean(self, value):
        # The store persists this field uncoerced, so a hand-edited string must
        # not be read as "on" by truthiness.
        class _Persisted:
            id = "abcd1234"
            name = "x"
            event = "PreToolUse"
            command = "echo hi"
            matcher = ""
            timeout = 30
            enabled = value

        normalized = kas_wire.normalize_script_hook(_Persisted())
        assert normalized is not None
        assert normalized.enabled is False

    def test_an_oversized_command_is_withheld(self):
        class _Huge:
            id = "abcd1234"
            name = "x"
            event = "PreToolUse"
            command = "e" * (kas_wire.HOOK_COMMAND_MAX + 1)
            matcher = ""
            timeout = 30
            enabled = True

        assert kas_wire.normalize_script_hook(_Huge()) is None

    @pytest.mark.parametrize("matcher", [["Bash", "Read"], 7, {"tool": "Bash"}, object()])
    def test_a_matcher_that_cannot_be_read_is_withheld(self, matcher):
        # An empty matcher means NO restriction, so degrading an unreadable one
        # would widen the hook to every tool instead of withholding it. The store
        # persists this field uncoerced, so a hand-edited file can carry any shape.
        class _Persisted:
            id = "abcd1234"
            name = "x"
            event = "PreToolUse"
            command = "echo hi"
            timeout = 30
            enabled = True

        _Persisted.matcher = matcher
        assert kas_wire.normalize_script_hook(_Persisted()) is None

    def test_a_name_that_cannot_be_read_falls_back_to_the_id(self):
        # The opposite direction on purpose: a name restricts nothing, so falling
        # back cannot widen the hook.
        class _Persisted:
            id = "abcd1234"
            name = 7
            event = "PreToolUse"
            command = "echo hi"
            matcher = ""
            timeout = 30
            enabled = True

        normalized = kas_wire.normalize_script_hook(_Persisted())
        assert normalized is not None
        assert normalized.name == "abcd1234"

    def test_an_oversized_id_is_withheld(self):
        # The id is the one field the record retains, so a bounded map cannot
        # protect it by counting entries alone.
        class _Huge:
            id = "a" * (kas_wire.HOOK_ID_MAX + 1)
            name = "x"
            event = "PreToolUse"
            command = "echo hi"
            matcher = ""
            timeout = 30
            enabled = True

        assert kas_wire.normalize_script_hook(_Huge()) is None

    def test_an_oversized_name_is_withheld(self):
        class _Huge:
            id = "abcd1234"
            name = "n" * (kas_wire.HOOK_NAME_MAX + 1)
            event = "PreToolUse"
            command = "echo hi"
            matcher = ""
            timeout = 30
            enabled = True

        assert kas_wire.normalize_script_hook(_Huge()) is None

    def test_a_command_at_the_bound_is_served(self):
        class _AtBound:
            id = "abcd1234"
            name = "x"
            event = "PreToolUse"
            command = "e" * kas_wire.HOOK_COMMAND_MAX
            matcher = ""
            timeout = 30
            enabled = True

        normalized = kas_wire.normalize_script_hook(_AtBound())
        assert normalized is not None
        assert len(normalized.command) == kas_wire.HOOK_COMMAND_MAX

    def test_a_non_int_timeout_degrades_to_absent(self):
        class _Odd:
            id = "abcd1234"
            name = "x"
            event = "PreToolUse"
            command = "echo hi"
            matcher = ""
            timeout = True  # a bool is an int subclass and is not a duration
            enabled = True

        normalized = kas_wire.normalize_script_hook(_Odd())
        assert normalized is not None
        assert normalized.timeout is None

    def test_the_store_is_read_through_the_global_accessor(self, store: ScriptHookStore):
        store.create({"name": "audit", "event": "PreToolUse", "command": "echo hi"})
        assert kas_wire.crew_hooks() == []  # no global store registered yet
        set_global_hook_store(store)
        assert [h.name for h in kas_wire.crew_hooks()] == ["audit"]


class TestListResponse:
    def test_lists_crews_hooks_for_a_trigger_filtered_by_tool_id(self, store: ScriptHookStore):
        bash = store.create(
            {"name": "on bash", "event": "PreToolUse", "matcher": "Bash*", "command": "echo a"}
        )
        store.create(
            {"name": "on read", "event": "PreToolUse", "matcher": "Read", "command": "echo b"}
        )
        store.create({"name": "on stop", "event": "Stop", "command": "echo c"})
        set_global_hook_store(store)

        result = kas_wire.hooks_list_response(
            {"trigger": "preToolUse", "sessionId": "s1", "toolId": "Bash"}
        )
        assert [h["name"] for h in result["hooks"]] == ["on bash"]
        assert result["hooks"][0]["id"] == f"crew:script:{bash.id}"

    def test_disabled_excluded_by_default_and_included_on_request(self, store: ScriptHookStore):
        store.create({"name": "on", "event": "PreToolUse", "command": "echo a"})
        off = store.create({"name": "off", "event": "PreToolUse", "command": "echo b"})
        store.toggle(off.id)
        set_global_hook_store(store)

        default = kas_wire.hooks_list_response({"trigger": "preToolUse", "sessionId": "s1"})
        assert [h["name"] for h in default["hooks"]] == ["on"]

        widened = kas_wire.hooks_list_response(
            {"trigger": "preToolUse", "sessionId": "s1", "includeDisabled": True}
        )
        assert [h["name"] for h in widened["hooks"]] == ["on", "off"]

    def test_include_disabled_must_be_the_boolean(self, store: ScriptHookStore):
        off = store.create({"name": "off", "event": "PreToolUse", "command": "echo b"})
        store.toggle(off.id)
        set_global_hook_store(store)
        for value in ["true", 1, {}, None]:
            result = kas_wire.hooks_list_response(
                {"trigger": "preToolUse", "sessionId": "s1", "includeDisabled": value}
            )
            assert result["hooks"] == []

    def test_host_supplied_shapes_degrade_to_absent(self, store: ScriptHookStore):
        # Every field is host data. A wrong shape answers empty rather than
        # raising inside the turn's dispatch.
        created = store.create({"name": "audit", "event": "PreToolUse", "command": "echo hi"})
        set_global_hook_store(store)

        assert kas_wire.hooks_list_response({}) == {"hooks": []}
        assert kas_wire.hooks_list_response({"trigger": 7}) == {"hooks": []}
        listed = kas_wire.hooks_list_response(
            {"trigger": "preToolUse", "sessionId": None, "toolId": []}
        )
        assert [h["id"] for h in listed["hooks"]] == [f"crew:script:{created.id}"]

    def test_tool_tags_and_workspace_paths_do_not_narrow(self, store: ScriptHookStore):
        created = store.create(
            {"name": "on bash", "event": "PreToolUse", "matcher": "Bash*", "command": "echo a"}
        )
        set_global_hook_store(store)
        result = kas_wire.hooks_list_response(
            {
                "trigger": "preToolUse",
                "sessionId": "s1",
                "toolId": "Bash",
                "toolTags": ["destructive"],
                "workspacePaths": ["/nowhere"],
            }
        )
        assert [h["id"] for h in result["hooks"]] == [f"crew:script:{created.id}"]

    @pytest.mark.parametrize("params", [None, "nope", 7, []])
    def test_a_params_object_of_the_wrong_shape_is_answered_empty(self, params):
        # Guarded at the sink, not only at the route: a raise here would leave the
        # request unanswered inside the dispatch loop.
        assert kas_wire.hooks_list_response(params) == {"hooks": []}
        assert kas_wire.hooks_session_start_response(params) == {"results": []}

    def test_no_store_answers_empty_rather_than_raising(self):
        assert kas_wire.hooks_list_response({"trigger": "preToolUse", "sessionId": "s1"}) == {
            "hooks": []
        }


class TestSessionStartResponse:
    def test_the_buffer_is_empty(self):
        assert kas_wire.hooks_session_start_response({"trigger": "sessionStart"}) == {"results": []}

    def test_every_trigger_answers_the_same_way(self):
        assert kas_wire.hooks_session_start_response({"trigger": "preToolUse"}) == {"results": []}
        assert kas_wire.hooks_session_start_response({}) == {"results": []}


class TestTheCapabilityIsNotAnnounced:
    """The surface exists and the backend is not told, which is the whole gate.

    A hooks provider is chosen at handshake: announcing the capability is what
    makes the backend route hook extraction to its client. Until an execute path
    exists, announcing it would hand the agent a surface whose third method Crew
    answers with an error.
    """

    def test_the_handshake_meta_does_not_carry_hooks(self):
        kiro_meta = KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]
        assert "hooks" not in kiro_meta

    def test_the_harness_declares_the_same_meta(self):
        kiro_meta = KasHarness().client_capabilities["_meta"]["kiro"]
        assert "hooks" not in kiro_meta


class TestExecuteHookIsNotServed:
    def test_it_classifies_as_an_unknown_server_request(self):
        msg = JsonRpcMessage(
            id=11,
            method=METHOD_HOOKS_EXECUTE,
            params={"hookId": "crew:script:abcd1234", "command": "rm -rf /", "sessionId": "s1"},
        )
        assert classify_notification(msg) == "server_request_unknown"

    def test_the_unknown_answer_is_method_not_found(self):
        assert JSONRPC_METHOD_NOT_FOUND == -32601

    def test_the_product_names_no_execute_method(self):
        # A constant for it would read as a promise to serve it.
        named = [value for value in vars(kas_wire).values() if isinstance(value, str)]
        assert METHOD_HOOKS_EXECUTE not in named

    def test_the_two_read_only_methods_are_named(self):
        assert kas_wire.METHOD_HOOKS_LIST == "_kiro/hooks/list"
        assert kas_wire.METHOD_HOOKS_SESSION_START == "_kiro/hooks/sessionStart"


class TestTheRoute:
    """The two requests reach the builders; the third one does not exist here."""

    def test_only_the_kas_backend_is_served(self):
        # The loop is shared by every backend the runtime demuxes, and only one of
        # them defines this channel.
        assert ACP_BACKENDS_HOOKS_LIST == {ACP_BACKEND_KAS}
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_HOOKS_LIST

    @pytest.mark.parametrize(
        ("backend", "method", "request_id", "served"),
        [
            (ACP_BACKEND_KAS, "_kiro/hooks/list", 1, True),
            (ACP_BACKEND_KAS, "_kiro/hooks/sessionStart", 1, True),
            (ACP_BACKEND_KIRO, "_kiro/hooks/list", 1, False),
            (ACP_BACKEND_KIRO, "_kiro/hooks/sessionStart", 1, False),
            (ACP_BACKEND_KAS, METHOD_HOOKS_EXECUTE, 1, False),
            (ACP_BACKEND_KAS, "session/update", 1, False),
            (ACP_BACKEND_KAS, "_kiro/hooks/list", None, False),
        ],
    )
    def test_the_route_predicate_decides_per_backend_and_method(
        self, backend: str, method: str, request_id, served: bool
    ):
        # Drives the predicate rather than asserting the set's contents: deleting the
        # membership clause keeps a set-contents assertion green, and the clause is
        # what keeps hook commands away from a backend that never defined the
        # channel.
        handle = _FakeHandle("s1")
        handle._runtime.acp_backend = backend
        msg = JsonRpcMessage(id=request_id, method=method, params={})

        assert AcpSessionHandle._is_kas_hooks_request(handle, msg) is served

    @pytest.mark.parametrize(
        "method",
        [
            ["_kiro/hooks/list"],
            {"method": "_kiro/hooks/list"},
            {"_kiro/hooks/list"},
            17,
            None,
        ],
    )
    def test_a_method_that_is_not_a_string_is_not_served_and_does_not_raise(self, method):
        # The peer decides what lands in `method`, and an unhashable JSON value there
        # makes a bare membership test raise inside the dispatch loop, which would take
        # the whole session down rather than refuse one frame.
        handle = _FakeHandle("s1")
        handle._runtime.acp_backend = ACP_BACKEND_KAS
        msg = JsonRpcMessage(id=1, method=method, params={})

        assert AcpSessionHandle._is_kas_hooks_request(handle, msg) is False

    def test_only_the_read_only_methods_are_routed(self):
        assert session_handle._KAS_HOOKS_METHODS == {
            "_kiro/hooks/list",
            "_kiro/hooks/sessionStart",
        }
        assert METHOD_HOOKS_EXECUTE not in session_handle._KAS_HOOKS_METHODS

    @pytest.mark.asyncio
    async def test_a_list_request_is_answered_with_crews_hooks(self, store: ScriptHookStore):
        created = store.create({"name": "audit", "event": "PreToolUse", "command": "echo hi"})
        set_global_hook_store(store)
        handle = _FakeHandle("route-1")
        runtime = handle._runtime
        msg = JsonRpcMessage(
            id=5,
            method="_kiro/hooks/list",
            params={"trigger": "preToolUse", "sessionId": "a-different-session"},
        )

        await AcpSessionHandle._answer_kas_hooks_request(handle, msg)

        assert runtime.responses == [
            (
                5,
                {
                    "hooks": [
                        {
                            "id": f"crew:script:{created.id}",
                            "name": "audit",
                            "action": {"type": "runCommand", "command": "echo hi", "timeout": 30},
                        }
                    ]
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_a_session_start_request_is_answered_with_an_empty_buffer(self):
        handle = _FakeHandle("route-2")
        runtime = handle._runtime
        msg = JsonRpcMessage(
            id=6,
            method="_kiro/hooks/sessionStart",
            params={"trigger": "sessionStart", "sessionId": "route-2"},
        )

        await AcpSessionHandle._answer_kas_hooks_request(handle, msg)

        assert runtime.responses == [(6, {"results": []})]

    @pytest.mark.asyncio
    async def test_a_malformed_params_object_is_still_answered(self):
        handle = _FakeHandle("route-3")
        runtime = handle._runtime
        msg = JsonRpcMessage(id=7, method="_kiro/hooks/list", params="not an object")

        await AcpSessionHandle._answer_kas_hooks_request(handle, msg)

        assert runtime.responses == [(7, {"hooks": []})]
