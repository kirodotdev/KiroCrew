"""Where codex refuses a tool the agent spec switched off.

The unit half of ``test_codex_session_mcp.py`` for ``disabledTools`` on the control
plane: codex has no wire channel for it, so the client answers the permission prompt
for a switched-off tool with the adapter's reject option, ``AcpSessionHandle`` refuses
it on the runtime approval path, and the tripwire notices one that ran anyway.
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import MagicMock, patch

import codex_session_mcp_helpers
import pytest
from codex_session_mcp_helpers import _CODEX_1_11_CAPS, _by_name, _codex_session_array, _write_spec

from kiro_crew.acp._dispatch import identified_mcp_call
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX
from kiro_crew.providers.mirrors.codex import codex_projection

# The shared fixtures, bound here by name so pytest registers them for this module.
agents_dir = codex_session_mcp_helpers.agents_dir


# ── the per-tool restriction on the control plane ────────────────────────────


def _codex_mcp_tool_call(call_id: str, server: str, tool: str) -> JsonRpcMessage:
    """The ``tool_call`` frame codex-acp emits for an MCP call (``createMcpToolCallUpdate``)."""
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s-1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "kind": "execute",
                "title": f"mcp.{server}.{tool}",
                "status": "pending",
                "rawInput": {"server": server, "tool": tool, "arguments": {"a": 1}},
                "_meta": {"is_mcp_tool_call": True},
            },
        },
    )


def _codex_mcp_approval(request_id: int, call_id: str) -> JsonRpcMessage:
    """The correlated ``session/request_permission`` (``buildMcpPermissionRequest``):
    no rawInput of its own, the adapter's three options, ``cancel`` as the reject."""
    return JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={
            "sessionId": "s-1",
            "toolCall": {"toolCallId": call_id, "kind": "execute", "status": "pending"},
            "_meta": {"is_mcp_tool_approval": True},
            "options": [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {
                    "optionId": "allow_session",
                    "name": "Allow for this session",
                    "kind": "allow_always",
                },
                {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
            ],
        },
    )


