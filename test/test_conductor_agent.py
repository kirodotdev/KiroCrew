"""Conductor agent installer + bundled acceptance evaluator.

``kirocrew-conductor`` IS the work-ledger conductor: it mounts ``kirocrew-work``,
dispatches bind-before-seed, and settles every ``done`` claim with the bundled
``scripts/accept_eval.py``. ``kirocrew-ledger-conductor`` is a deprecated alias
emitting this same spec under its old name for one release, and the proof that
the two cannot drift lives in ``test_ledger_conductor_agent.py``.

The installer test mirrors the research-agent installer test's shape: stub the
agents dir and ``build_agent_config``, run the installer, assert on the JSON it
wrote. The evaluator tests run the real script over stdin/stdout — it is the
deterministic half of the conductor's patrol, so its verdict vocabulary is
pinned here.

The ``cmd`` fixtures deliberately use ``git`` rather than ``sys.executable``:
bare interpreters are NOT on the evaluator's allowlist (a spec could otherwise
name ``python -c <payload>``), and pinning that is one of the tests below.
"""

import inspect
import json
import subprocess
import sys
from pathlib import Path

from skill_script_helpers import load_skill_script

from kiro_crew import agent
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

SKILL_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "builtin_skills" / "goal-conductor"
)
SCRIPT = SKILL_DIR / "scripts" / "accept_eval.py"

#: An allowlisted command that always exits 0 — the "pass" fixture.


