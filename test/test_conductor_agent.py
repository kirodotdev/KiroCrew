"""Conductor agent installer + bundled acceptance evaluator.

The installer test mirrors the research-agent installer test's shape: stub the
agents dir and ``build_agent_config``, run the installer, assert on the JSON it
wrote. The evaluator tests run the real script over stdin/stdout — it is the
deterministic half of the conductor's patrol, so its verdict vocabulary is
pinned here.

The ``cmd`` fixtures deliberately use ``git`` rather than ``sys.executable``:
bare interpreters are NOT on the evaluator's allowlist (a spec could otherwise
name ``python -c <payload>``), and pinning that is one of the tests below.
"""

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
        dies at the turn cap mid-round, which loses the loop. Both halves of the
        contract are pinned: ``monitor_start`` drives the round, and the
        conductor knows the tool it needs to stop the loop it armed. Whitespace
        is normalised so re-wrapping the prompt cannot fail this for a
        formatting reason.
        """
        data = self._install(tmp_path, monkeypatch)
        prompt = " ".join(data["prompt"].split())
        assert "Patrol with `monitor_start`, never with `wait`" in prompt
        assert "autonudge_stop" in prompt

    def test_prompt_names_the_tools_it_expects_to_be_used(self, tmp_path, monkeypatch):
        """The charter mounts whole servers, so the prompt must name what it wants.

        ``@kirocrew-core`` puts ~70 tools in front of this agent. Naming only the
        session-control ones left the rest unaddressed, which is how a conductor
        talks itself into running a work item in its own turn. Both directions are
        pinned: the tools the four jobs need, and the four that would end-run the
        no-write property by executing the work here.
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
        ``@kirocrew-dashboard`` is MOUNTED whole but never granted whole — the
        auto-approve list names verbs, so the destructive ones keep prompting.
        ``execute_bash`` is mounted and never granted, because ``allowedTools``
        has no argument matching and trusting the two bundled scripts cannot be
        told apart from trusting arbitrary shell.
        """
        data = self._install(tmp_path, monkeypatch)
        assert "fs_write" not in data["tools"]
        assert "@kirocrew-dashboard" in data["tools"]
        assert "@kirocrew-dashboard" not in data["allowedTools"]
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

    def test_template_grants_never_leak_into_the_conductor_list(self, tmp_path, monkeypatch):
        """No-op proof for #7401: the conductor replaces ``allowedTools``
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

    def test_mcp_surface_is_narrowed_to_core_plus_dashboard(self, tmp_path, monkeypatch):
        """Inherited servers the conductor has no charter for are dropped."""
        data = self._install(tmp_path, monkeypatch)
        assert set(data["mcpServers"]) == {"kirocrew-core", "kirocrew-dashboard"}
        assert data["mcpServers"]["kirocrew-dashboard"]["args"] == ["mcp-dashboard"]

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

        A ceiling with an opinion about ``@kirocrew-core`` must not be silently
        bypassed by a static grant list: the ref stays MOUNTED (still in
        ``tools``) but loses its blanket auto-approve, so its calls prompt and
        the gate applies the real per-tool rule.
        """
        data = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core",
        )
        assert "@kirocrew-core" in data["tools"]
        assert "@kirocrew-core" not in data["allowedTools"]
        assert data["allowedTools"] == [
            "session",
            "report",
            "tool_search",
            "@kirocrew-dashboard/chat_folder_tree",
            "@kirocrew-dashboard/chat_folder_create",
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/session_read_message",
        ]

    def test_kas_permissions_are_derived_from_the_filtered_grants(self, tmp_path, monkeypatch):
        """The KAS block is derived, not restated — so the ceiling reaches it too.

        Ungoverned: the per-tool dashboard grants project to EXACT ``server/tool``
        resources rather than a ``kirocrew-dashboard/*`` wildcard, which is what
        makes the narrowing real on the KAS backend as well as on kiro-cli — a
        wildcard here would re-grant the ``session_stop`` / ``session_send`` that
        ``allowedTools`` deliberately withholds. Governed against
        ``@kirocrew-core``: that pattern is gone while the dashboard ones survive,
        which is the property a rule list restated as a literal would have lost.
        """
        dashboard_resources = [
            "kirocrew-dashboard/chat_folder_create",
            "kirocrew-dashboard/chat_folder_tree",
            "kirocrew-dashboard/session_create",
            "kirocrew-dashboard/session_read_message",
        ]
        data = self._install(tmp_path, monkeypatch)
        assert data["permissions"] == {
            "rules": [
                {
                    "capability": "mcp",
                    "match": ["kirocrew-core/*", *dashboard_resources],
                    "effect": "allow",
                }
            ]
        }
        assert "kirocrew-dashboard/*" not in data["permissions"]["rules"][0]["match"]

        governed = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core",
        )
        assert governed["permissions"] == {
            "rules": [{"capability": "mcp", "match": dashboard_resources, "effect": "allow"}]
        }

    def test_a_fully_governed_host_still_emits_the_permissions_key(self, tmp_path, monkeypatch):
        """``{"rules": []}`` when nothing qualifies — the key's PRESENCE loads the spec.

        Split from the test above once the dashboard grant meant a ceiling on
        ``@kirocrew-core`` alone no longer empties the rule list: without this,
        the empty-policy branch would have silently lost its coverage.
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
        """
        calls: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        self._install(tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-core")
        withheld = [c for c in calls if c.get("operation") == "mcp_auto_approve_withheld"]
        assert len(withheld) == 1
        assert "@kirocrew-core" in withheld[0]["resources"]
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
            tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-core"
        )
        assert data["allowedTools"] == [
            "session",
            "report",
            "tool_search",
            "@kirocrew-dashboard/chat_folder_tree",
            "@kirocrew-dashboard/chat_folder_create",
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/session_read_message",
        ]

    def test_skill_gates_the_plan_once_instead_of_interrogating(self):
        """Round 0 must be ONE plan message, not a round of questions.

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
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "Reads and creates do not prompt" in text
        assert "`session_send`\n  and `session_stop` are deliberately NOT auto-approved" in text
        assert "accept_eval.py` invocation" in text

    def test_skill_documents_artifacts_as_a_string_map(self):
        """The ledger's ``artifacts`` values MUST be JSON-serialized strings.

        ``session_ledger`` handler validation rejects a non-string value with
        ``artifacts_not_string_map`` (HTTP 400), so a skill that told the
        conductor to send a nested object would lose every dispatched item's
        state — the exact durability this entry exists to provide. Pinned as a
        doc ratchet because the instruction, not the code, is what would drift.

        The format is owned by ``scripts/ledger_entry.py`` (issue #5912), so
        the skill must route encoding through the codec rather than carrying a
        hand-written byte-format example for models to re-derive — the worked
        example was the specification once, and produced real defects on
        PR #5652.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "artifacts_not_string_map" in text
        assert "map of string to STRING" in text
        # The codec is the format's one code owner: the skill must direct the
        # conductor to it for encode/decode/validate/rotate...
        assert "scripts/ledger_entry.py" in text
        assert "ledger_entry.py encode" in text
        # ...and must never show a bare `item-1 -> {` object literal, which is
        # exactly the value shape the ledger rejects.
        assert "item-1 -> {" not in text

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

        ``session_create`` files the slot atomically at creation (#6118), which
        is what closed the create-then-move window a folder delete could land
        in. The instruction layer must not resurrect the workaround: a separate
        ``chat_folder_create`` precondition or ``chat_folder_move_session`` step
        reopens exactly the non-atomic window the tool argument removed. Pinned
        as a doc ratchet because the instruction, not the code, is what would
        drift back.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "1. `session_create`" in text, "dispatch must open with the atomic create"
        assert "`folder`" in text, "dispatch must name the folder argument at create"
        assert "chat_folder_move_session" not in text, "the move workaround must stay deleted"
        # Scoped to the dispatch STEP, not the whole document: a future
        # legitimate mention of the tool elsewhere in the skill must not fail a
        # pin whose intent is only that the precreation step stay deleted.
        assert "1. `chat_folder_create`" not in text, "no folder-precreation dispatch step"


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
        mod._run = lambda argv, cwd=None: (seen.append((argv, cwd)), ("pass", "ok"))[1]
        verdict, _ = mod._evaluate(
            {"accept": {"kind": "pr_checks", "pr": 123, "repo": "owner/name"}}
        )
        assert verdict == "pass"
        assert seen == [(["gh", "pr", "checks", "123", "--repo", "owner/name"], None)]

    def test_run_refuses_a_command_it_did_not_build(self):
        """The internal guard fails closed if a handler ever leaks spec input."""
        mod = _load_evaluator()
        verdict, evidence = mod._run(["git", "--version"])
        assert verdict == "refused"
        assert "not a command this script builds" in evidence
        assert mod._SELF_BUILT_COMMANDS == {"gh"}

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

    def test_the_cmd_kind_is_refused_and_says_what_to_use(self):
        """A conductor carrying an older skill gets guidance, not 'unknown kind'.

        `cmd` used to exist, so the removal is named explicitly: the refusal
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
