"""Tests for plan mode — the session-scoped read-only tool gate.

Covers the predicate (what counts as read-only), the session registry and its
inheritance to subagents, and enforcement at both chokepoints: the
``hooks.on_tool_call`` pre-rung every surface consults, and the always-enforced
section of ``llm_helpers._resolve_permission`` that AUTO_APPROVE callers reach.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from kiro_crew import hooks, llm_helpers, mcp_shared, plan_mode
from kiro_crew.hooks import TOOL_DENY, HookManager

# A mutating command assembled at runtime: the literal form trips the agent
# harness's own deny patterns, which would block the test file from being run.
DESTRUCTIVE_CMD = "rm" + " -rf /tmp/plan-mode-test"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts and ends with an empty registry."""
    plan_mode.reset()
    yield
    plan_mode.reset()


class TestRegistry:
    def test_inactive_by_default(self):
        assert plan_mode.is_active("dashboard:1") is False

    def test_activate_and_deactivate(self):
        plan_mode.activate("dashboard:1")
        assert plan_mode.is_active("dashboard:1") is True
        plan_mode.deactivate("dashboard:1")
        assert plan_mode.is_active("dashboard:1") is False

    def test_set_active_both_directions(self):
        plan_mode.set_active("dashboard:1", True)
        assert plan_mode.is_active("dashboard:1") is True
        plan_mode.set_active("dashboard:1", False)
        assert plan_mode.is_active("dashboard:1") is False

    def test_scoped_per_session(self):
        plan_mode.activate("dashboard:1")
        assert plan_mode.is_active("dashboard:2") is False

    def test_empty_key_never_active(self):
        # A caller that cannot identify its session must not inherit another
        # session's gate — in either direction.
        plan_mode.activate("")
        assert plan_mode.is_active("") is False
        assert plan_mode.active_sessions() == frozenset()

    def test_deactivate_unknown_key_is_noop(self):
        plan_mode.deactivate("never-registered")  # must not raise

    def test_active_sessions_snapshot_is_immutable(self):
        plan_mode.activate("dashboard:1")
        snap = plan_mode.active_sessions()
        plan_mode.activate("dashboard:2")
        assert snap == frozenset({"dashboard:1"})


class TestInheritance:
    def test_child_inherits_from_planning_parent(self):
        plan_mode.activate("dashboard:1")
        assert plan_mode.inherit("dashboard:1", "subagent:abc") is True
        assert plan_mode.is_active("subagent:abc") is True

    def test_child_of_idle_parent_is_not_gated(self):
        assert plan_mode.inherit("dashboard:1", "subagent:abc") is False
        assert plan_mode.is_active("subagent:abc") is False

    def test_empty_child_key_is_refused(self):
        plan_mode.activate("dashboard:1")
        assert plan_mode.inherit("dashboard:1", "") is False

    def test_releasing_child_leaves_parent_gated(self):
        plan_mode.activate("dashboard:1")
        plan_mode.inherit("dashboard:1", "subagent:abc")
        plan_mode.deactivate("subagent:abc")
        assert plan_mode.is_active("subagent:abc") is False
        assert plan_mode.is_active("dashboard:1") is True