class TestSpecDisabledToolRefusal:
    """``disabledTools`` on the control plane is HONOURED on codex, not dropped.

    kiro-cli enforces it itself (``_MANAGED_MCP_ENTRY_KEYS`` admits the key on a
    managed entry) and claude gets it as ``permissions.deny``; codex has no wire
    channel for it, and withholding ``kirocrew-core`` whole would leave the session
    unable to report back. So the server mounts and the CALL is refused where codex
    asks: every un-annotated MCP call prompts under ``mode=read-only``
    (codex-rs ``requires_mcp_tool_approval`` with ``AppToolApproval::Auto``), Crew's
    servers declare no annotations, and the client answers the prompt for a
    switched-off tool with the adapter's reject option.
    """

    def _client(self, tmp_path, agents_dir, *, disabled: list[str]) -> AcpClient:
        _write_spec(
            agents_dir,
            servers={
                "kirocrew-core": {
                    "command": "/opt/kirocrew",
                    "args": ["mcp-core"],
                    "disabledTools": disabled,
                }
            },
            tools=["@kirocrew-core"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        # The spawn path's warm, run inline: this is where the deny set is derived.
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        return client

    @staticmethod
    def _capture(client: AcpClient) -> list[tuple]:
        sent: list[tuple] = []

        async def _send(request_id, payload):
            sent.append((request_id, payload))

        client._send_response = _send  # type: ignore[method-assign]
        return sent

    def test_the_deny_set_comes_out_of_the_projection(self, tmp_path, agents_dir):
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        assert ("kirocrew-core", "spawn_run") in client._spec_denied_tools
        # And the server itself is still mounted: the restriction narrows a tool,
        # it does not cost the session its control plane.
        assert "kirocrew-core" in _by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1])

    @pytest.mark.asyncio
    async def test_a_switched_off_tool_is_refused_at_the_approval_request(
        self, tmp_path, agents_dir, monkeypatch
    ):
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())

        # The tool_call frame arrives first and is cached by toolCallId...
        assert client._extract_tool_event(_codex_mcp_tool_call("c1", "kirocrew-core", "spawn_run"))
        # ...then codex asks. The event is built exactly as the dispatch loop builds it.
        event = client._build_permission_event(_codex_mcp_approval(7, "c1"))
        assert event.raw_params_trusted
        assert await client._deny_spec_disabled_tool(event) is True

        # Answered with the adapter's OWN reject option, never `cancelled`: a
        # cancelled outcome is the turn-scoped fallback, this is one call.
        assert sent == [(7, {"outcome": {"outcome": "selected", "optionId": "cancel"}})]
        assert audited and audited[0]["outcome"] == "denied"
        assert audited[0]["tool_name"] == "mcp__kirocrew-core__spawn_run"
        assert audited[0]["metadata"]["reason"] == "spec_disabled_tool"

    @pytest.mark.asyncio
    async def test_a_tool_the_spec_left_on_goes_to_the_ordinary_gate(self, tmp_path, agents_dir):
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        client._extract_tool_event(_codex_mcp_tool_call("c2", "kirocrew-core", "send_message"))
        event = client._build_permission_event(_codex_mcp_approval(8, "c2"))
        assert await client._deny_spec_disabled_tool(event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_the_permission_payloads_own_fields_are_never_the_identity(
        self, tmp_path, agents_dir
    ):
        """Identity comes from the cached tool_call frame or not at all.

        An uncorrelated approval carries ``rawInput = {serverName, description,
        schema}`` on the permission frame itself; a forged one could carry anything.
        With no cached frame the params are not trusted and the request goes on to
        the human -- toward ASKING, never toward running.
        """
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        msg = _codex_mcp_approval(9, "never-seen")
        msg.params["toolCall"]["rawInput"] = {"server": "kirocrew-core", "tool": "spawn_run"}
        event = client._build_permission_event(msg)
        assert not event.raw_params_trusted
        assert await client._deny_spec_disabled_tool(event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_a_non_codex_client_is_untouched(self, tmp_path, agents_dir):
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/x", "disabledTools": ["spawn_run"]}},
            tools=["@kirocrew-core"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        assert client._spec_denied_tools == frozenset()
        client._extract_tool_event(_codex_mcp_tool_call("c3", "kirocrew-core", "spawn_run"))
        event = client._build_permission_event(_codex_mcp_approval(10, "c3"))
        assert await client._deny_spec_disabled_tool(event) is False

    @pytest.mark.asyncio
    async def test_the_auto_approve_site_refuses_a_switched_off_tool(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """``_handle_permission`` answers with no consumer's gate in between, so the
        refusal must run there too -- and it does, before the approve."""
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(
            sel_mod, "sel", lambda: type("S", (), {"log_tool_invocation": lambda *a, **k: None})()
        )
        client._extract_tool_event(_codex_mcp_tool_call("c4", "kirocrew-core", "spawn_run"))
        await client._handle_permission(_codex_mcp_approval(11, "c4"))
        assert sent == [(11, {"outcome": {"outcome": "selected", "optionId": "cancel"}})]

    def test_a_first_class_pair_is_retained_verbatim_and_never_marked_unreadable(
        self, tmp_path, agents_dir
    ):
        """The KAS MCP-name bound and the unreadable mark belong to the KAS channel;
        the codex pair is a first-class path and reads as it always did -- a
        513-character tool half is retained verbatim, an exact deny on it binds,
        and nothing is marked or refused for it (H13)."""
        client = self._client(tmp_path, agents_dir, disabled=[])
        client._extract_tool_event(_codex_mcp_tool_call("c9", "kirocrew-core", "t" * 513))
        assert client._tool_call_identity_unreadable == {}
        event = client._build_permission_event(_codex_mcp_approval(31, "c9"))
        assert event is not None and event.mcp_identity_unreadable is False
        assert (event.mcp_server_name, event.tool_name) == ("kirocrew-core", "t" * 513)

    @pytest.mark.asyncio
    async def test_the_auto_approve_site_refuses_an_MCP_approval_it_cannot_identify(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """No human at this site, so "unidentified" cannot fall toward asking.

        An MCP tool approval (``_meta.is_mcp_tool_approval``) with no cached
        ``tool_call`` frame -- a loop that never populated the provenance caches, a
        standalone approval, an overflowed cache -- would otherwise be approved
        blind on a session whose spec switches tools off. It is refused instead.
        A shell approval on the same path is untouched: the check is scoped to the
        one shape it can reason about.
        """
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        await client._handle_permission(_codex_mcp_approval(12, "never-seen"))
        assert sent == [(12, {"outcome": {"outcome": "selected", "optionId": "cancel"}})]
        # A permission decision, so it reaches the SEL like the identified refusal.
        assert audited and audited[0]["outcome"] == "denied"
        assert audited[0]["metadata"]["reason"] == "spec_disabled_tool_unidentified_call"

        shell = JsonRpcMessage(
            id=13,
            method="session/request_permission",
            params={
                "sessionId": "s-1",
                "toolCall": {"toolCallId": "sh1", "kind": "execute", "title": "ls"},
                "options": [
                    {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                ],
            },
        )
        await client._handle_permission(shell)
        assert sent[-1] == (13, {"outcome": {"outcome": "selected", "optionId": "allow_once"}})

    @pytest.mark.asyncio
    async def test_the_auto_approve_site_is_untouched_without_a_deny_set(
        self, tmp_path, agents_dir
    ):
        """With nothing switched off, the site behaves exactly as before: no event
        is built, no option ids are recorded, the canonical allow id is sent."""
        client = self._client(tmp_path, agents_dir, disabled=[])
        assert client._spec_denied_tools == frozenset()
        sent = self._capture(client)
        await client._handle_permission(_codex_mcp_approval(14, "never-seen"))
        assert sent == [(14, {"outcome": {"outcome": "selected", "optionId": "allow_once"}})]
        assert 14 not in client._permission_options

    def test_the_streaming_loop_populates_the_provenance_the_refusal_reads(self):
        """``send_message_stream`` answers permissions through the auto-approve site,
        so it must run the FULL tool_call extractor (which caches raw params by
        toolCallId), not the stats-only tracker. Pinned on the source: the loop has
        no unit seam, and the failure mode is a switched-off tool running."""
        import inspect

        from kiro_crew.acp import client as client_mod

        body = inspect.getsource(client_mod.AcpClient.send_message_stream)
        # The predicate is true on every session with a deny set (and on a harness
        # whose identity channel is judged -- see _judges_permission_requests).
        assert "if self._judges_permission_requests:" in body
        assert "self._extract_tool_event(msg)" in body
        # ...and on a session that judges nothing, the stats-only tracker every other
        # backend had.
        assert "self._track_tool_call(msg)" in body

    def test_a_dashboard_toggle_on_the_control_plane_is_honoured(self, tmp_path, agents_dir):
        """The ordinary path: the dashboard writes the restriction to the GLOBAL
        settings file only, the spec never carries it for a managed server, and
        the deny set must still name it."""
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]}},
            tools=["@kirocrew-core"],
        )
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps({"mcpServers": {"kirocrew-core": {"disabledTools": ["spawn_run"]}}}),
            encoding="utf-8",
        )
        projection, kept = _codex_session_array(caps=_CODEX_1_11_CAPS)
        assert ("kirocrew-core", "spawn_run") in projection.denied_tools
        assert "kirocrew-core" in _by_name(kept)

    def test_a_third_party_server_narrowed_only_in_the_global_file_is_withheld(
        self, tmp_path, agents_dir
    ):
        """The withhold set reads the same two sources as the deny set.

        A third-party tool switched off in the dashboard lands in the global file
        only. The per-call refusal cannot reach a third-party server (an annotated
        tool is approved inside codex without asking), so the server must be
        withheld -- and it is, from the same unioned pairs, while the control plane
        stays mounted and takes the per-call path.
        """
        _write_spec(
            agents_dir,
            servers={
                "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                "third": {"command": "/bin/third"},
            },
            tools=["@kirocrew-core", "@third"],
        )
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "third": {"command": "/bin/third", "disabledTools": ["risky"]},
                        "kirocrew-core": {"disabledTools": ["spawn_run"]},
                    }
                }
            ),
            encoding="utf-8",
        )
        projection, kept = _codex_session_array(caps=_CODEX_1_11_CAPS)
        names = _by_name(kept)
        assert "third" not in names
        assert "kirocrew-core" in names
        assert ("kirocrew-core", "spawn_run") in projection.denied_tools

    def test_a_switched_off_tool_that_ran_anyway_trips_the_wire(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Independent of the adapter's prompting: if a denied call COMPLETES, the
        result frame says so and Crew makes it loud. Not enforcement -- the call
        ran -- but the difference between a silent drift and a red line."""
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        client._extract_tool_event(_codex_mcp_tool_call("c9", "kirocrew-core", "spawn_run"))
        done = JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "s-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c9",
                    "status": "completed",
                    "rawOutput": {"result": {"ok": True}, "error": None},
                },
            },
        )
        result = client._extract_tool_call_update(done)
        assert result is not None and result.tool_final
        client._tripwire_spec_disabled_tool(result)
        assert audited and audited[0]["outcome"] == "ran_despite_spec_disable"
        assert audited[0]["tool_name"] == "mcp__kirocrew-core__spawn_run"

        # A permitted tool completing, or a denied one that did NOT complete, is silent.
        audited.clear()
        client._extract_tool_event(_codex_mcp_tool_call("c10", "kirocrew-core", "send_message"))
        done.params["update"]["toolCallId"] = "c10"
        client._tripwire_spec_disabled_tool(client._extract_tool_call_update(done))
        done.params["update"]["toolCallId"] = "c9"
        done.params["update"]["status"] = "failed"
        failed = client._extract_tool_call_update(done)
        if failed is not None:
            client._tripwire_spec_disabled_tool(failed)
        assert audited == []

    def test_every_result_site_runs_the_tripwire(self):
        import inspect

        from kiro_crew.acp import client as client_mod

        for site in ("_dispatch_events", "_read_prompt_response"):
            body = inspect.getsource(getattr(client_mod.AcpClient, site))
            assert "_extract_tool_call_update(msg)" in body
            assert "_tripwire_spec_disabled_tool(" in body, site

    def test_the_control_plane_declares_no_tool_annotations(self):
        """The load-bearing premise, pinned rather than stated.

        The refusal is complete for the control plane ONLY because codex prompts
        for every one of its calls, and codex prompts (mode ``Auto``,
        ``requires_mcp_tool_approval``) only for a tool that carries no
        annotations -- a ``readOnlyHint: true`` tool is approved inside codex and
        never reaches Crew. So a Crew tool gaining an annotation for another
        backend's UX would silently reopen the dropped-restriction defect on codex.
        Both control-plane servers' ``tools/list`` descriptors are asserted
        annotation-free here; the day one needs an annotation, this is the test that
        says the codex refusal must then fire on the ``tool_call`` frame instead.
        """
        from kiro_crew import mcp_core, mcp_cron
        from kiro_crew.mcp_tools import apps as apps_tools

        # ``kirocrew-core`` lists an app's tools only while that app is enabled and
        # the isolated test home has none installed, so the gate is held open: the
        # premise must hold for every DECLARED tool, hidden or not.
        with patch.object(apps_tools, "app_enabled_state", lambda _name: True):
            core_tools = mcp_core._list_tools()
        for server, tools in (
            ("kirocrew-core", core_tools),
            ("kirocrew-cron", mcp_cron._list_tools()),
        ):
            assert tools, server
            annotated = [t["name"] for t in tools if t.get("annotations")]
            assert not annotated, (
                f"{server} tools {annotated} declare MCP annotations; codex approves an "
                "annotated read-only tool internally without asking, so the spec's "
                "disabledTools refusal at the permission request cannot reach it"
            )

    def test_a_reset_drops_the_deny_set_with_the_array(self, tmp_path, agents_dir):
        """Per-spawn freshness, same rule as the array: an edited spec is what the
        NEXT session enforces, not this one's snapshot."""
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        assert client._spec_denied_tools
        client._reset_state()
        assert client._spec_denied_tools == frozenset()
        assert client._session_mcp_cache is None

    def test_the_server_is_spelled_as_codex_registers_it(self, tmp_path, agents_dir):
        """``rawInput.server`` is the REGISTERED name, so the deny set must be too."""
        _write_spec(
            agents_dir,
            servers={"my tool": {"command": "/bin/foo", "disabledTools": ["x"]}},
            tools=["@my tool"],
        )
        projection = codex_projection("kirocrew")
        assert ("my_tool", "x") in projection.denied_tools
        assert ("my tool", "x") not in projection.denied_tools

    def test_every_site_that_answers_a_permission_request_runs_the_refusal(self):
        """Structural: the refusal is paired with EVERY ``_build_permission_event``.

        Three sites answer a ``session/request_permission`` -- the event-yielding
        dispatch loop and the two auto-approve paths through ``_handle_permission``
        -- and a restriction that holds on two of them is not a restriction. Pinned
        on the source in this file's neighbour's idiom, because the sites have no
        unit-level seam of their own.
        """
        import inspect

        from kiro_crew.acp import client as client_mod

        source = "".join(
            inspect.getsource(sys.modules[name])
            for name in (client_mod.__name__, *client_mod._CLIENT_OWNERS)
        )
        builds = source.count("self._build_permission_event(")
        # One per answering site; `_build_permission_event` is defined once more.
        assert builds == 2, "a site that answers a permission request was added or removed"
        for site in ("_dispatch_events", "_handle_permission"):
            body = inspect.getsource(getattr(client_mod.AcpClient, site))
            assert (
                "_build_permission_event(" in body
                and "_deny_spec_disabled_tool(" in body
                and "_deny_zero_tools(" in body
            ), site
        # And the two other loops answer ONLY through _handle_permission.
        assert source.count("await self._handle_permission(msg)") == 2


class TestTheRuntimeApprovalPathHonoursTheDenySet:
    """Condition 5(c): ``AcpSessionHandle`` refuses what the projection switched off.

    Semantics are the client's, and the identity read is literally the client's
    function -- a restriction that held on one transport and not the other would be
    the defect a shared driver invites.
    """

    @staticmethod
    def _handle(denied: set[tuple[str, str]]):
        runtime = MagicMock()
        runtime.acp_backend = ACP_BACKEND_CODEX
        sent: list[tuple] = []

        async def _send_response(request_id, payload):
            sent.append((request_id, payload))

        runtime.send_response = _send_response
        handle = AcpSessionHandle("s-1", asyncio.Queue(), runtime)
        handle.spec_denied_tools = frozenset(denied)
        return handle, sent

    @pytest.mark.asyncio
    async def test_a_switched_off_tool_is_refused_at_the_approval_request(self, monkeypatch):
        handle, sent = self._handle({("kirocrew-core", "spawn_run")})
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())

        # The tool_call frame arrives first and is cached by toolCallId, which is the
        # only channel the model cannot reach...
        list(handle._handle_update(_codex_mcp_tool_call("c1", "kirocrew-core", "spawn_run")))
        # ...then codex asks.
        event = handle._build_permission_event(_codex_mcp_approval(7, "c1"))
        assert event.raw_params_trusted
        assert await handle._deny_spec_disabled_tool(event) is True

        # The adapter's own reject option, never ``cancelled``: that one is the
        # turn-scoped fallback and this is one call.
        assert sent == [(7, {"outcome": {"outcome": "selected", "optionId": "cancel"}})]
        # The audit runs off the loop after the reject was sent.
        for task in list(handle._audit_tasks):
            await task
        assert audited and audited[0]["outcome"] == "denied"
        assert audited[0]["tool_name"] == "mcp__kirocrew-core__spawn_run"
        assert audited[0]["error"] == "spec_disabled_tool"

    @pytest.mark.asyncio
    async def test_a_tool_the_spec_left_on_goes_to_the_consumers_gate(self):
        handle, sent = self._handle({("kirocrew-core", "spawn_run")})
        list(handle._handle_update(_codex_mcp_tool_call("c2", "kirocrew-core", "send_message")))
        event = handle._build_permission_event(_codex_mcp_approval(8, "c2"))
        assert await handle._deny_spec_disabled_tool(event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_the_permission_payloads_own_fields_are_never_the_identity(self):
        """An uncorrelated approval carries its own ``rawInput``, which a forged frame
        could fill with anything. With no cached frame the params are untrusted and the
        request goes on to the consumer -- toward ASKING, never toward running."""
        handle, sent = self._handle({("kirocrew-core", "spawn_run")})
        msg = _codex_mcp_approval(9, "never-seen")
        msg.params["toolCall"]["rawInput"] = {"server": "kirocrew-core", "tool": "spawn_run"}
        event = handle._build_permission_event(msg)
        assert not event.raw_params_trusted
        assert await handle._deny_spec_disabled_tool(event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_an_unidentifiable_mcp_approval_is_refused_on_a_restricted_session(self):
        """codex marks its STANDALONE approval as an MCP one too, and a standalone one
        has no preceding ``tool_call`` frame -- so nothing cached its ``(server, tool)``.

        The handle has no second answering site the way ``AcpClient`` does, and a
        consumer auto-approves by hook glob or in trust mode. Left to the consumer
        that is a switched-off tool running unasked, so it is refused here.
        """
        handle, sent = self._handle({("kirocrew-core", "spawn_run")})
        msg = _codex_mcp_approval(11, "standalone-no-tool-call")
        event = handle._build_permission_event(msg)
        assert identified_mcp_call(event) is None
        assert await handle._deny_spec_disabled_tool(event) is False
        assert await handle._refuse_unidentifiable_mcp_approval(msg, event) is True
        assert sent == [(11, {"outcome": {"outcome": "selected", "optionId": "cancel"}})]

    @pytest.mark.asyncio
    async def test_a_session_with_no_deny_set_still_asks_about_an_unidentified_call(self):
        """The refusal is scoped to a session that HAS restrictions it cannot verify.
        Every other session -- kiro, KAS, a mirrored spec that switched nothing off --
        reads one falsy attribute and goes to the consumer's own gate."""
        handle, sent = self._handle(set())
        msg = _codex_mcp_approval(12, "standalone-no-tool-call")
        event = handle._build_permission_event(msg)
        assert await handle._refuse_unidentifiable_mcp_approval(msg, event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_a_non_mcp_approval_is_never_refused_by_this(self):
        """A shell or edit approval carries neither codex's marker nor a trusted
        ``_meta`` identity, so the deny set has nothing to say about it."""
        handle, sent = self._handle({("kirocrew-core", "spawn_run")})
        msg = _codex_mcp_approval(13, "shellish")
        del msg.params["_meta"]
        event = handle._build_permission_event(msg)
        assert await handle._refuse_unidentifiable_mcp_approval(msg, event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_an_identified_allowed_call_is_not_swept_up(self):
        """An MCP approval the handle CAN identify goes to the identified check, which
        lets a tool the spec left on through. This refusal must not double-refuse it."""
        handle, sent = self._handle({("kirocrew-core", "spawn_run")})
        list(handle._handle_update(_codex_mcp_tool_call("c9", "kirocrew-core", "send_message")))
        msg = _codex_mcp_approval(14, "c9")
        event = handle._build_permission_event(msg)
        assert await handle._deny_spec_disabled_tool(event) is False
        assert await handle._refuse_unidentifiable_mcp_approval(msg, event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_a_session_with_no_deny_set_checks_nothing(self):
        """Every host with no mirror, and every mirrored session whose spec switched
        nothing off: one falsy read and out."""
        handle, sent = self._handle(set())
        list(handle._handle_update(_codex_mcp_tool_call("c3", "kirocrew-core", "spawn_run")))
        event = handle._build_permission_event(_codex_mcp_approval(10, "c3"))
        assert await handle._deny_spec_disabled_tool(event) is False
        assert sent == []


def _codex_mcp_tool_result(call_id: str) -> JsonRpcMessage:
    """The terminal ``tool_call_update`` for a completed call: no identity of its own,
    only the ``toolCallId`` the ``tool_call`` frame's cache is keyed by."""
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s-1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
            },
        },
    )


class TestTheTripwireNoticesASwitchedOffToolThatRan:
    """The third deny-set reader. The two refusals fire on a permission REQUEST, and
    whether the adapter sends one is the adapter's behaviour -- so a release that
    stopped prompting would make the restriction silently inert. This is the in-band
    notice that it did.
    """

    @staticmethod
    def _handle(denied: set[tuple[str, str]]):
        runtime = MagicMock()
        runtime.acp_backend = ACP_BACKEND_CODEX
        handle = AcpSessionHandle("s-1", asyncio.Queue(), runtime)
        handle.spec_denied_tools = frozenset(denied)
        return handle

    @staticmethod
    def _audited(monkeypatch) -> list[dict]:
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        return audited

    @staticmethod
    def _feed(handle, msg) -> None:
        """Drive the frame through the parser exactly as the dispatch loop does."""
        for ev in handle._handle_update(msg):
            handle._tripwire_spec_disabled_tool(ev, msg)

    @pytest.mark.asyncio
    async def test_a_completed_switched_off_call_is_logged_and_audited(self, monkeypatch, caplog):
        handle = self._handle({("kirocrew-core", "spawn_run")})
        audited = self._audited(monkeypatch)
        self._feed(handle, _codex_mcp_tool_call("t1", "kirocrew-core", "spawn_run"))
        with caplog.at_level("WARNING"):
            self._feed(handle, _codex_mcp_tool_result("t1"))
        assert "COMPLETED although the agent spec switches it off" in caplog.text
        for task in list(handle._audit_tasks):
            await task
        assert audited and audited[0]["tool_name"] == "mcp__kirocrew-core__spawn_run"
        # NOT "denied": the call ran. A false record here is worse than no record.
        assert audited[0]["outcome"] == "ran_despite_spec_disable"
        assert audited[0]["error"] == "spec_disabled_tool_completed"

    @pytest.mark.asyncio
    async def test_a_completed_allowed_call_is_silent(self, monkeypatch, caplog):
        handle = self._handle({("kirocrew-core", "spawn_run")})
        audited = self._audited(monkeypatch)
        self._feed(handle, _codex_mcp_tool_call("t2", "kirocrew-core", "send_message"))
        with caplog.at_level("WARNING"):
            self._feed(handle, _codex_mcp_tool_result("t2"))
        assert "COMPLETED although" not in caplog.text
        assert audited == []

    @pytest.mark.asyncio
    async def test_a_session_with_no_deny_set_is_a_no_op(self, monkeypatch, caplog):
        """Every host that needs no projection: one falsy read and out."""
        handle = self._handle(set())
        audited = self._audited(monkeypatch)
        self._feed(handle, _codex_mcp_tool_call("t3", "kirocrew-core", "spawn_run"))
        with caplog.at_level("WARNING"):
            self._feed(handle, _codex_mcp_tool_result("t3"))
        assert "COMPLETED although" not in caplog.text
        assert audited == []

    @pytest.mark.asyncio
    async def test_a_pending_frame_is_not_a_completed_call(self, monkeypatch, caplog):
        """It reads ``tool_final``, so the tool_call frame that OPENS the call -- the
        one the refusals act on -- must not be reported as a call that ran."""
        handle = self._handle({("kirocrew-core", "spawn_run")})
        audited = self._audited(monkeypatch)
        with caplog.at_level("WARNING"):
            self._feed(handle, _codex_mcp_tool_call("t4", "kirocrew-core", "spawn_run"))
        assert "COMPLETED although" not in caplog.text
        assert audited == []

    @pytest.mark.asyncio
    async def test_a_cache_entry_from_another_origin_is_not_read_as_this_sessions(
        self, monkeypatch, caplog
    ):
        """The cache key carries the emitting origin, so this session cannot resolve an
        id a CHILD frame wrote -- which is what stops a replayed id from lending its
        trusted params to a different operation.

        Written under the child's scope, read under this session's: a miss. With the
        scope dropped from the key both halves collapse to the bare id, the lookup
        hits, and the tripwire fires on an entry it does not own.
        """
        handle = self._handle({("kirocrew-core", "spawn_run")})
        audited = self._audited(monkeypatch)
        child_call = _codex_mcp_tool_call("t5", "kirocrew-core", "spawn_run")
        child_call.params["sessionId"] = "some-child-session"
        self._feed(handle, child_call)
        # Written, but under the child's scope.
        assert any("some-child-session|t5" == k for k in handle._tool_call_raw_params)
        with caplog.at_level("WARNING"):
            self._feed(handle, _codex_mcp_tool_result("t5"))
        assert "COMPLETED although" not in caplog.text
        assert audited == []


def test_the_tripwire_is_wired_into_the_handles_update_branch():
    """Structural: the tripwire is called from the dispatch loop, not merely defined.

    The behavioural tests above drive the method. A detector nothing calls detects
    nothing, and this one has no other caller to notice its absence.
    """
    import inspect

    body = inspect.getsource(AcpSessionHandle._dispatch_events)
    assert "self._tripwire_spec_disabled_tool(" in body
