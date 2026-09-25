"""The ENFORCED solo-spawn gate: one sub-agent for one task has to say why.

``test_spawn_single_task_gate`` pins the textual gate in the tool
descriptions. This module pins the mechanical one behind it -- the handshake
the text could only ask for:

* tool side (``spawn_run`` / ``spawn_sub_agents``): one task, no
  ``solo_reason``, nothing named that could differ from the caller -> refused
  with the question, nothing POSTed;
* the wire: a one-task call carries ``solo`` (and its reason) to the gateway,
  a batch never does, so the SDK and apps are never gated;
* gateway side (``api_spawn``): a solo call that named the parent's OWN
  agent / model / crew to get past the tool is refused there, fail-open when
  a parent fact is unknown.
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.solo_spawn import (
    SOLO_SPAWN_REASONS,
    SOLO_SPAWN_REFUSED_CODE,
    solo_spawn_difference,
    solo_spawn_note,
    solo_spawn_question,
    solo_spawn_refusal,
)
from kiro_crew.validation import (
    SPAWN_RUN_SCHEMA,
    SPAWN_SUB_AGENTS_SCHEMA,
    ValidationError,
    validate_tool_args,
)

# ── the predicate ────────────────────────────────────────────────────────────


class TestRefusalPredicate:
    def test_one_task_with_nothing_is_refused(self):
        text = solo_spawn_refusal(1, "")
        assert text is not None
        assert text.startswith("Error:")
        # The feedback is a QUESTION first, then the two ways forward.
        assert "Can you do this task yourself" in text
        assert "solo_reason" in text
        assert "bulk_data" not in text
        assert "fresh_context" not in text
        assert "Nothing was spawned" in text

    @pytest.mark.parametrize("count", [0, 2, 3, 16])
    def test_any_other_count_is_not_a_solo_spawn(self, count: int):
        assert solo_spawn_refusal(count, "") is None

    @pytest.mark.parametrize("reason", sorted(r for r in SOLO_SPAWN_REASONS if r))
    def test_a_reason_opens_the_gate(self, reason: str):
        assert solo_spawn_refusal(1, reason) is None

    @pytest.mark.parametrize(
        "named",
        [{"model": "opus"}, {"agent": "kirocrew-worker"}, {"crew": "coder"}, {"backend": "kas"}],
    )
    def test_a_named_model_agent_or_crew_passes_the_tool_side(self, named: dict[str, str]):
        """The tool cannot tell a named value from the caller's own -- the
        gateway can (see TestRosterCheck) -- so it lets a named one through."""
        assert solo_spawn_refusal(1, "", **named) is None

    @pytest.mark.parametrize("sentinel", ["auto", "AUTO", " auto ", ""])
    def test_the_auto_sentinel_is_not_a_named_model(self, sentinel: str):
        """``model="auto"`` means "no model chosen": it must not open the gate,
        and the result line must not claim it as a ground."""
        assert solo_spawn_refusal(1, "", model=sentinel) is not None
        assert solo_spawn_refusal(1, "", model=sentinel, agent="kirocrew-worker") is None
        note = solo_spawn_note("")
        assert note == (
            "Solo spawn -- not refused: the gateway's roster check did not find "
            "everything it names to be your own; the ground is in the spawn.solo audit."
        )

    def test_sub_agents_wording_names_its_own_parameter(self):
        text = solo_spawn_question(tool="spawn_sub_agents")
        assert "agent_or_mode" in text
        assert "crew" not in text  # spawn_sub_agents has no crew/model parameter

    def test_note_names_the_grounds(self):
        assert solo_spawn_note("bulk_data") == "Solo spawn -- reason: bulk_data."
        note = solo_spawn_note("")
        assert note == (
            "Solo spawn -- not refused: the gateway's roster check did not find "
            "everything it names to be your own; the ground is in the spawn.solo audit."
        )
        assert "agent=" not in note and "model=" not in note


# ── schema + advertisement ───────────────────────────────────────────────────


def _tools() -> dict[str, dict]:
    from kiro_crew.mcp_tools import spawn as spawn_tools

    roster = [types.SimpleNamespace(name="kirocrew")]
    with patch.object(spawn_tools.mcp_core, "list_agents", return_value=roster):
        return {t["name"]: t for t in spawn_tools.schemas()}


class TestSchema:
    @pytest.mark.parametrize("schema", [SPAWN_RUN_SCHEMA, SPAWN_SUB_AGENTS_SCHEMA])
    def test_vocabulary_is_closed(self, schema):
        base = {"task": "x"} if schema is SPAWN_RUN_SCHEMA else {"agents": [{"prompt": "x"}]}
        for reason in SOLO_SPAWN_REASONS:
            assert (
                validate_tool_args({**base, "solo_reason": reason}, schema)["solo_reason"] == reason
            )
        # The reasons the base prompt hands out are exactly the ones NOT here.
        for bad in ("preserve_context", "investigation", "yes", "true"):
            with pytest.raises(ValidationError):
                validate_tool_args({**base, "solo_reason": bad}, schema)

    def test_empty_means_not_given(self):
        assert validate_tool_args({"task": "x"}, SPAWN_RUN_SCHEMA).get("solo_reason") in (None, "")

    @pytest.mark.parametrize("tool", ["spawn_run", "spawn_sub_agents"])
    def test_both_tools_advertise_the_enforced_gate(self, tool: str):
        t = _tools()[tool]
        prop = t["inputSchema"]["properties"]["solo_reason"]
        assert set(prop["enum"]) == {r for r in SOLO_SPAWN_REASONS if r}
        assert "REQUIRED" in prop["description"]
        # The description says the gate is enforced, not merely advised.
        assert "ENFORCED" in t["description"]
        # ...and still opens with the textual gate the sibling test pins.
        assert t["description"].startswith("Do focused work directly")


# ── tool side ────────────────────────────────────────────────────────────────


def _run(tool: str, args: dict[str, Any], answer: dict | None = None):
    """Run a spawn tool; return (POSTed bodies, result text, sel mock)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []
    sel = MagicMock()

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
            return dict(answer or {"id": "a1"})
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock(return_value=sel)),
    ):
        result = mcp_core._call_tool_inner(tool, args)
    return bodies, result, sel