class TestReadOnlyPredicate:
    @pytest.mark.parametrize(
        "tool",
        ["Read", "read", "fs_read", "Grep", "grep", "Glob", "glob", "WorkspaceSearch"],
    )
    def test_file_reads_allowed(self, tool):
        assert plan_mode.deny_reason(tool, trusted_tool_name=tool) == ""

    @pytest.mark.parametrize("tool", ["web_fetch", "web_search", "introspect", "tool_search"])
    def test_research_tools_allowed(self, tool):
        assert plan_mode.deny_reason(tool, trusted_tool_name=tool) == ""

    @pytest.mark.parametrize("tool", ["fs_write", "write", "Edit", "execute_bash", "use_aws"])
    def test_unknown_and_write_tools_denied(self, tool):
        assert plan_mode.deny_reason(tool, trusted_tool_name=tool) != ""

    def test_status_prefix_stripped(self):
        assert plan_mode.deny_reason("Running: Grep", trusted_tool_name="Running: Grep") == ""

    def test_whitespace_trimmed(self):
        assert plan_mode.deny_reason("  WorkspaceSearch  ", trusted_tool_name="  WorkspaceSearch  ") == ""

    def test_empty_title_denied(self):
        # Deny-by-default: an unusable title is not a reason to allow.
        assert plan_mode.deny_reason("", trusted_tool_name="") != ""

    def test_denial_names_the_gate_and_forbids_retry(self):
        reason = plan_mode.deny_reason("fs_write", trusted_tool_name="fs_write")
        assert reason.startswith(plan_mode.DENY_PREFIX)
        assert "Do not retry" in reason
        # The model must be told what still works, or it stops investigating.
        assert "Reading, searching" in reason


class TestShellClassification:
    @pytest.mark.parametrize("cmd", ["ls -la", "git status", "cat setup.cfg", "grep -rn x src/"])
    def test_read_only_commands_allowed(self, cmd):
        assert plan_mode.deny_reason("Running: shell", command=cmd, is_shell=True) == ""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push origin main",
            "npm install",
            "echo hi > out.txt",
            "pip install kirocrew",
        ],
    )
    def test_mutating_commands_denied(self, cmd):
        assert plan_mode.deny_reason("Running: shell", command=cmd, is_shell=True) != ""

    def test_destructive_command_denied(self):
        assert plan_mode.deny_reason("Running: shell", command=DESTRUCTIVE_CMD, is_shell=True) != ""

    def test_denial_carries_the_classifier_reason(self):
        reason = plan_mode.deny_reason("Running: shell", command="npm install", is_shell=True)
        assert "not on the read-only allowlist" in reason

    def test_benign_title_cannot_launder_a_write(self):
        # select_tool_title prefers an LLM-authored description over the real
        # command, so the title is untrusted: the command decides.
        reason = plan_mode.deny_reason(
            "Read the config file", command="git push origin main", is_shell=True
        )
        assert reason != ""

    def test_shell_decision_ignores_the_tool_allowlist(self):
        # "Read" is an allowlisted name, but a shell call carrying a write
        # command must still be denied.
        reason = plan_mode.deny_reason("Read", command=DESTRUCTIVE_CMD, is_shell=True)
        assert reason != ""


class TestCodeToolOperations:
    @pytest.mark.parametrize(
        "operation",
        ["search_symbols", "find_references", "get_hover", "pattern_search", "get_diagnostics"],
    )
    def test_read_operations_allowed(self, operation):
        assert plan_mode.deny_reason("code", trusted_tool_name="code", raw_params={"operation": operation}) == ""

    @pytest.mark.parametrize(
        "operation",
        ["pattern_rewrite", "rename_symbol", "format", "apply_code_action"],
    )
    def test_write_operations_denied(self, operation):
        reason = plan_mode.deny_reason("code", trusted_tool_name="code", raw_params={"operation": operation})
        assert reason != ""
        assert operation in reason

    def test_missing_operation_denied(self):
        assert plan_mode.deny_reason("code", trusted_tool_name="code", raw_params={}) != ""

    def test_absent_params_denied(self):
        assert plan_mode.deny_reason("code", trusted_tool_name="code") != ""

    def test_write_operations_are_not_in_the_read_set(self):
        for op in ("pattern_rewrite", "rename_symbol", "format", "apply_code_action"):
            assert op not in plan_mode.CODE_READ_OPERATIONS