class TestConductorInstaller:
    def _install(self, tmp_path, monkeypatch, *, may_auto_approve=None):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(
            agent,
            "build_agent_config",
            lambda: {
                "name": "kirocrew",
                "prompt": "file://x",
                "mcpServers": {
                    "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
                    "builder-mcp": {"command": "/x/builder", "args": []},
                },
                "tools": ["fs_write", "@kirocrew-core"],
                "allowedTools": ["@kirocrew-core"],
            },
        )
        monkeypatch.setattr(
            agent,
            "_kirocrew_mcp_invocation",
            lambda sub: ("/resolved/kirocrew", [sub]),
        )
        # Pin the ceiling predicate: the default is an ungoverned host (keep every
        # grant), and the governed case gets its own test below.
        monkeypatch.setattr(agent, "_may_auto_approve", may_auto_approve or (lambda ref: True))
        agent._install_conductor_agent()
        return json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))

    def test_identity_and_charter(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        assert data["name"] == "kirocrew-conductor"
        assert "work item" in data["prompt"]

    def test_prompt_carries_the_verbosity_placeholder(self, tmp_path, monkeypatch):
        """The conductor is a custom agent, so it gets its OWN prompt.

        ``build_message`` reads a custom agent's prompt from its spec instead of
        ``config/prompt.md``, and ``_resolve_prompt_templates`` only expands the
        token where it appears. Without the token here the user's
        ``dashboard.verbosity`` setting silently never reaches this agent, so
        every conductor turn answers at ``default`` length no matter what the
        person picked. Pinned, not commented, because the omission is invisible.
        """
        data = self._install(tmp_path, monkeypatch)
        assert "{{VERBOSITY_BLOCK}}" in data["prompt"]

    def test_prompt_drives_patrol_with_monitor_start_not_wait(self, tmp_path, monkeypatch):
        """A patrol round outlives a turn, so the loop must own the turn boundary.

        An in-turn ``wait`` + re-poll loop spends the turn budget on latency and
        dies at the turn cap mid-round, which loses the loop. Three halves of the
        contract are pinned: ``monitor_start`` arms the round, ``wait`` is the
        single-round fallback for a refused arm rather than the primary
        mechanism, and the conductor knows the tool it needs to stop the loop it
        armed. Whitespace is normalised so re-wrapping the prompt cannot fail
        this for a formatting reason.
        """
        data = self._install(tmp_path, monkeypatch)
        prompt = " ".join(data["prompt"].split())
        assert "Arm a loop on your own session with `monitor_start`" in prompt
        assert "If arming is refused outright" in prompt
        assert "drive that one round with `wait`" in prompt
        assert "autonudge_stop" in prompt

    def test_prompt_names_the_tools_it_expects_to_be_used(self, tmp_path, monkeypatch):
        """The charter mounts whole servers, so the prompt must name what it wants.

        ``@kirocrew-core`` puts 74 registered tools in front of this agent. Naming
        only the session-control ones left the rest unaddressed, which is how a
        conductor talks itself into running a work item in its own turn. Both
        directions are pinned: the tools the four jobs need, and the four that
        would end-run the no-write property by executing the work here.

        Those four are also the reason the core grant is per-verb rather than
        server-wide — the prompt PROHIBITS them, so a blanket auto-approve
        contradicted the charter it shipped with.
        """
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        for named in (
            "session_create",
            "session_ledger_record",
            "monitor_update",
            "resource_status",
            "ask_question",
            "skill_search",
            "tool_search",
        ):
            assert named in prompt, named
        for forbidden in ("spawn_run", "spawn_sub_agents", "workflow_run", "task_run"):
            assert forbidden in prompt, forbidden
        assert "never goes to" in prompt

    def test_no_write_tool_and_shell_not_preapproved(self, tmp_path, monkeypatch):
        """The security properties of the spec, in one place.

        No ``fs_write``: the conductor cannot do a work item's work itself.
        ``@kirocrew-core`` and ``@kirocrew-dashboard`` are MOUNTED whole but never
        granted whole — the auto-approve list names verbs, so the destructive ones
        keep prompting. ``execute_bash`` is mounted and never granted, because
        ``allowedTools`` has no argument matching and trusting the two bundled
        scripts cannot be told apart from trusting arbitrary shell.
        """
        data = self._install(tmp_path, monkeypatch)
        assert "fs_write" not in data["tools"]
        assert "@kirocrew-dashboard" in data["tools"]
        assert "@kirocrew-dashboard" not in data["allowedTools"]
        assert "@kirocrew-core" in data["tools"]
        assert "@kirocrew-core" not in data["allowedTools"]
        assert "execute_bash" in data["tools"]
        assert "execute_bash" not in data["allowedTools"]

    def test_only_create_and_read_verbs_are_auto_approved(self, tmp_path, monkeypatch):
        """The grant invariant: create or read, never mutate someone else's thing.

        Every auto-approved verb is reachable by content the conductor ingested
        (its charter reads issue text; it holds ``web_fetch`` for that), with no
        human in the loop on a nudge-driven cycle. So a granted verb may make a
        NEW folder or session, or read — never write a resource the user already
        arranged, because that destroys state no prompt asked about.

        Pinned as literal names rather than by importing the tuple: the point is
        that WIDENING the tuple fails a test, which a tautological assertion
        against the tuple itself could never catch. Each withheld verb below is
        withheld for a reason recorded next to it in ``agent.py``:
        ``chat_folder_move_session`` PATCHes another session's ``folder_id``
        (target comes from the arguments, not the caller), ``session_send`` runs
        text as a peer's turn, ``session_stop`` discards a peer's in-flight work,
        and ``chat_folder_move`` reparents an existing tree.
        """
        granted = self._install(tmp_path, monkeypatch)["allowedTools"]
        for verb in (
            "chat_folder_move_session",
            "chat_folder_move",
            "session_send",
            "session_stop",
        ):
            assert f"@kirocrew-dashboard/{verb}" not in granted, verb
        for verb in (
            "chat_folder_tree",
            "chat_folder_create",
            "session_create",
            "session_read_message",
        ):
            assert f"@kirocrew-dashboard/{verb}" in granted, verb

    def test_no_dashboard_verb_is_granted_outside_the_named_set(self, tmp_path, monkeypatch):
        """Nothing reaches ``allowedTools`` that the audit above did not rule on.

        Guards the gap the per-verb lists cannot: a NEW tool added to the
        dashboard server, or a stray entry added to the grant tuple, would
        otherwise be auto-approved without anyone deciding it should be. The
        expected set is spelled out so adding a grant is a deliberate edit here.
        """
        granted = self._install(tmp_path, monkeypatch)["allowedTools"]
        dashboard = {g for g in granted if g.startswith("@kirocrew-dashboard")}
        assert dashboard == {
            "@kirocrew-dashboard/chat_folder_tree",
            "@kirocrew-dashboard/chat_folder_create",
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/session_read_message",
        }
        # The bare server must never appear: it would grant every verb, including
        # the four the test above withholds.
        assert "@kirocrew-dashboard" not in granted

    def test_core_grants_are_named_verbs_never_the_whole_server(self, tmp_path, monkeypatch):
        """Both directions of the core narrowing, because only one of them can rot.

        The positive half — the 14 verbs the charter needs are granted — passes
        just as well against the server-wide ``@kirocrew-core`` this replaced, so
        on its own it is vacuous on the thing being changed. The negative half is
        the real assertion: the verbs the charter never names must be DENIED, and
        it is the half a future edit can silently undo by restoring the bare
        server or widening the tuple.

        The denied set carries a representative per family rather than one token
        case, because they reach ``allowed_tools_to_permissions`` as separate
        patterns and a family could classify differently: persistent work
        (``task_run``, the ``workflow_*`` group), agent fan-out (the ``spawn_*``
        group), session and workspace mutation (``set_project``,
        ``reset_conversation``), egress (``deploy_artifact``, ``browser``,
        ``ops_mission_control_api``) and the ``artifact_*`` writes.

        Spelled as literals, deliberately: deriving the expectation from
        ``agent._CONDUCTOR_CORE_GRANTS`` would let the tuple and its test drift
        together and assert nothing.
        """
        granted = self._install(tmp_path, monkeypatch)["allowedTools"]
        core = {g for g in granted if g.startswith("@kirocrew-core")}
        assert core == {
            "@kirocrew-core/monitor_start",
            "@kirocrew-core/monitor_update",
            "@kirocrew-core/autonudge_stop",
            "@kirocrew-core/wait",
            "@kirocrew-core/resource_status",
            "@kirocrew-core/list_sessions",
            "@kirocrew-core/session_ledger_read",
            "@kirocrew-core/session_ledger_record",
            "@kirocrew-core/skill_search",
            "@kirocrew-core/skill_fetch",
            "@kirocrew-core/select_crew",
            "@kirocrew-core/send_message",
            "@kirocrew-core/send_notification",
            "@kirocrew-core/ask_question",
        }
        # The bare server is what this test exists to keep out: it would re-grant
        # all 74 registered core tools, including every verb named below.
        assert "@kirocrew-core" not in granted
        for denied in (
            # Persistent work — the three the prompt PROHIBITS by name.
            "task_run",
            "workflow_run",
            "workflow_author",
            "workflow_rerun_subtree",
            "workflow_cancel",
            # Agent fan-out with no human in the loop.
            "spawn_run",
            "spawn_sub_agents",
            "spawn_steer",
            "spawn_continue",
            # Mutating the caller's own session or project out from under it.
            "set_project",
            "reset_conversation",
            # Egress and machine reach.
            "deploy_artifact",
            "browser",
            "ops_mission_control_api",
            "file_send",
            # Artifact and memory writes.
            "artifact_save",
            "artifact_update",
            "artifact_delete",
            "learn_add",
            "knowledge_add_document",
        ):
            assert f"@kirocrew-core/{denied}" not in granted, denied
        # And the projected KAS policy must deny them too, not just the kiro-cli
        # allowlist: a ``kirocrew-core/*`` pattern here would re-grant every one
        # of them on the KAS backend while the list above still looked narrow.
        match = self._install(tmp_path, monkeypatch)["permissions"]["rules"][0]["match"]
        assert "kirocrew-core/*" not in match
        for denied in ("task_run", "spawn_run", "set_project", "browser", "artifact_save"):
            assert f"kirocrew-core/{denied}" not in match, denied

    def test_template_grants_never_leak_into_the_conductor_list(self, tmp_path, monkeypatch):
        """No-op proof: the conductor replaces ``allowedTools``
        wholesale with its own filtered ``granted`` list, so the ceiling filter
        moving into ``build_agent_config`` changes nothing here — and template
        grants (filtered or not) can never leak through.
        """
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(
            agent,
            "build_agent_config",
            lambda: {
                "name": "kirocrew",
                "prompt": "file://x",
                "mcpServers": {
                    "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]}
                },
                "tools": ["fs_read", "@kirocrew-core"],
                # A template list carrying floor builtins — as if the build-time
                # filter did not exist. None of it may survive the replacement.
                "allowedTools": ["fs_read", "code", "glob", "grep", "@kirocrew-core"],
            },
        )
        monkeypatch.setattr(
            agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
        )
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)
        agent._install_conductor_agent()
        data = json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
        for template_grant in ("fs_read", "code", "glob", "grep"):
            assert template_grant not in data["allowedTools"], template_grant

    def test_mounts_no_tool_the_charter_never_names(self, tmp_path, monkeypatch):
        """An unused grant is surface the charter cannot account for.

        `web_fetch` serves the skill's worked example (reading an issue list
        during triage). `web_search`, `grep` and `glob` appeared in neither the
        prompt nor the skill and were dropped — `fs_read` covers every read the
        charter describes. `tool_search` stays and is now NAMED in the prompt:
        with MCP Tool Search active the session-control specs are deferred, so
        without it the conductor cannot reach `session_create` at all.
        """
        data = self._install(tmp_path, monkeypatch)
        for absent in ("web_search", "grep", "glob"):
            assert absent not in data["tools"], absent
        assert "web_fetch" in data["tools"]
        assert "tool_search" in data["tools"]
        assert "tool_search" in data["prompt"]

    def test_mounts_no_tool_that_can_write_a_file(self, tmp_path, monkeypatch):
        """The no-write property must hold against the tool list, not the prose.

        `fs_write` is the obvious one, but `code` is the trap: governance maps it
        to `filesystem.write` because it "writes files AND can shell out"
        (`platform/governance.py` BUILTIN_TOOL_SCOPES), and it sits in
        `WITHHELD_FROM_AUTO_APPROVE` for the same reason. Mounting it would make
        "never does a work item's work itself" false while the docstring still
        claimed it, so both are pinned here together.
        """
        data = self._install(tmp_path, monkeypatch)
        for writer in ("fs_write", "code"):
            assert writer not in data["tools"], writer

    def test_mcp_surface_is_core_plus_dashboard_plus_work(self, tmp_path, monkeypatch):
        """Exactly three servers, and inherited ones the charter has no use for go.

        ``kirocrew-work`` is one of them now: this spec IS the ledger conductor,
        so the mount is the charter rather than an addition to it. Pinned
        positively AND as an exact set, so a fourth inherited server cannot ride
        in unnoticed.
        """
        data = self._install(tmp_path, monkeypatch)
        assert set(data["mcpServers"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
            "kirocrew-work",
        }
        assert data["mcpServers"]["kirocrew-dashboard"]["args"] == ["mcp-dashboard"]
        assert data["mcpServers"]["kirocrew-work"]["args"] == ["mcp-work"]
        assert "builder-mcp" not in data["mcpServers"]
        # Never auto-approved at the SERVER level: an autoApproved MCP tool is
        # approved inside kiro-cli and emits no permission request, so
        # ``hooks.on_tool_call`` — the deny floor, the sensitive-path check, the
        # governance ceiling — is never reached for it.
        assert "autoApprove" not in data["mcpServers"]["kirocrew-work"]

    def test_the_work_mount_lands_on_all_four_surfaces(self, tmp_path, monkeypatch):
        """A mount is four separate facts, and only three of them are visible.

        ``tools`` mounts the server, ``mcpServers`` gives it a command to launch,
        ``allowedTools`` decides which verbs skip the approval prompt, and
        ``permissions.rules[].match`` is what the KAS backend reads INSTEAD of
        ``allowedTools``. The last one is the one that silently survives an
        incomplete edit: a spec can look correctly mounted on kiro-cli while the
        KAS projection grants nothing, and every patrol cycle then stalls on an
        approval nobody is there to give.
        """
        data = self._install(tmp_path, monkeypatch)
        assert "@kirocrew-work" in data["tools"]
        assert "kirocrew-work" in data["mcpServers"]
        assert "@kirocrew-work/work_ledger_read" in data["allowedTools"]
        assert "@kirocrew-work/work_ledger_record" in data["allowedTools"]
        assert "@kirocrew-work/work_brief" in data["allowedTools"]
        match = data["permissions"]["rules"][0]["match"]
        assert "kirocrew-work/work_ledger_read" in match
        assert "kirocrew-work/work_ledger_record" in match
        assert "kirocrew-work/work_brief" in match
        # ``work_report`` WRITES into a parent's record across a dispatch
        # relationship, so it is mounted and gated on every surface that grants.
        assert "@kirocrew-work/work_report" not in data["allowedTools"]
        assert "kirocrew-work/work_report" not in match
        # And never the whole server, which would hand over the worker half by
        # the back door on either backend.
        assert "@kirocrew-work" not in data["allowedTools"]
        assert "kirocrew-work/*" not in match

    def test_prompt_carries_the_work_ledger_procedure(self, tmp_path, monkeypatch):
        """The charter has to describe the flow the mount makes reachable.

        The four rules that make this procedure what it is: bind before seed, a
        ``done`` is a claim the evaluator settles, the batch is filtered to
        ``done`` before it is piped, and a claimed ``pr`` is promoted by an
        explicit ``action=accept`` rather than read as the bar.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        for token in (
            "work_ledger_read",
            "work_ledger_record",
            "work_brief",
            "work_report",
            "@kirocrew-work",
            "action=create",
            "action=bind",
            "action=verdict",
            "action=accept",
            "action=close",
            "Bind before you seed",
            "accept_batch",
            "CLAIM",
        ):
            assert token in prompt, token
        # In dispatch order, not merely all present.
        assert prompt.index("action=create") < prompt.index("session_create")
        assert prompt.index("session_create") < prompt.index("action=bind")
        assert prompt.index("action=bind") < prompt.index("session_send")
        # The codec this flow replaced must not be named: there is a store now,
        # and two records that can disagree is what the ledger removed.
        assert "ledger_entry" not in prompt

    def test_prompt_and_skill_filter_the_batch_to_done_items(self, tmp_path, monkeypatch):
        """``accept_batch`` is status-blind by design (it is the promotion seam), so
        the procedure must filter it. Unfiltered, a ``progress`` worker whose stub
        already satisfies a ``file`` condition earns a genuine ``pass`` and can be
        closed under itself. Both the prompt and the skill have to state the
        filter, because the agent copies whichever it read last.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        assert "Filter the returned" in prompt
        assert "whose status is `done`" in prompt
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "keep only the entries whose item is currently `status: done`" in body
        assert "Never pipe the unfiltered document" in body

    def test_prompt_and_skill_make_a_nested_conductor_report_upward(self, tmp_path, monkeypatch):
        """A second-level conductor is bound as its parent's worker, and the parent
        reads its OWN ledger — so a nested conductor that never calls
        ``work_report`` leaves its parent's item statusless forever, which the
        parent reads as a stall. The worker half is mounted on this spec for
        exactly this caller; the text has to tell it to use it. The root case is
        named too, so a root conductor does not read ``not_bound`` as a failure.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        assert "If a conductor dispatched you" in prompt
        assert "`work_brief` before you plan" in prompt
        assert "`work_report` `status: progress`" in prompt
        assert "not_bound" in prompt
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "When a conductor dispatched you" in body
        assert "`work_brief` before Round 0" in body
        assert "`work_report` at round boundaries" in body
        assert "root conductor gets `not_bound`" in body

    def test_prompt_notes_the_patrol_gate_is_still_a_timer(self, tmp_path, monkeypatch):
        """``monitor_start`` gates on one pull-request URL and nothing else today, so
        a cycle fires whether or not anything was reported. The prompt says so, and
        says what to switch to, rather than implying a gate that does not exist.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        assert "monitor_start" in prompt
        assert 'watch: "work-ledger"' in prompt

    def test_prompt_names_its_own_skill_and_not_the_deprecated_alias(self, tmp_path, monkeypatch):
        """The procedure lives in ``goal-conductor``. ``goal-ledger-conductor`` is a
        deprecation pointer kept for one release, so naming it here would send the
        agent to a paragraph instead of to the procedure.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        assert "`goal-conductor`" in prompt
        assert "goal-ledger-conductor" not in prompt

    def test_skill_body_matches_the_prompt_on_the_load_bearing_rules(self):
        """The prompt is the summary and the skill is the procedure; a disagreement
        between them is resolved nondeterministically by whichever the model weighs
        more. These four are the rules that make this flow what it is.
        """
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "Bind BEFORE you seed" in body
        assert "Never leave `agent` unset" in body
        assert "work_ledger_read` first, every cycle" in body
        assert "action=accept" in body

    def test_prompt_and_skill_close_a_child_once_its_item_is_terminal(self, tmp_path, monkeypatch):
        """A conductor that stops an item's loop and leaves its session open leaves a
        finished worker parked in the sidebar with nothing to re-arm. The prompt
        names the verb in the child-session tool line, and the skill ties it to
        ``action=close`` so ending the item and ending its session are one step.
        """
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        assert "`session_close` (close a child once its item is terminal)" in prompt
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "Closing the item and closing its session happen together" in body
        assert "`session_close` archives (reopenable); it never deletes" in body
        assert "`session_close` each one whose item is terminal" in body
        assert (
            "leave open any child still holding a pending human question or driving an"
            " unmerged PR" in body
        )

    def test_skill_feeds_the_evaluator_through_a_quoted_heredoc(self):
        """The acceptance document is built from ingested text, and the skill's
        example is what the agent copies. A ``printf '%s' '<json>'`` form ends its
        string at the first single quote inside a path and hands the remainder to
        the shell, which ``execute_bash`` then runs after one approval. A quoted
        heredoc is the one form the shell copies to stdin without interpreting.
        """
        body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "<<'ACCEPT_BATCH'" in body
        assert "printf '%s' '<" not in body
        assert "printf '%s' '{" not in body

    def test_dashboard_entry_omits_managed_metadata_on_a_default_install(
        self, tmp_path, monkeypatch
    ):
        """Neither helper contributes by default, so the emitted entry stays minimal.

        Pinned explicitly rather than read off the ambient environment: the
        repo-root conftest pins ``KIROCREW_HOME`` for every test, so the real
        ``_managed_mcp_env()`` legitimately returns a pin in-suite.
        """
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: False)
        monkeypatch.setattr(agent, "_managed_mcp_env", dict)
        dash = self._install(tmp_path, monkeypatch)["mcpServers"]["kirocrew-dashboard"]
        assert dash == {"command": "/resolved/kirocrew", "args": ["mcp-dashboard"]}

    def test_dashboard_entry_carries_managed_server_metadata(self, tmp_path, monkeypatch):
        """The hand-built dashboard entry is enriched like every managed server.

        Without ``"type": "registry"`` a registry-mode client silently drops the
        entry, so the conductor's session-control tools never launch. Without the
        ``KIROCREW_HOME`` pin the shim reads the default data home while the
        gateway runs under an override, so session control acts on a different
        session store than it reports on.
        """
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: True)
        monkeypatch.setattr(agent, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/tmp/override"})
        data = self._install(tmp_path, monkeypatch)
        dash = data["mcpServers"]["kirocrew-dashboard"]
        assert dash["type"] == "registry"
        assert dash["env"] == {"KIROCREW_HOME": "/tmp/override"}

    def test_grants_pass_through_the_governance_ceiling(self, tmp_path, monkeypatch):
        """``allowedTools`` never reaches the PreToolUse gate, so it is filtered.

        A ceiling with an opinion about a granted ref must not be silently
        bypassed by a static grant list: the server stays MOUNTED (still in
        ``tools``) but the governed VERB loses its auto-approve, so its calls
        prompt and the gate applies the real per-tool rule.

        Governs one core verb rather than the whole server, which is what the
        ceiling now has to be able to reach: after the per-verb narrowing there is
        no bare ``@kirocrew-core`` entry left for it to have an opinion about.
        """
        data = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core/monitor_start",
        )
        assert "@kirocrew-core" in data["tools"]
        assert "@kirocrew-core" not in data["allowedTools"]
        assert "@kirocrew-core/monitor_start" not in data["allowedTools"]
        # Every sibling grant survives — the ceiling removed one verb, not the set.
        assert "@kirocrew-core/monitor_update" in data["allowedTools"]
        assert data["allowedTools"] == [
            "session",
            "report",
            "tool_search",
            "@kirocrew-core/monitor_update",
            "@kirocrew-core/autonudge_stop",
            "@kirocrew-core/wait",
            "@kirocrew-core/resource_status",
            "@kirocrew-core/list_sessions",
            "@kirocrew-core/session_ledger_read",
            "@kirocrew-core/session_ledger_record",
            "@kirocrew-core/skill_search",
            "@kirocrew-core/skill_fetch",
            "@kirocrew-core/select_crew",
            "@kirocrew-core/send_message",
            "@kirocrew-core/send_notification",
            "@kirocrew-core/ask_question",
            "@kirocrew-dashboard/chat_folder_tree",
            "@kirocrew-dashboard/chat_folder_create",
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/session_read_message",
            "@kirocrew-work/work_ledger_read",
            "@kirocrew-work/work_ledger_record",
            "@kirocrew-work/work_brief",
        ]

    def test_kas_permissions_are_derived_from_the_filtered_grants(self, tmp_path, monkeypatch):
        """The KAS block is derived, not restated — so the ceiling reaches it too.

        Ungoverned: the per-tool grants project to EXACT ``server/tool`` resources
        rather than ``kirocrew-core/*`` / ``kirocrew-dashboard/*`` wildcards, which
        is what makes the narrowing real on the KAS backend as well as on kiro-cli
        — a wildcard here would re-grant the ``task_run`` / ``spawn_run`` and the
        ``session_stop`` / ``session_send`` that ``allowedTools`` deliberately
        withholds. Governed against one core verb: that pattern is gone while
        every sibling survives, which is the property a rule list restated as a
        literal would have lost.
        """
        core_resources = [
            "kirocrew-core/ask_question",
            "kirocrew-core/autonudge_stop",
            "kirocrew-core/list_sessions",
            "kirocrew-core/monitor_start",
            "kirocrew-core/monitor_update",
            "kirocrew-core/resource_status",
            "kirocrew-core/select_crew",
            "kirocrew-core/send_message",
            "kirocrew-core/send_notification",
            "kirocrew-core/session_ledger_read",
            "kirocrew-core/session_ledger_record",
            "kirocrew-core/skill_fetch",
            "kirocrew-core/skill_search",
            "kirocrew-core/wait",
        ]
        dashboard_resources = [
            "kirocrew-dashboard/chat_folder_create",
            "kirocrew-dashboard/chat_folder_tree",
            "kirocrew-dashboard/session_create",
            "kirocrew-dashboard/session_read_message",
        ]
        work_resources = [
            "kirocrew-work/work_brief",
            "kirocrew-work/work_ledger_read",
            "kirocrew-work/work_ledger_record",
        ]
        data = self._install(tmp_path, monkeypatch)
        assert data["permissions"] == {
            "rules": [
                {
                    "capability": "mcp",
                    "match": [*core_resources, *dashboard_resources, *work_resources],
                    "effect": "allow",
                }
            ]
        }
        assert "kirocrew-core/*" not in data["permissions"]["rules"][0]["match"]
        assert "kirocrew-dashboard/*" not in data["permissions"]["rules"][0]["match"]
        # The work server is narrowed the same way, and for a sharper reason: a
        # ``kirocrew-work/*`` wildcard would re-grant ``work_report`` — the one
        # verb that writes across a dispatch relationship — on the backend where
        # nobody reads ``allowedTools``.
        assert "kirocrew-work/*" not in data["permissions"]["rules"][0]["match"]
        assert "kirocrew-work/work_report" not in data["permissions"]["rules"][0]["match"]

        governed = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core/monitor_start",
        )
        assert governed["permissions"] == {
            "rules": [
                {
                    "capability": "mcp",
                    "match": [
                        *(r for r in core_resources if r != "kirocrew-core/monitor_start"),
                        *dashboard_resources,
                        *work_resources,
                    ],
                    "effect": "allow",
                }
            ]
        }

    def test_a_fully_governed_host_still_emits_the_permissions_key(self, tmp_path, monkeypatch):
        """``{"rules": []}`` when nothing qualifies — the key's PRESENCE loads the spec.

        Split from the test above once the dashboard grant meant a ceiling on one
        ref alone does not empty the rule list: without this, the empty-policy
        branch would have silently lost its coverage.
        """
        data = self._install(tmp_path, monkeypatch, may_auto_approve=lambda ref: False)
        assert data["permissions"] == {"rules": []}
        assert data["allowedTools"] == []

    def test_withholding_a_grant_is_audit_logged(self, tmp_path, monkeypatch):
        """A withheld grant is a permission DECISION and must leave a record.

        Every other writer of an ``allowedTools`` list emits this event;
        ``strip_ungoverned_auto_approve`` names a silent pop as the one withhold
        path with no audit trail. Filtering silently here would make this
        installer exactly that path.

        The withheld ref must be named in the record, which after the per-verb
        narrowing means the VERB and not just the server — an operator reading
        "@kirocrew-core was withheld" could not tell which of 14 grants he lost.
        """
        calls: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        self._install(
            tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-core/select_crew"
        )
        withheld = [c for c in calls if c.get("operation") == "mcp_auto_approve_withheld"]
        assert len(withheld) == 1
        assert "@kirocrew-core/select_crew" in withheld[0]["resources"]
        assert withheld[0]["source"] == "_install_conductor_agent"

    def test_no_audit_event_when_nothing_is_withheld(self, tmp_path, monkeypatch):
        """An ungoverned host withholds nothing, so it records no decision."""
        calls: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        self._install(tmp_path, monkeypatch)
        assert [c for c in calls if c.get("operation") == "mcp_auto_approve_withheld"] == []

    def test_audit_failure_does_not_break_the_install(self, tmp_path, monkeypatch):
        """The spec still lands when the audit sink is unavailable."""

        class _Broken:
            def log_api_access(self, **kw):
                raise RuntimeError("sel down")

        monkeypatch.setattr(agent, "sel", lambda: _Broken())
        data = self._install(
            tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-core/select_crew"
        )
        assert "@kirocrew-core/select_crew" not in data["allowedTools"]
        assert data["allowedTools"] == [
            "session",
            "report",
            "tool_search",
            "@kirocrew-core/monitor_start",
            "@kirocrew-core/monitor_update",
            "@kirocrew-core/autonudge_stop",
            "@kirocrew-core/wait",
            "@kirocrew-core/resource_status",
            "@kirocrew-core/list_sessions",
            "@kirocrew-core/session_ledger_read",
            "@kirocrew-core/session_ledger_record",
            "@kirocrew-core/skill_search",
            "@kirocrew-core/skill_fetch",
            "@kirocrew-core/send_message",
            "@kirocrew-core/send_notification",
            "@kirocrew-core/ask_question",
            "@kirocrew-dashboard/chat_folder_tree",
            "@kirocrew-dashboard/chat_folder_create",
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/session_read_message",
            "@kirocrew-work/work_ledger_read",
            "@kirocrew-work/work_ledger_record",
            "@kirocrew-work/work_brief",
        ]

    def test_skill_gates_the_plan_once_instead_of_interrogating(self):
        """The opening round must be ONE plan message, not a round of questions.

        The first live run opened with a clarification round: the previous
        wording ("restate the plan, wait for the user") left room for one ahead
        of the plan, and every question spent there is latency on work the
        conductor could have assumed and let the user correct. Pinned as a doc
        ratchet because the instruction, not the code, is what would drift.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        # What the conductor can settle itself is an assumption, not a question.
        assert "Decide, do not ask" in text
        assert "Assumptions" in text
        # And a goal that already authorizes execution must not be re-gated.
        assert "Skip the gate when the user already gave one" in text

    def test_skill_states_the_real_approval_cost(self):
        """The cost note must match the spec, or patrol plans for wrong prompts.

        The granted verbs run silently while `session_send` / `session_stop` and
        the bundled scripts prompt, so a skill that claimed either "everything
        prompts" or "nothing prompts" would have the conductor sizing its nudge
        interval around approvals it does not pay — or walking into ones it does.
        """
        text = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "Reads and creates do not prompt" in text
        assert "`session_send` and `session_stop` are deliberately NOT auto-approved" in text
        assert "accept_eval.py` invocation" in text

    def test_skill_keeps_item_state_in_the_store_and_not_in_artifacts(self):
        """One record per item, in the one place the evaluator batch reads.

        Encoding items into ``session_ledger`` ``artifacts`` as well would give
        two records that can disagree, and the ledger is the one
        ``work_ledger_read`` returns and the Crew page will render. The codec
        that squeezed an item into a 2000-character ``artifacts`` value under an
        entry cap belongs to a conductor with no item store, so the skill must
        not send a reader to it. Pinned as a doc ratchet because the
        instruction, not the code, is what would drift.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "Do not encode items into `session_ledger` artifacts" in text
        assert "ledger_entry" not in text
        # ``session_ledger`` still holds the conductor's OWN goal/phase/next, so
        # the skill has to keep naming it for that.
        assert "session_ledger_record" in text

    def test_spec_is_registered_as_kirocrew_owned(self):
        """Every managed spec registers in ``OWNED_KIRO_AGENT_FILES``.

        Three consumers key off that tuple (the Playwright convergence sweep,
        ``doctor``'s dead-path repair, connection minting). Absent from it, a
        conductor spec whose resolved MCP command path dies is classified as a
        foreign file and reported as unfixable instead of being repaired.
        """
        assert CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_builtin_skill_does_not_collide_with_the_delegation_skill(self):
        """The packaged skill must NOT be named ``conductor``.

        ``conductor_skill.generate_conductor_skill`` owns
        ``<skills>/conductor/SKILL.md``, and two paths DELETE that file when
        ``agent.conductor_skill`` is false (the default): ``cli_setup`` on every
        setup run and the dashboard config handler on toggle-off. A packaged
        skill sharing the name would be erased on a stock install.
        """
        assert SKILL_DIR.is_dir()
        assert not (_BUILTIN_SKILLS_DIR / "conductor").exists()
        assert "name: goal-conductor" in (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    def test_skill_files_the_session_at_creation_not_by_a_move(self):
        """Dispatch passes ``folder`` to ``session_create``; no move step exists.

        ``session_create`` files the slot atomically at creation, which
        is what closed the create-then-move window a folder delete could land
        in. The instruction layer must not resurrect the workaround: a separate
        ``chat_folder_create`` precondition or ``chat_folder_move_session`` step
        reopens exactly the non-atomic window the tool argument removed. Pinned
        as a doc ratchet because the instruction, not the code, is what would
        drift back.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "2. `session_create`" in text, "dispatch must reach the atomic create"
        assert "`folder`" in text, "dispatch must name the folder argument at create"
        assert "chat_folder_move_session" not in text, "the move workaround must stay deleted"
        # Scoped to the dispatch STEP, not the whole document: a future
        # legitimate mention of the tool elsewhere in the skill must not fail a
        # pin whose intent is only that the precreation step stay deleted.
        assert "1. `chat_folder_create`" not in text, "no folder-precreation dispatch step"


class _proc:
    """Minimal ``CompletedProcess`` stand-in for the exec seam's two callers.

    The real thing needs an argv and encoding to construct; these three fields
    are the whole surface ``_run`` and ``_pr_is_draft`` read.
    """

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _load_evaluator():
    """Load accept_eval.py by file location, via the shared no-bytecode helper.

    The script lives inside the checked-in skill dir (no package import path);
    loading it by file location is what lets a test assert on its internals.
    ``load_skill_script`` is the residue-guarded loader — a hand-rolled
    ``exec_module`` here would drop ``__pycache__`` beside the checked-in
    script, the exact side effect ``no-test-side-effects`` forbids.
    """
    return load_skill_script("_accept_eval_under_test", SCRIPT)


class TestAcceptEvaluatorInvariant:
    """No model-authored argv may reach subprocess.

    This is the property three review rounds converged on: constraining a
    spec-supplied argv (allowlist, basename check) never closes the class,
    because the script runs as an approved wrapper and Kiro Crew's
    denied-command floor cannot see the argv it receives on stdin. The fix was
    to stop accepting one at all.
    """

    def test_pr_checks_builds_its_own_argv(self):
        """The only exec path constructs argv from narrowly-typed fields."""
        mod = _load_evaluator()
        seen = []
        mod._pr_is_draft = lambda pr, repo=None: False
        mod._run = lambda argv, cwd=None: (seen.append((argv, cwd)), ("pass", "ok"))[1]
        verdict, _ = mod._evaluate(
            {"accept": {"kind": "pr_checks", "pr": 123, "repo": "owner/name"}}
        )
        assert verdict == "pass"
        assert seen == [(["gh", "pr", "checks", "123", "--repo", "owner/name"], None)]

    def test_the_draft_probe_builds_its_own_argv_too(self):
        """The second invocation is built here as well, from the same fields.

        Both go through ``_exec``, whose ``_SELF_BUILT_COMMANDS`` guard is the
        one seam: a new invocation must not arrive with a new way past it.
        """
        mod = _load_evaluator()
        seen = []

        def _fake_exec(argv, cwd=None):
            seen.append((argv, cwd))
            return (None, _proc(stdout="false"))

        mod._exec = _fake_exec
        assert mod._pr_is_draft(123, "owner/name") is False
        assert seen == [
            (
                [
                    "gh",
                    "pr",
                    "view",
                    "123",
                    "--repo",
                    "owner/name",
                    "--json",
                    "isDraft",
                    "-q",
                    ".isDraft",
                ],
                None,
            )
        ]

    def test_run_refuses_a_command_it_did_not_build(self):
        """The internal guard fails closed if a handler ever leaks spec input."""
        mod = _load_evaluator()
        verdict, evidence = mod._run(["git", "--version"])
        assert verdict == "refused"
        assert "not a command this script builds" in evidence
        assert mod._SELF_BUILT_COMMANDS == {"gh"}

    def test_the_draft_probe_refuses_a_command_it_did_not_build(self):
        """Same guard, reached through the probe's own seam."""
        mod = _load_evaluator()
        problem, proc = mod._exec(["git", "--version"])
        assert proc is None
        assert problem[0] == "refused"
        assert "not a command this script builds" in problem[1]

    def test_no_spec_field_can_name_a_command(self):
        """Source ratchet: no handler may read an argv/command/shell spec field.

        A behavioural test only covers the kinds that exist today; this one fails
        if a future kind re-introduces the shape, which is the actual regression
        to prevent.
        """
        src = SCRIPT.read_text(encoding="utf-8")
        for banned in ('accept.get("argv")', 'accept.get("command")', 'accept.get("shell")'):
            assert banned not in src, f"a spec field must never name a command: {banned}"

    def test_pr_checks_rejects_a_non_integer_pr(self):
        """Including bool, which is an int subclass and would render as 'True'."""
        mod = _load_evaluator()
        for bad in ("123", True, None, 12.5):
            verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": bad}})
            assert verdict == "error", bad
            assert "integer pr" in evidence


class TestAcceptEvaluator:
    def _run(self, items):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"items": items}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return {r["id"]: r for r in json.loads(proc.stdout)["results"]}

    def test_verdict_vocabulary_across_kinds(self, tmp_path):
        exists = tmp_path / "made"
        exists.write_text("x", encoding="utf-8")
        out = self._run(
            [
                {"id": "have", "accept": {"kind": "file", "path": str(exists), "exists": True}},
                {
                    "id": "miss",
                    "accept": {"kind": "file", "path": str(tmp_path / "no"), "exists": True},
                },
                {"id": "human", "accept": {"kind": "human_approval"}},
                {"id": "junk", "accept": {"kind": "wat"}},
                {"id": "nopath", "accept": {"kind": "file"}},
            ]
        )
        assert out["have"]["verdict"] == "pass"
        assert out["miss"]["verdict"] == "fail"
        assert out["human"]["verdict"] == "pending"
        assert out["junk"]["verdict"] == "error"
        assert out["nopath"]["verdict"] == "error"

    def test_file_rejects_a_non_boolean_exists(self, tmp_path):
        """``bool()`` coercion would read ``{"exists": "false"}`` as ``True``.

        The reported shape: an acceptance assembled from text carries string
        booleans, and a truthy ``"false"`` silently inverts an absence check
        into a presence check — the evaluator reports ``pass`` for exactly the
        state the spec asked to reject. ``1``/``0`` are rejected too, on
        purpose: the ``pr`` guard already refuses ``bool`` as an ``int``, so
        accepting ``int`` as a ``bool`` here would contradict the sibling
        field. A malformed spec gets a loud ``error``, never a coerced verdict.
        """
        mod = _load_evaluator()
        present = tmp_path / "made"
        present.write_text("x", encoding="utf-8")
        for bad in ("false", "true", 1, 0, None):
            verdict, evidence = mod._evaluate(
                {"accept": {"kind": "file", "path": str(present), "exists": bad}}
            )
            assert verdict == "error", bad
            assert "boolean exists" in evidence

    def test_file_absent_exists_key_still_defaults_to_presence_check(self, tmp_path):
        """The default is unchanged: no ``exists`` key means ``exists: true``."""
        mod = _load_evaluator()
        present = tmp_path / "made"
        present.write_text("x", encoding="utf-8")
        verdict, _ = mod._evaluate({"accept": {"kind": "file", "path": str(present)}})
        assert verdict == "pass"
        verdict, _ = mod._evaluate({"accept": {"kind": "file", "path": str(tmp_path / "no")}})
        assert verdict == "fail"

    def test_file_real_booleans_still_work(self, tmp_path):
        """Regression floor: genuine ``True``/``False`` keep their semantics."""
        mod = _load_evaluator()
        present = tmp_path / "made"
        present.write_text("x", encoding="utf-8")
        missing = tmp_path / "no"
        cases = [
            (present, True, "pass"),
            (present, False, "fail"),
            (missing, True, "fail"),
            (missing, False, "pass"),
        ]
        for path, want, expected in cases:
            verdict, _ = mod._evaluate(
                {"accept": {"kind": "file", "path": str(path), "exists": want}}
            )
            assert verdict == expected, (path.name, want)

    def test_the_cmd_kind_is_refused_and_says_what_to_use(self):
        """A conductor carrying an older skill gets guidance, not 'unknown kind'.

        `cmd` is a removed kind, so it is named explicitly: the refusal
        points at `pr_checks` and notes it already covers "the tests pass",
        since CI runs them.
        """
        out = self._run(
            [{"id": "old", "accept": {"kind": "cmd", "argv": ["git", "reset", "--hard"]}}]
        )
        assert out["old"]["verdict"] == "refused"
        assert "may not name a command" in out["old"]["evidence"]
        assert "pr_checks" in out["old"]["evidence"]

    def test_a_non_object_item_does_not_abort_the_run(self):
        """ID extraction is inside the per-item guard.

        ``{"items": [1, {...}]}`` raises on ``.get`` before the handler runs; if
        that raise escaped, every sibling verdict would be lost.
        """
        out = self._run([1, "nope", {"id": "fine", "accept": {"kind": "human_approval"}}])
        assert out["fine"]["verdict"] == "pending"
        assert out["#0"]["verdict"] == "error"
        assert out["#1"]["verdict"] == "error"
        assert "JSON object" in out["#0"]["evidence"]

    def test_malformed_stdin_is_a_clean_exit_2(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert proc.returncode == 2


class TestDraftPullRequest:
    """A draft PR whose checks stay PENDING is ``refused``, and only then.

    Two halves, and the order between them is the whole design. A draft is not
    unconditionally unsatisfiable: without an aggregate check gated on the draft
    flag, a draft's checks resolve and ``gh pr checks`` exits 0. What cannot
    resolve is pending BECAUSE the PR is a draft - so the probe explains a
    pending verdict rather than pre-empting the check, which also keeps the pass
    and fail paths free of it.
    """

    def _wire(self, mod, monkeypatch, script):
        """Feed ``_exec`` scripted results, newest call last; record the argvs."""
        seen = []

        def _fake_run(argv, **kwargs):
            seen.append(list(argv))
            return _proc(*script[len(seen) - 1])

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        return seen

    def test_a_pending_draft_is_refused(self, monkeypatch):
        mod = _load_evaluator()
        seen = self._wire(mod, monkeypatch, [(8, "3 checks still running", ""), (0, "true\n", "")])
        verdict, evidence = mod._evaluate(
            {"accept": {"kind": "pr_checks", "pr": 123, "repo": "owner/name"}}
        )
        assert verdict == "refused"
        assert "PR #123 is a draft" in evidence
        assert "have not finished" in evidence
        assert "mark it ready for review" in evidence
        # The check ran first; the probe only explained its pending.
        assert seen[0] == ["gh", "pr", "checks", "123", "--repo", "owner/name"]
        assert seen[1][:4] == ["gh", "pr", "view", "123"]

    def test_a_draft_whose_checks_are_still_running_is_refused_too(self, monkeypatch):
        """The deliberate scope, pinned so it reads as a decision.

        A stateless evaluator cannot tell "pending because a readiness check is
        gated on the draft flag" (never resolves) from "pending because CI is
        still running" (resolves), and this returns the same ``refused`` for
        both. The ground is the spec, not a prediction about CI: a draft is the
        author's own "not ready for review", so an unfinished check run on one
        is a person's turn. "Mark it ready for review" is bounded and owed
        anyway; waiting is the reported defect.
        """
        mod = _load_evaluator()
        seen = self._wire(
            mod,
            monkeypatch,
            [(8, "Backend Tests (1)\tpending\t0s\nE2E\tpending\t0s", ""), (0, "true", "")],
        )
        verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 55}})
        assert verdict == "refused"
        assert "have not finished" in evidence
        assert "not a review-ready PR" in evidence
        assert len(seen) == 2

    def test_the_refusal_does_not_predict_what_ci_will_do(self):
        """Source ratchet on the evidence a human reads.

        Two earlier wordings claimed more than this script can know - that a
        draft "cannot pass", and that its checks are "still pending" as though
        the evaluator had established they would stay that way. The shipped
        sentence must rest on the draft flag, which is observed.
        """
        src = SCRIPT.read_text(encoding="utf-8")
        assert "cannot pass while draft" not in src
        assert "is not a review-ready PR" in src

    def test_a_green_draft_still_passes_and_is_never_probed(self, monkeypatch):
        """The regression an unconditional probe would ship.

        This script is a builtin skill pointed at whatever repo a work item
        names. On a repo with no draft-gated aggregate check, a draft's checks
        complete and exit 0 - the acceptance is genuinely met, and refusing it
        would escalate a satisfied condition to a human. Only the pending loop
        was ever the defect.
        """
        mod = _load_evaluator()
        seen = self._wire(mod, monkeypatch, [(0, "all checks pass", "")])
        verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 123}})
        assert verdict == "pass"
        assert "all checks pass" in evidence
        assert len(seen) == 1, "a passing check must cost no draft probe"

    def test_a_failing_check_is_never_probed_either(self, monkeypatch):
        """``fail`` is already terminal; the draft flag cannot change it."""
        mod = _load_evaluator()
        seen = self._wire(mod, monkeypatch, [(1, "2 checks failing", "")])
        verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 123}})
        assert verdict == "fail"
        assert "2 checks failing" in evidence
        assert len(seen) == 1

    def test_a_non_draft_pr_runs_the_check_unchanged(self, monkeypatch):
        mod = _load_evaluator()
        seen = self._wire(mod, monkeypatch, [(0, "all checks pass", "")])
        verdict, evidence = mod._evaluate(
            {"accept": {"kind": "pr_checks", "pr": 123, "repo": "owner/name"}}
        )
        assert verdict == "pass"
        assert "all checks pass" in evidence
        assert seen[0] == ["gh", "pr", "checks", "123", "--repo", "owner/name"]

    def test_a_non_draft_pr_still_reports_pending_while_checks_run(self, monkeypatch):
        """The wait contract for a ready-for-review PR is untouched."""
        mod = _load_evaluator()
        self._wire(mod, monkeypatch, [(8, "still running", ""), (0, "false", "")])
        verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 7}})
        assert verdict == "pending"
        assert "still running" in evidence

    def test_a_probe_that_cannot_answer_leaves_the_pending_standing(self, monkeypatch):
        """gh missing, no such PR, an auth error: the verdict stays ``pending``.

        One-sided on purpose. A probe that fails for any reason other than a
        clean ``true`` must not turn a wait into a ``refused`` the conductor has
        to escalate to a human.
        """
        mod = _load_evaluator()
        for probe in [(1, "", "no pull requests found"), (0, "", ""), (0, "null", "")]:
            self._wire(mod, monkeypatch, [(8, "still running", ""), probe])
            verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 9}})
            assert verdict == "pending", probe
            assert "still running" in evidence, probe

    def test_the_probe_omits_repo_when_the_spec_does_not_name_one(self, monkeypatch):
        mod = _load_evaluator()
        seen = self._wire(mod, monkeypatch, [(8, "still running", ""), (0, "false", "")])
        mod._evaluate({"accept": {"kind": "pr_checks", "pr": 9}})
        assert seen[1] == ["gh", "pr", "view", "9", "--json", "isDraft", "-q", ".isDraft"]

    def test_a_malformed_pr_is_rejected_before_any_subprocess(self, monkeypatch):
        """The integer guard still runs first; a bad spec spends no subprocess."""
        mod = _load_evaluator()
        seen = self._wire(mod, monkeypatch, [(0, "true", "")])
        verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": "123"}})
        assert verdict == "error"
        assert "integer pr" in evidence
        assert seen == []


class TestUsageInsteadOfBlockingOnStdin:
    """No input means print usage and exit 2, never a read that hangs.

    Run on a terminal or with ``--help``, the ``json.load(sys.stdin)`` below
    blocks until the caller's tool timeout: an approval spent, no output, and
    nothing that says the input goes on stdin.
    """

    def test_help_exits_2_with_usage_on_stderr(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
        assert "Usage:" in proc.stderr
        assert "accept_eval.py < items.json" in proc.stderr
        # Usage, not the module's security essay.
        assert "THE SECURITY INVARIANT" not in proc.stderr

    def test_help_runs_no_subprocess_and_reads_no_stdin(self, monkeypatch):
        mod = _load_evaluator()

        def _forbidden(*args, **kwargs):
            raise AssertionError("--help must not run a subprocess")

        monkeypatch.setattr(mod.subprocess, "run", _forbidden)
        stdin = _RefusingStdin()
        monkeypatch.setattr(mod.sys, "stdin", stdin)
        for flag in ("-h", "--help"):
            monkeypatch.setattr(mod.sys, "argv", ["accept_eval.py", flag])
            assert mod.main() == 2
        assert stdin.read_calls == 0

    def test_a_tty_stdin_prints_usage_instead_of_reading(self, monkeypatch):
        mod = _load_evaluator()
        stdin = _RefusingStdin(tty=True)
        monkeypatch.setattr(mod.sys, "stdin", stdin)
        monkeypatch.setattr(mod.sys, "argv", ["accept_eval.py"])
        assert mod.main() == 2
        assert stdin.read_calls == 0

    def test_a_stdin_that_cannot_answer_isatty_is_not_treated_as_a_tty(self, monkeypatch):
        """Piped input must keep working when ``isatty`` raises (closed stdin)."""
        mod = _load_evaluator()

        class _Broken:
            def isatty(self):
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(mod.sys, "stdin", _Broken())
        assert mod._stdin_is_a_tty() is False

    def test_piped_input_keeps_its_exit_codes(self):
        """The contract for real input is unchanged: 0 evaluated, 2 malformed."""
        for payload, expected in (('{"items": []}', 0), ("not json", 2)):
            proc = subprocess.run(
                [sys.executable, str(SCRIPT)],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            assert proc.returncode == expected, payload

    def test_main_takes_no_injected_argv(self):
        """``main`` reads ``sys.argv`` like the entry point that calls it.

        An ``argv`` parameter had zero non-test consumers - the tests were its
        only caller, and a parameter that exists to be passed by its own tests
        is a seam the shipped script does not have.
        """
        mod = _load_evaluator()
        assert list(inspect.signature(mod.main).parameters) == []

    def test_usage_is_sliced_from_the_docstring_not_duplicated(self):
        """One source for the contract, so help cannot drift from the module."""
        mod = _load_evaluator()
        usage = mod._usage_text()
        assert usage in (mod.__doc__ or "")
        # The anchors the slice depends on must both stay present, and the
        # opening one must stay unique inside the docstring or the slice moves.
        doc = mod.__doc__ or ""
        assert doc.count("Usage:") == 1
        assert "THE SECURITY INVARIANT" in doc


class _RefusingStdin:
    """A stdin that counts reads. ``isatty`` is configurable.

    It returns "" rather than raising, because ``main`` catches every
    ``Exception`` around ``json.load`` and would convert a raise into the same
    exit 2 the usage path returns - hiding a gate that stopped working. The
    count is what the assertion reads.
    """

    def __init__(self, tty=False):
        self._tty = tty
        self.read_calls = 0

    def isatty(self):
        return self._tty

    def read(self, *args, **kwargs):
        self.read_calls += 1
        return ""