class TestSpawnRunToolSide:
    @pytest.mark.parametrize("reason", ["parent_parallel", "specialist", "user_requested"])
    def test_new_reasons_need_concrete_details_before_any_post(self, reason):
        bodies, result, _ = _run("spawn_run", {"task": "x", "solo_reason": reason})
        assert not bodies
        assert result.startswith("Error:") and "solo_details" in result

    @pytest.mark.parametrize("supported", [True, False, None])
    def test_parent_parallel_receipt_controls_work_boundary(self, supported):
        bodies, result, _ = _run(
            "spawn_run",
            {
                "task": "Document the finalized API",
                "solo_reason": "parent_parallel",
                "solo_details": "I implement validation in backend.py; child owns api.md.",
            },
            {"id": "a1", "parent_work_supported": supported},
        )
        assert bodies[0]["solo_details"].startswith("I implement")
        assert bodies[0]["solo_reason"] == "parent_parallel"
        assert "END YOUR TURN" in result
        assert ("at most one minute" in result) is (supported is True)
        assert ("no confirmed parent-work" in result) is (supported is not True)

    def test_lone_task_is_refused_before_any_post(self):
        bodies, result, sel = _run("spawn_run", {"task": "read the log and fix it"})
        assert bodies == []
        assert result == solo_spawn_question()
        kw = sel.log_tool_invocation.call_args.kwargs
        assert kw["tool_name"] == "spawn_run" and kw["outcome"] == "refused"
        sel.log_api_access.assert_called_once_with(
            caller="internal",
            operation="spawn.solo",
            outcome="denied",
            source="solo_gate",
            resources="dashboard:chat-1",
            error="one task, no reason, nothing named",
        )

    def test_keep_alone_is_not_a_reason(self):
        """'I will come back to it later' does not say the parent cannot do it."""
        bodies, result, _ = _run("spawn_run", {"task": "x", "keep": True})
        assert bodies == []
        assert result == solo_spawn_question()

    def test_reason_opens_the_gate_and_travels(self):
        bodies, result, _ = _run("spawn_run", {"task": "x", "solo_reason": "bulk_data"})
        assert len(bodies) == 1
        assert bodies[0]["solo"] is True
        assert bodies[0]["solo_reason"] == "bulk_data"
        # The grounds are in the transcript, next to the spawned id.
        assert "Solo spawn -- reason: bulk_data." in result
        assert "a1" in result

    def test_named_model_passes_the_tool_and_is_marked_solo(self):
        bodies, result, _ = _run("spawn_run", {"task": "x", "model": "deepseek-3.2"})
        assert len(bodies) == 1
        assert bodies[0]["solo"] is True
        assert "solo_reason" not in bodies[0]
        assert (
            "Solo spawn -- not refused: the gateway's roster check did not find "
            "everything it names to be your own; the ground is in the spawn.solo audit."
        ) in result

    def test_model_auto_alone_is_refused_before_any_post(self):
        bodies, result, _ = _run("spawn_run", {"task": "x", "model": "auto"})
        assert bodies == []
        assert result.startswith("Error: solo spawn refused")

    def test_named_agent_via_agents_list_counts(self):
        bodies, _, _ = _run("spawn_run", {"tasks": ["x"], "agents": ["kirocrew-worker"]})
        assert len(bodies) == 1 and bodies[0]["solo"] is True

    def test_a_batch_is_never_solo(self):
        bodies, result, sel = _run("spawn_run", {"tasks": ["t1", "t2"]})
        assert len(bodies) == 2
        assert all("solo" not in b and "solo_reason" not in b for b in bodies)
        assert "Solo spawn" not in result
        sel.log_tool_invocation.assert_not_called()

    def test_gateway_roster_refusal_is_the_whole_result(self):
        """When the gateway's half refuses, the caller reads the same question,
        not a 'failed to start' wrapper around it."""
        bodies, result, _ = _run(
            "spawn_run",
            {"task": "x", "agent": "kirocrew"},
            answer={"error": solo_spawn_question(), "code": SOLO_SPAWN_REFUSED_CODE},
        )
        assert len(bodies) == 1
        assert result == solo_spawn_question()