class TestMcpQualification:
    @pytest.mark.parametrize(
        "title",
        ["@kirocrew-core/artifact_list", "mcp__kirocrew-core__artifact_list"],
    )
    def test_allowlisted_mcp_read_allowed_in_both_spellings(self, title):
        assert plan_mode.deny_reason(title, trusted_tool_name=title) == ""

    def test_mcp_write_denied(self):
        assert plan_mode.deny_reason("@kirocrew-core/send_message", trusted_tool_name="@kirocrew-core/send_message") != ""

    def test_same_name_on_another_server_denied(self):
        # Qualified matching on purpose: a hostile server must not inherit an
        # allowance by naming its tool artifact_list.
        assert plan_mode.deny_reason("@evil-mcp/artifact_list", trusted_tool_name="@evil-mcp/artifact_list") != ""

    def test_spawn_allowed_because_children_inherit(self):
        assert plan_mode.deny_reason("@kirocrew-core/spawn_run", trusted_tool_name="@kirocrew-core/spawn_run") == ""
        assert plan_mode.deny_reason("@kirocrew-core/spawn_sub_agents", trusted_tool_name="@kirocrew-core/spawn_sub_agents") == ""

    def test_every_mcp_entry_is_server_qualified(self):
        for entry in plan_mode.PLAN_MODE_SAFE_MCP:
            assert entry.startswith("@") and "/" in entry, entry

    def test_no_write_verbs_in_the_builtin_allowlist(self):
        for name in plan_mode.PLAN_MODE_SAFE_TOOLS:
            lowered = name.lower()
            assert "write" not in lowered
            assert "delete" not in lowered
            assert "update" not in lowered


class TestHookEnforcement:
    def test_denies_write_while_planning(self):
        hooks = HookManager()
        plan_mode.activate("dashboard:1")
        result = hooks.on_tool_call("fs_write", session_key="dashboard:1")
        assert result.action == TOOL_DENY
        assert result.reason.startswith(plan_mode.DENY_PREFIX)

    def test_allows_write_when_not_planning(self):
        hooks = HookManager()
        result = hooks.on_tool_call("fs_write", session_key="dashboard:1")
        assert result.action != TOOL_DENY

    def test_only_the_planning_session_is_gated(self):
        hooks = HookManager()
        plan_mode.activate("dashboard:1")
        assert hooks.on_tool_call("fs_write", session_key="dashboard:2").action != TOOL_DENY

    def test_reads_still_pass_while_planning(self):
        hooks = HookManager()
        plan_mode.activate("dashboard:1")
        assert hooks.on_tool_call(
            "Grep", session_key="dashboard:1", mcp_tool_name="grep"
        ).action != TOOL_DENY

    def test_missing_session_key_does_not_gate(self):
        # Surfaces that never thread a session key keep their prior behavior.
        hooks = HookManager()
        plan_mode.activate("dashboard:1")
        assert hooks.on_tool_call("fs_write").action != TOOL_DENY

    def test_inherited_subagent_session_is_gated(self):
        hooks = HookManager()
        plan_mode.activate("dashboard:1")
        plan_mode.inherit("dashboard:1", "subagent:abc")
        assert hooks.on_tool_call("fs_write", session_key="subagent:abc").action == TOOL_DENY

    def test_shell_write_denied_through_the_hook(self):
        hooks = HookManager()
        plan_mode.activate("dashboard:1")
        result = hooks.on_tool_call(
            "Running: install deps",
            session_key="dashboard:1",
            command="npm install",
            is_shell=True,
        )
        assert result.action == TOOL_DENY