class TestSpawnSubAgentsToolSide:
    def test_blocking_tool_cannot_claim_parent_parallelism(self):
        bodies, result, _ = _run(
            "spawn_sub_agents",
            {
                "agents": [{"prompt": "docs"}],
                "solo_reason": "parent_parallel",
                "solo_details": "parent implements backend",
            },
        )
        assert bodies == []
        assert "asynchronous spawn_run" in result

    def test_lone_entry_is_refused_before_any_post(self):
        bodies, result, sel = _run("spawn_sub_agents", {"agents": [{"prompt": "review this"}]})
        assert bodies == []
        assert result == solo_spawn_question(tool="spawn_sub_agents")
        kw = sel.log_tool_invocation.call_args.kwargs
        assert kw["tool_name"] == "spawn_sub_agents" and kw["outcome"] == "refused"
        sel.log_api_access.assert_called_once_with(
            caller="internal",
            operation="spawn.solo",
            outcome="denied",
            source="solo_gate",
            resources="dashboard:chat-1",
            error="one task, no reason, nothing named",
        )

    def test_reason_marks_the_wire(self):
        bodies, _, _ = _run(
            "spawn_sub_agents",
            {"agents": [{"prompt": "x"}], "solo_reason": "fresh_context"},
            answer={"id": "a1", "done": True, "result": "ok"},
        )
        assert len(bodies) == 1
        assert bodies[0]["solo"] is True and bodies[0]["solo_reason"] == "fresh_context"

    def test_named_agent_passes_the_tool_side(self):
        bodies, _, _ = _run(
            "spawn_sub_agents",
            {"agents": [{"prompt": "x", "agent_or_mode": "kirocrew-research"}]},
            answer={"id": "a1", "done": True, "result": "ok"},
        )
        assert len(bodies) == 1 and bodies[0]["solo"] is True

    def test_two_entries_are_never_solo(self):
        bodies, _, _ = _run(
            "spawn_sub_agents",
            {"agents": [{"prompt": "a"}, {"prompt": "b"}]},
            answer={"id": "a1", "done": True, "result": "ok"},
        )
        assert len(bodies) == 2
        assert all("solo" not in b for b in bodies)


# ── gateway side ─────────────────────────────────────────────────────────────


def _state(
    selection=("template", "kirocrew"),
    slot_model: str | None = None,
    raises=False,
    template: str | None = None,
):
    """A gateway state whose parent session has *selection* and runs on
    *template* (defaults to the selection's own name, which is what a plain
    template session's ``get_agent`` answers; a member session answers its
    RESOLVED template, not its alias)."""
    sessions = MagicMock()
    if raises:
        sessions.get_agent_selection.side_effect = RuntimeError("no live session")
        sessions.get_agent.side_effect = RuntimeError("no live session")
    else:
        sessions.get_agent_selection.return_value = selection
        sessions.get_agent.return_value = selection[1] if template is None else template
    slots: dict[str, Any] = {}
    if slot_model is not None:
        slots["chat-1"] = SimpleNamespace(model=slot_model, key="chat-1")
    return SimpleNamespace(sessions=sessions, _slots=slots)


class TestRosterCheck:
    """``solo_spawn_difference``: ``""`` only when everything named is the
    parent's own; otherwise the ground; unknown facts fail OPEN and say so."""

    def test_own_agent_does_not_differ(self):
        assert solo_spawn_difference(_state(), "dashboard:chat-1", agent="kirocrew") == ""

    def test_other_agent_differs(self):
        assert (
            solo_spawn_difference(_state(), "dashboard:chat-1", agent="kirocrew-worker") == "agent"
        )

    def test_member_naming_its_own_template_does_not_differ(self):
        """A member ``coder`` runs on the ``kirocrew-worker`` template. Naming
        that template as ``agent`` names its OWN agent: the comparison is
        against the resolved template, never the member alias."""
        st = _state(selection=("member", "coder"), template="kirocrew-worker")
        assert solo_spawn_difference(st, "dashboard:chat-1", agent="kirocrew-worker") == ""
        assert solo_spawn_difference(st, "dashboard:chat-1", agent="coder") == "agent"
        assert solo_spawn_difference(st, "dashboard:chat-1", agent="kirocrew") == "agent"

    def test_default_crew_from_a_template_session_is_its_own(self):
        assert solo_spawn_difference(_state(), "dashboard:chat-1", crew="default") == ""

    def test_another_crew_differs(self):
        assert solo_spawn_difference(_state(), "dashboard:chat-1", crew="coder") == "crew"

    def test_member_naming_itself_does_not_differ(self):
        st = _state(selection=("member", "coder"), template="kirocrew-worker")
        assert solo_spawn_difference(st, "dashboard:chat-1", crew="coder") == ""
        assert solo_spawn_difference(st, "dashboard:chat-1", crew="reviewer") == "crew"

    def test_unknown_parent_fails_open_and_says_so(self):
        got = solo_spawn_difference(_state(raises=True), "dashboard:chat-1", agent="kirocrew")
        assert got == "agent (parent unknown)"
        got = solo_spawn_difference(_state(selection=("template", "")), "slack:T1", agent="x")
        assert got == "agent (parent unknown)"
        got = solo_spawn_difference(_state(raises=True), "dashboard:chat-1", crew="coder")
        assert got == "crew (parent unknown)"

    def test_nothing_named_does_not_differ(self):
        assert solo_spawn_difference(_state(), "dashboard:chat-1") == ""

    def test_the_auto_sentinel_is_nothing_named(self):
        """On the gateway too: ``model="auto"`` cannot be the ground a lone
        spawn is let through on, even when the slot pins a real model."""
        st = _state(slot_model="deepseek-3.2")
        with patch(
            "kiro_crew.dashboard.chat_utils.effective_session_key", return_value="dashboard:chat-1"
        ):
            assert solo_spawn_difference(st, "dashboard:chat-1", model="auto") == ""
            assert solo_spawn_difference(_state(), "dashboard:chat-1", model="auto") == ""

    def test_same_model_as_the_slot_does_not_differ(self):
        st = _state(slot_model="deepseek-3.2")
        with patch(
            "kiro_crew.dashboard.chat_utils.effective_session_key", return_value="dashboard:chat-1"
        ):
            assert solo_spawn_difference(st, "dashboard:chat-1", model="deepseek-3.2") == ""
            assert solo_spawn_difference(st, "dashboard:chat-1", model="DEEPSEEK-3.2") == ""
            assert solo_spawn_difference(st, "dashboard:chat-1", model="opus-test") == "model"

    def test_channel_linked_slot_model_is_compared(self):
        st = _state(slot_model="deepseek-3.2")
        parent_session = "slack:C1/171.2"
        with patch(
            "kiro_crew.dashboard.chat_utils.effective_session_key", return_value=parent_session
        ):
            assert solo_spawn_difference(st, parent_session, model="deepseek-3.2") == ""
            assert solo_spawn_difference(st, parent_session, model="opus-test") == "model"

    @pytest.mark.parametrize("stored", ["", "auto"])
    def test_unpinned_slot_model_fails_open(self, stored: str):
        st = _state(slot_model=stored)
        with patch(
            "kiro_crew.dashboard.chat_utils.effective_session_key", return_value="dashboard:chat-1"
        ):
            got = solo_spawn_difference(st, "dashboard:chat-1", model="deepseek-3.2")
            assert got == "model (parent unknown)"

    def test_slot_owned_by_another_session_is_not_consulted(self):
        st = _state(slot_model="deepseek-3.2")
        with patch(
            "kiro_crew.dashboard.chat_utils.effective_session_key", return_value="dashboard:other"
        ):
            got = solo_spawn_difference(st, "dashboard:chat-1", model="deepseek-3.2")
            assert got == "model (parent unknown)"