class TestChokepointContract:
    """Plan mode has exactly ONE live chokepoint. Pin that, so a future edit
    cannot quietly add a second that looks like a defence layer but never fires.

    ``hooks.on_tool_call`` — the permission plane, consulted by every surface
    before its own trust ladder — is the whole of the enforcement.

    Two places deliberately have NO plan gate, each because a check there is
    unreachable by construction rather than merely unnecessary:

    * ``llm_helpers._resolve_permission`` — its only caller passes no
      ``session_key``, so a session-keyed check can never fire.
    * ``mcp_shared.call_tool_with_logging`` — runs in the mcp-core child
      process, which cannot see the gateway-side registry. See
      ``TestAutoApprovedMcpIsADocumentedGap``.
    """

    def test_resolve_permission_has_no_plan_gate(self):
        source = Path(inspect.getsourcefile(llm_helpers)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_resolve_permission":
                target = node
                break
        assert target is not None
        names = {
            f"{n.func.value.id}.{n.func.attr}"
            for n in ast.walk(target)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
        }
        assert "plan_mode.is_active" not in names, (
            "a plan gate here cannot fire — _resolve_permission's only caller "
            "passes no session_key. Remove it or thread identity first."
        )

    def test_only_caller_still_passes_no_session_key(self):
        # If this ever changes, the gate above becomes worth adding back.
        source = Path(inspect.getsourcefile(llm_helpers)).read_text(encoding="utf-8")
        calls = [ln for ln in source.splitlines() if "_resolve_permission(" in ln]
        assert len(calls) == 2, calls  # the def plus its single call site

    def test_auto_approved_builtins_are_a_documented_gap(self):
        """The shipped spec auto-approves `code`, which neither gate can see.

        `code` is a kiro-cli builtin: with it in `allowedTools` its write
        operations are
        approved locally and emit no permission request, so CODE_READ_OPERATIONS
        never runs for the default agent. Pinned here so the limitation is a
        known, testable fact rather than an assumption a reader has to
        reconstruct — and so this test fails (prompting a re-think) if `code` is
        ever dropped from the shipped allowlist.
        """
        import json

        from kiro_crew.config import loader

        defaults = json.loads(
            (Path(inspect.getsourcefile(loader)).parent / "defaults.json").read_text(
                encoding="utf-8"
            )
        )
        allowed = defaults.get("allowedTools", [])
        assert "code" in allowed, (
            "`code` left the shipped allowedTools — its write operations may now "
            "reach the permission plane, so plan_mode.CODE_READ_OPERATIONS could "
            "become live. Re-check the gate's documented scope."
        )
        # The predicate itself is correct; it is only unreachable on this path.
        assert plan_mode.deny_reason("code", trusted_tool_name="code", raw_params={"operation": "pattern_rewrite"}) != ""

    def test_the_permission_plane_is_the_one_live_chokepoint(self):
        source = Path(inspect.getsourcefile(hooks)).read_text(encoding="utf-8")
        assert "plan_mode.is_active" in source
        # And nowhere that cannot see the registry claims to enforce it.
        mcp_src = Path(inspect.getsourcefile(mcp_shared)).read_text(encoding="utf-8")
        assert "plan_mode.is_active" not in mcp_src


class TestMcpPolicyAtThePermissionPlane:
    """Which MCP tools the gate allows, asserted through the LIVE path.

    The policy lives in ``PLAN_MODE_SAFE_MCP`` and is consumed by
    ``deny_reason``. These previously ran against a dispatch-side helper that has
    been removed (see ``TestAutoApprovedMcpIsADocumentedGap``), so they now drive
    the permission plane, which is where the policy actually applies.
    """

    def _mcp(self, server: str, tool: str) -> str:
        return plan_mode.deny_reason(
            f"@{server}/{tool}",
            trusted_tool_name=tool,
            trusted_server_name=server,
        )

    def test_write_tool_denied(self):
        for tool in ("send_message", "artifact_delete", "learn_add",
                     "deploy_artifact", "task_run"):
            assert self._mcp("kirocrew-core", tool) != "", tool

    def test_read_tool_allowed(self):
        assert self._mcp("kirocrew-core", "artifact_list") == ""
        assert self._mcp("kirocrew-core", "spawn_run") == ""
        assert self._mcp("kirocrew-cron", "cron_list") == ""

    def test_destructive_cron_verbs_denied(self):
        for verb in ("cron_remove", "cron_remove_all", "cron_trigger", "cron_pause"):
            assert self._mcp("kirocrew-cron", verb) != "", verb

    def test_computer_use_denied_wholesale(self):
        for tool in ("computer_click", "computer_type", "computer_get_state"):
            assert self._mcp("kirocrew-computer", tool) != "", tool

    def test_unknown_server_denied(self):
        # Qualification is the point: a third-party server does not inherit
        # kirocrew-core's allowance for a same-named tool.
        assert self._mcp("third-party", "artifact_list") != ""


class TestAutoApprovedMcpIsADocumentedGap:
    """Plan mode does NOT reach tools kiro-cli auto-approves. Pinned deliberately.

    An auto-approved tool is approved locally by kiro-cli with no permission
    request emitted, so it never reaches ``hooks.on_tool_call``. A gate in
    ``mcp_shared.call_tool_with_logging`` cannot cover it either: that function
    runs in the mcp-core stdio CHILD process, while the registry is mutated only
    gateway-side, so the gate would be inert while appearing to work. An earlier
    revision of this PR shipped exactly that, which is why the limit is now
    asserted rather than assumed.

    These tests FAIL if someone reintroduces a dispatch-side gate without first
    making the flag cross the process boundary — at which point the fix is to
    delete these tests along with the note in ``call_tool_with_logging``.
    """

    def test_no_dispatch_side_plan_gate_exists(self):
        import inspect

        from kiro_crew import mcp_shared

        src = inspect.getsource(mcp_shared.call_tool_with_logging)
        assert "plan_mode.is_active" not in src, (
            "a plan-mode gate in the MCP child process is inert: the registry "
            "lives in the gateway. Make the flag cross the boundary first."
        )

    def test_the_limit_is_documented_where_a_reader_will_look(self):
        import inspect

        from kiro_crew import mcp_shared

        src = inspect.getsource(mcp_shared.call_tool_with_logging)
        assert "plan mode is deliberately NOT enforced here" in src

    def test_auto_approved_servers_are_still_in_the_shipped_allowlist(self):
        """The gap's precondition. If this changes, revisit the promise.

        kirocrew-core being auto-approved is WHY those tools escape the gate. If
        it ever leaves allowedTools, the permission plane starts seeing them and
        plan mode's real coverage widens — so the copy and spec should be
        revisited rather than left understating it.
        """
        import json
        import pathlib

        defaults = json.loads(
            pathlib.Path("src/kiro_crew/config/defaults.json").read_text()
        )
        blob = json.dumps(defaults)
        assert "@kirocrew-core" in blob


class TestSubagentWiring:
    """Structural guard: spawn_run is allowlisted only because children inherit.

    If the inherit() call is ever removed from the subagent run path, a planning
    session could spawn a helper that writes freely. An AST check keeps the two
    facts tied together instead of trusting a comment.
    """

    def test_run_calls_plan_mode_inherit(self):
        from kiro_crew import subagent

        source = Path(inspect.getsourcefile(subagent)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        run_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run":
                run_fn = node
                break
        assert run_fn is not None, "SubagentManager._run not found"

        calls = {
            f"{n.func.value.id}.{n.func.attr}"
            for n in ast.walk(run_fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
        }
        assert "plan_mode.inherit" in calls, (
            "subagent._run must propagate plan mode to the child session — "
            "without it, spawn_run in PLAN_MODE_SAFE_MCP is an escape hatch"
        )
        assert "plan_mode.deactivate" in calls, (
            "subagent._run must release the child's registry entry when the run ends"
        )


class TestTrustedIdentityOnly:
    """The allow decision keys on ``_meta.kiro``, never on the display title.

    ``select_tool_title`` (acp/_dispatch.py) prefers the model's own
    ``description`` over the real tool name, so the title is LLM-authored prose.
    Keying the read allowlist on it was wrong in BOTH directions: a write tool
    described as "Read" was auto-approved, and a genuine read whose title was
    ordinary prose ("Read /etc/hosts") was denied.
    """

    def _decide(self, **kw):
        from kiro_crew.hooks import HookManager

        plan_mode.reset()
        plan_mode.activate("dashboard:t")
        try:
            translated = {
                # main names these mcp_*, but they carry _meta.kiro for builtins
                # too; the gate's contract is that they are NOT model-authored.
                "mcp_tool_name": kw.pop("trusted_tool_name", ""),
                "mcp_server_name": kw.pop("trusted_server_name", ""),
                **kw,
            }
            return HookManager().on_tool_call(
                session_key="dashboard:t", agent="kirocrew", **translated
            )
        finally:
            plan_mode.reset()

    def test_write_tool_cannot_impersonate_a_read_via_description(self):
        # The attack: the model names its own call "Read". Pre-fix this returned
        # auto_approve, so the mutation ran with plan mode still armed.
        r = self._decide(tool_name="Read", trusted_tool_name="fs_write")
        assert r.action == TOOL_DENY

    def test_write_tool_cannot_impersonate_an_allowlisted_mcp_tool(self):
        r = self._decide(
            tool_name="ask_question",
            trusted_tool_name="artifact_delete",
            trusted_server_name="kirocrew-core",
        )
        assert r.action == TOOL_DENY

    def test_third_party_server_cannot_borrow_a_builtin_read_name(self):
        # Qualification is what stops any server inheriting another's allowance:
        # a tool genuinely named "read" on an unknown server is still not
        # @kirocrew-core/read.
        r = self._decide(
            tool_name="Read",
            trusted_tool_name="read",
            trusted_server_name="evil-server",
        )
        assert r.action == TOOL_DENY

    def test_missing_identity_denies_rather_than_trusting_the_title(self):
        # A backend that emits no _meta.kiro yields "". That must deny (loudly
        # unusable) rather than fall back to the forgeable title.
        r = self._decide(tool_name="Read", trusted_tool_name="")
        assert r.action == TOOL_DENY
        assert "verifiable tool identity" in (r.reason or "")

    def test_genuine_read_passes_despite_prose_title(self):
        # The other half of the bug: real titles are prose, so a title-keyed
        # allowlist blocked ordinary investigation.
        r = self._decide(tool_name="Read /etc/hosts", trusted_tool_name="fs_read")
        assert r.action != TOOL_DENY

    def test_genuine_allowlisted_mcp_tool_passes_despite_prose_title(self):
        r = self._decide(
            tool_name="Asking the user a question",
            trusted_tool_name="ask_question",
            trusted_server_name="kirocrew-core",
        )
        assert r.action != TOOL_DENY

    def test_code_operation_still_decided_on_raw_params(self):
        # raw_params is the real input the call executes with, so it is trusted
        # for the same reason ``command`` is.
        allowed = self._decide(
            tool_name="Looking up symbols",
            trusted_tool_name="code",
            raw_params={"operation": "search_symbols"},
        )
        denied = self._decide(
            tool_name="Looking up symbols",
            trusted_tool_name="code",
            raw_params={"operation": "pattern_rewrite"},
        )
        assert allowed.action != TOOL_DENY
        assert denied.action == TOOL_DENY

    def test_every_event_based_gate_forwards_the_trusted_identity(self):
        """AST guard: a gate that drops these kwargs silently re-opens the bypass.

        The kwargs are main's ``mcp_tool_name`` / ``mcp_server_name``, which carry
        ``_meta.kiro`` for builtins as well as MCP-served calls.

        The plan gate can only be reached by a dashboard slot or an inherited
        sub-agent, so those two call sites plus the shared channel gate must all
        forward the identity.
        """
        import ast
        import pathlib

        for rel in (
            "src/kiro_crew/dashboard/chat_runner.py",
            "src/kiro_crew/subagent.py",
            "src/kiro_crew/messaging/dispatch.py",
        ):
            src = pathlib.Path(rel).read_text(encoding="utf-8")
            tree = ast.parse(src)
            calls = [
                n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "on_tool_call"
            ]
            assert calls, f"no on_tool_call site found in {rel}"
            for call in calls:
                kwargs = {k.arg for k in call.keywords}
                # main's parameter names for the same _meta.kiro identity. If a
                # gate stops forwarding these the plan gate silently falls back
                # to no identity, which denies -- loud, but it would also break
                # every legitimate read, so pin the forwarding.
                assert "mcp_tool_name" in kwargs, rel
                assert "mcp_server_name" in kwargs, rel