class TestApiSpawnGate:
    """The gate needs a parent it can compare against, so every body names the
    ``dashboard:1`` slot the state carries -- the same shape the effort tests
    use to reach the spawn call with a parent."""

    PARENT = "dashboard:1"

    def _request(self, body: dict, selection=("template", "kirocrew"), template: str | None = None):
        from kiro_crew.dashboard.state import _ChatSlot

        body = {"parent_session": self.PARENT, **body}
        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        mgr.max_concurrent = 4
        sessions = MagicMock()
        # ``get_agent`` answers the RESOLVED template; for a member session that
        # is not the member alias the selection carries.
        sessions.get_agent.return_value = selection[1] if template is None else template
        sessions.get_agent_selection.return_value = selection
        state = SimpleNamespace(
            _slots={"1": _ChatSlot("1", memory_mode="persistent")},
            _restricted_keys=set(),
            subagents=mgr,
            sessions=sessions,
            conversation_log=SimpleNamespace(get_metadata_status=lambda key: ({}, True)),
        )
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": self.PARENT}

        async def _json() -> dict:
            return body

        request.json = _json
        return request, mgr

    async def _call(
        self, body: dict, selection=("template", "kirocrew"), template: str | None = None
    ):
        """(response, spawn manager, audit sink) for one POST /api/spawn."""
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.handlers import messaging

        request, mgr = self._request(body, selection, template)
        sel = MagicMock()
        with (
            patch.object(messaging, "_sel", return_value=sel),
            patch.object(messaging, "warm_project_agents_for_spawn", AsyncMock()),
        ):
            resp = await messaging.api_spawn(request)
        return resp, mgr, sel

    @staticmethod
    def _solo_audits(sel) -> list[dict]:
        return [
            c.kwargs
            for c in sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "spawn.solo"
        ]

    @pytest.mark.asyncio
    async def test_own_agent_named_to_slip_past_the_tool_is_refused(self):
        resp, mgr, sel = await self._call({"task": "x", "agent": "kirocrew", "solo": True})
        assert resp.status == 400
        assert SOLO_SPAWN_REFUSED_CODE in resp.text
        assert "Can you do this task yourself" in resp.text
        mgr.spawn.assert_not_called()
        kw = sel.log_api_access.call_args.kwargs
        assert kw["operation"] == "spawn.solo" and kw["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_member_naming_its_own_template_is_refused(self):
        """Member ``coder`` on template ``kirocrew-worker`` names that template:
        its own agent under another name, so the gate still fires."""
        resp, mgr, sel = await self._call(
            {"task": "x", "agent": "kirocrew-worker", "solo": True},
            selection=("member", "coder"),
            template="kirocrew-worker",
        )
        assert resp.status == 400
        assert SOLO_SPAWN_REFUSED_CODE in resp.text
        mgr.spawn.assert_not_called()
        assert [a["outcome"] for a in self._solo_audits(sel)] == ["denied"]

    @pytest.mark.asyncio
    async def test_reason_is_accepted_and_audited(self):
        resp, mgr, sel = await self._call({"task": "x", "solo": True, "solo_reason": "bulk_data"})
        assert resp.status == 200
        mgr.spawn.assert_called_once()
        kw = sel.log_api_access.call_args.kwargs
        assert kw["operation"] == "spawn.solo" and kw["outcome"] == "allowed"
        assert "reason=bulk_data" in kw["resources"]

    @pytest.mark.asyncio
    async def test_other_agent_passes_and_is_audited_with_the_ground(self):
        """The third gate outcome -- let through on a difference -- is audited
        too, naming what differed; no outcome is invisible after the fact."""
        resp, mgr, sel = await self._call({"task": "x", "agent": "kirocrew-worker", "solo": True})
        assert resp.status == 200
        mgr.spawn.assert_called_once()
        audits = self._solo_audits(sel)
        assert [a["outcome"] for a in audits] == ["allowed"]
        assert audits[0]["source"] == "solo_gate"
        assert audits[0]["resources"] == f"{self.PARENT} differs=agent"

    @pytest.mark.asyncio
    async def test_programmatic_clients_are_not_gated(self):
        """No ``solo`` marker (the SDK, an app posting directly): no gate."""
        resp, mgr, sel = await self._call({"task": "x"})
        assert resp.status == 200
        mgr.spawn.assert_called_once()
        assert not any(
            c.kwargs.get("operation") == "spawn.solo" for c in sel.log_api_access.call_args_list
        )

    @pytest.mark.asyncio
    async def test_bad_reason_is_a_validation_400(self):
        resp, mgr, _ = await self._call({"task": "x", "solo": True, "solo_reason": "because"})
        assert resp.status == 400
        mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_parallel_claim_reaches_manager_with_evidence_and_capability(self):
        import json

        resp, mgr, _ = await self._call(
            {
                "task": "docs",
                "solo": True,
                "solo_reason": "parent_parallel",
                "solo_details": "parent: backend.py; child: docs/api.md",
            }
        )
        assert resp.status == 200
        assert json.loads(resp.text)["parent_work_supported"] is True
        assert mgr.spawn.call_args.kwargs["delegation"] == {
            "reason": "parent_parallel",
            "details": "parent: backend.py; child: docs/api.md",
            "source": "model_claim",
        }

    @pytest.mark.asyncio
    async def test_parallel_claim_without_supported_parent_is_refused(self):
        with patch(
            "kiro_crew.dashboard.handlers.messaging.parent_work_supported", return_value=False
        ):
            resp, mgr, _ = await self._call(
                {
                    "task": "docs",
                    "solo": True,
                    "solo_reason": "parent_parallel",
                    "solo_details": "parent implements backend",
                }
            )
        assert resp.status == 400
        assert "dashboard-owned" in resp.text
        mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_http_cannot_skip_new_reason_details(self):
        resp, mgr, _ = await self._call({"task": "review", "solo_reason": "user_requested"})
        assert resp.status == 400
        mgr.spawn.assert_not_called()


@pytest.mark.parametrize("parent", ["", "cron:a", "subagent:a", "hook:a", "slack:unlinked"])
def test_unknown_or_background_parent_cannot_claim_concurrent_work(parent):
    from kiro_crew.solo_spawn import parent_work_supported

    assert not parent_work_supported(SimpleNamespace(_slots={}), parent)
