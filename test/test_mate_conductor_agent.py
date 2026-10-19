# SPDX-License-Identifier: Apache-2.0
"""``kirocrew-mate-conductor`` is a source-defined, DISPATCH-ONLY conductor.

Migration step 1 of ``rfc-mate-owned-agent-config.md``: a crewmate's own chat
runs on a source-defined spec whose whole point is that it plans and dispatches
work to a worker and never does the work itself. So unlike a pure rename, the
spec is NARROWED against ``build_agent_config()``'s default surface: no
file-writing tool (``fs_write``/``code``), no shell (``execute_bash``), and no
user-configured MCP server. An unnamed ``spawn_run`` by a member bound to it
routes to ``kirocrew-worker``, not back to the write-less conductor. There is
deliberately no test here for the per-mate ``~/.kiro/mates/agents/`` file -- that
is step 3.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import MATE_CONDUCTOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_materialization import service_agents


@pytest.fixture
def installed(tmp_path, monkeypatch) -> dict:
    """The spec this installer actually writes, read back off disk."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_conductor_agent()
    return json.loads((tmp_path / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))


class TestTheInstallerWritesADispatchOnlyConductor:
    def test_the_file_carries_this_agents_name_and_a_dispatch_only_description(self, installed):
        assert installed["name"] == "kirocrew-mate-conductor"
        # The description states the dispatch-only shape AND the read-only file
        # access the team asked be noted there.
        desc = installed["description"]
        assert "dispatch-only" in desc
        assert "read-only file" in desc and "no fs_write" in desc

    def test_the_filename_is_owned_so_the_boot_sweep_covers_it(self):
        assert MATE_CONDUCTOR_AGENT_FILENAME == "kirocrew-mate-conductor.json"
        assert MATE_CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_the_prompt_is_the_source_charter_not_the_native_stub(self, installed):
        """The conductor's prompt is its own source charter, NOT the default
        template's native stub -- the behaviour change step 1 now is."""
        assert installed["prompt"] == agent._MATE_CONDUCTOR_SYSTEM_PROMPT
        assert installed["prompt"] != agent._NATIVE_PROMPT_STUB

    def test_it_has_no_file_writing_tool_and_no_shell(self, installed):
        """The dispatch-only invariant, enforced against the tool list: a
        conductor that could write a file or shell out could do the work inline,
        which is the case the tool set exists to exclude (RFC §5.0)."""
        tools = installed["tools"]
        assert "fs_read" in tools  # read-only, to size a request
        for forbidden in ("fs_write", "code", "execute_bash", "shell"):
            assert forbidden not in tools, forbidden

    def test_it_mounts_no_user_or_shell_class_mcp_server(self, installed):
        """Narrowed to the member platform + the conductor's own servers; a
        dispatch-only conductor carries no user server (those belong to the
        worker) and neither the scheduler nor the computer-use server."""
        servers = set(installed["mcpServers"])
        assert servers == {
            "kirocrew-core",
            "kirocrew-guide",
            "kirocrew-mate",
            "kirocrew-dashboard",
            "kirocrew-work",
        }

    def test_it_is_strictly_narrower_than_the_default_template(self, installed):
        """Every tool it carries is one the default template carries or a
        conductor-added server; it adds no capability the default lacks and drops
        the write/shell ones. This is the opposite of a byte-identical rename."""
        template_tools = set(agent.build_agent_config().get("tools") or [])
        conductor_added = {"@kirocrew-dashboard", "@kirocrew-work"}
        for tool in installed["tools"]:
            assert tool in template_tools or tool in conductor_added, tool
        # And it is genuinely narrower: the default's write/shell tools are gone.
        assert {"fs_write", "code", "execute_bash"} & template_tools
        assert not ({"fs_write", "code", "execute_bash"} & set(installed["tools"]))

    def test_it_can_dispatch_and_manage_its_own_record(self, installed):
        """The grants that make it a conductor: it can spawn/dispatch and manage
        its own crewmate record (the capability change card), via allowedTools."""
        allowed = set(installed.get("allowedTools") or [])
        assert "@kirocrew-core/spawn_run" in allowed
        assert "@kirocrew-mate/request_capabilities" in allowed

    def test_it_carries_the_operators_installed_permissions_deny(self, tmp_path, monkeypatch):
        """``build_agent_config()`` never reads the installed ``kirocrew.json``, so a
        KAS deny an operator hand-wrote there must be carried into the conductor
        explicitly -- else KAS would silently auto-approve it for every crewmate
        chat. The installer reads the installed default's
        ``permissions`` block and merges it in before the governed write, which
        (under ``inherit_narrowing``) keeps the ``deny``. Uses the valid KAS rules
        shape and pins the writer's version gate so the block is relayed."""
        import json as _json

        from kiro_crew.agent_files import AGENT_FILENAME
        from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

        monkeypatch.setattr(
            "kiro_crew.kiro_cli.installed_kiro_cli_version",
            lambda: SPEC_PERMISSIONS_MIN_VERSION,
        )
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        # An operator-authored kirocrew.json carrying a web_fetch deny (valid KAS
        # rules shape).
        operator_default = {
            "name": "kirocrew",
            "permissions": {"rules": [{"capability": "web_fetch", "effect": "deny"}]},
        }
        (tmp_path / AGENT_FILENAME).write_text(_json.dumps(operator_default), encoding="utf-8")

        service_agents._install_mate_conductor_agent()
        spec = _json.loads((tmp_path / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
        rules = (spec.get("permissions") or {}).get("rules") or []
        # The operator's web_fetch deny survives onto the conductor.
        assert {"capability": "web_fetch", "effect": "deny"} in rules


def test_rebuild_agent_config_installs_it_alongside_the_default(tmp_path, monkeypatch):
    """A full ``rebuild_agent_config()`` writes the mate conductor too, not just a
    direct call to the installer -- this is the self-healing boot path a crewmate's
    turn actually depends on."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent.rebuild_agent_config(clean=True)
    assert (tmp_path / MATE_CONDUCTOR_AGENT_FILENAME).exists()


class TestTheCharterPromptIsADispatchContract:
    """A prompt-contract test, the way the goal conductor's charter is pinned:
    the charter must state that the conductor dispatches and never runs a work
    item inline, and must name why (it has no write tool / no shell)."""

    def test_the_charter_says_dispatch_never_inline(self):
        prompt = agent._MATE_CONDUCTOR_SYSTEM_PROMPT
        assert "never do a work item's work yourself" in prompt.lower() or (
            "never do" in prompt.lower() and "yourself" in prompt.lower()
        )
        assert "dispatch" in prompt.lower()
        # It names the enforcement, not just the intent.
        assert "no tool that writes a file" in prompt.lower()
        assert "no shell" in prompt.lower() or "execute_bash` is not mounted" in prompt.lower()

    def test_the_conductor_is_default_for_identity_but_not_for_its_surface(self):
        """The two predicates split: the conductor is a Crew default for
        identity (workspace/model/docs) but renders its OWN surface (prompt +
        MCP list). If it were default for its surface too, the full persona
        prompt would replace its charter and the MCP panel would report servers
        it never mounts."""
        from kiro_crew.agent_files import (
            is_kirocrew_default_agent,
            renders_default_agent_surface,
        )

        # Identity: the conductor counts as the default.
        assert is_kirocrew_default_agent("kirocrew-mate-conductor")
        assert is_kirocrew_default_agent("kirocrew")
        # Surface: only the real default renders the persona/global list.
        assert renders_default_agent_surface("kirocrew")
        assert not renders_default_agent_surface("kirocrew-mate-conductor")
        assert not renders_default_agent_surface("code-reviewer")

    def test_the_installed_conductor_resolves_to_its_charter_not_the_persona(
        self, tmp_path, monkeypatch
    ):
        """The installed spec's prompt resolves to the dispatch-only charter, NOT
        the shared Kiro Crew persona (``prompt.md``). ``_resolve_agent_prompt``
        treats the conductor as custom-for-prompt via ``renders_default_agent_surface``
        and loads the spec's own ``prompt`` -- the charter -- so a crewmate chat
        gets the dispatch contract, not a full-surface persona."""
        from kiro_crew import agent as agent_mod
        from kiro_crew.context_assembly import agent_spec as aspec

        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", tmp_path)
        monkeypatch.setattr(
            "kiro_crew.member_essential_context.resolve_template_path",
            lambda a, p=None: (
                (tmp_path / MATE_CONDUCTOR_AGENT_FILENAME)
                if a == "kirocrew-mate-conductor"
                else None
            ),
        )
        service_agents._install_mate_conductor_agent()

        loaded = aspec._load_agent_prompt("kirocrew-mate-conductor", None)
        # The charter is what loads: it names the dispatch-only contract.
        assert "dispatch" in loaded.lower()
        assert loaded == agent_mod._MATE_CONDUCTOR_SYSTEM_PROMPT


class TestWorkerRoute:
    """A mate-conductor-bound member's unnamed ``spawn_run`` must run
    ``kirocrew-worker``, not the write-less conductor itself."""

    def test_the_mate_conductor_template_routes_to_the_worker(self):
        from kiro_crew.agent_files import worker_route_for_template

        assert worker_route_for_template("kirocrew-mate-conductor") == "kirocrew-worker"

    def test_every_other_template_inherits_unchanged(self):
        from kiro_crew.agent_files import worker_route_for_template

        assert worker_route_for_template("kirocrew") == ""
        assert worker_route_for_template("code-reviewer") == ""
        assert worker_route_for_template("kirocrew-worker") == ""
        assert worker_route_for_template("") == ""

    def test_inherited_member_agent_maps_the_mate_conductor_to_the_worker(self):
        """The ``subagent_parts/agent_policy`` inheritance site: a member whose
        template is the mate conductor inherits ``kirocrew-worker``."""
        from kiro_crew.subagent_parts.agent_policy import _inherited_member_agent

        assert _inherited_member_agent("member", "kirocrew-mate-conductor") == "kirocrew-worker"
        # A member on an ordinary custom template still inherits that template.
        assert _inherited_member_agent("member", "code-reviewer") == "code-reviewer"
        # A non-member selection is unaffected.
        assert _inherited_member_agent("template", "kirocrew-mate-conductor") == ""


class TestIsKirocrewDefaultAgent:
    """``context_assembly``'s ``is_custom`` checks all defer to this predicate so
    the mate conductor is treated as the kirocrew default, not a user's custom
    agent -- see test_context.py's ``test_docs_still_injected_for_the_mate_conductor``
    for the behavioral proof."""

    def test_kirocrew_and_the_mate_conductor_are_both_the_default(self):
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert is_kirocrew_default_agent("kirocrew")
        assert is_kirocrew_default_agent("kirocrew-mate-conductor")

    def test_a_real_custom_agent_is_not(self):
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert not is_kirocrew_default_agent("code-reviewer")
        assert not is_kirocrew_default_agent("")
        assert not is_kirocrew_default_agent(None)

    def test_other_derived_specs_stay_out_of_scope(self):
        """Research/worker/team-lead are backend loops this fix does not touch;
        widening their classification is a separate decision."""
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert not is_kirocrew_default_agent("kirocrew-research")
        assert not is_kirocrew_default_agent("kirocrew-worker")
        assert not is_kirocrew_default_agent("kirocrew-team-lead")


def test_the_three_creation_defaults_all_name_the_mate_conductor():
    """All three crewmate-creation defaults name the mate conductor on a fresh
    install (the three defaults RFC §6 step 1 binds to the conductor): the first
    crewmate's implicit fallback (``first_crewmate._ORDINARY_TEMPLATE`` and
    ``_default_template()``) AND the New Crewmate dialog's / ``crewmate.create``
    card's ``change_card_catalog.DEFAULT_CREWMATE_TEMPLATE``. §6 step 1 is a
    behaviour change, so every crewmate created through any door runs the
    dispatch-only conductor rather than the full-surface ``kirocrew`` template."""
    from kiro_crew import change_card_catalog
    from kiro_crew.agent_materialization import first_crewmate

    assert first_crewmate._ORDINARY_TEMPLATE == "kirocrew-mate-conductor"
    assert first_crewmate._default_template() == "kirocrew-mate-conductor"
    assert change_card_catalog.DEFAULT_CREWMATE_TEMPLATE == "kirocrew-mate-conductor"


class TestTheRewiredBindingDoesNotFlipDefaultAgentPathsToCustom:
    """``first_crewmate._ORDINARY_TEMPLATE`` now names ``kirocrew-mate-conductor``
    instead of ``kirocrew``. Every reader that decides "default vs custom" by the
    agent's NAME must keep treating it as the default, or the crewmate's chat
    silently loses behaviour a ``kirocrew``-bound crewmate has today -- the very
    thing step 1's "no behaviour change" claim promises. These pin the paths the
    four ``is_custom`` sites in ``context_assembly`` do not cover."""

    def test_crew_context_is_injected_for_the_mate_conductor(self):
        """``_agent_includes_crew_context`` must return ``True`` outright for the
        mate conductor -- it is a default agent, never a custom one that could
        opt out with ``includeCrewContext: false``."""
        from kiro_crew.context import _agent_includes_crew_context

        assert _agent_includes_crew_context("kirocrew") is True
        assert _agent_includes_crew_context("kirocrew-mate-conductor") is True

    def test_session_skill_globs_resolves_its_mapped_charter_skills(self, tmp_path, monkeypatch):
        """The dispatch-only conductor ships an explicit skill mapping for the two
        skills its charter names (``goal-conductor`` for the dispatch/ledger
        procedure, ``dashboard`` for the page it shows the person). So
        ``session_skill_globs`` resolves THAT mapped scope -- a narrowed, source-
        defined skill set -- rather than the whole global catalog. It is still
        non-empty (not a broken custom scope of ``[]``): the mapping is present,
        which is what proves the spec is a real narrowed conductor and not an
        empty custom agent. ``kirocrew`` with no mapping still resolves the global
        catalog (``None``)."""
        from kiro_crew import agent as agent_mod
        from kiro_crew.agent_discovery import clear_list_agents_cache, session_skill_globs

        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", tmp_path)
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        service_agents._install_mate_conductor_agent()
        clear_list_agents_cache()

        # The bare default still resolves the global catalog.
        assert session_skill_globs("", "kirocrew") is None
        # The mate conductor resolves its own mapped charter skills: a real,
        # non-empty, narrowed scope, not None and not [].
        mapped = session_skill_globs("", "kirocrew-mate-conductor")
        assert mapped is not None and mapped != []
        clear_list_agents_cache()

    def test_the_mate_conductor_is_not_a_dangling_template(self):
        """``config.loader._dangling_template`` must not flag the mate conductor as
        dangling even if a stale/racing declared-name set does not list it -- a
        dangling verdict would self-heal the binding back to the default, undoing
        the rewire. ``kirocrew`` self-heals the same way; the mate conductor joins
        it via ``is_kirocrew_default_agent``."""
        from kiro_crew.config.loader import _dangling_template

        empty: frozenset[str] = frozenset()
        assert _dangling_template("kirocrew", empty) is None
        assert _dangling_template("kirocrew-mate-conductor", empty) is None
        # A genuine custom name with no declaration is still dangling.
        assert _dangling_template("code-reviewer", empty) == "code-reviewer"

    def test_a_missing_mate_conductor_template_degrades_rather_than_raising(self, monkeypatch):
        """``member_essential_context`` raises for an unknown template whose spec
        file is absent, but degrades gracefully for a Crew default. If the mate
        conductor spec has not materialized yet (a boot race), it must degrade
        like ``kirocrew``, not raise -- it is a default agent, not a user's
        custom one. ``project=None`` isolates the template-resolution branch (no
        project-steering walk)."""
        from kiro_crew import member_essential_context as mec

        monkeypatch.setattr(mec, "resolve_template_path", lambda *a, **k: None)

        # A real custom template with no spec on disk still raises.
        with pytest.raises(mec.MemberEssentialContextError):
            mec.documents_for_member("code-reviewer", None)

        # The mate conductor degrades like kirocrew rather than raising.
        for template in ("kirocrew", "kirocrew-mate-conductor"):
            mec.documents_for_member(template, None)


class TestTheInstallerNeverOverwritesAForeignSpec:
    """The name ``kirocrew-mate-conductor`` was a user-publishable template stem before
    this step reserved it, so a user could have a custom template at that path when they
    upgrade. The installer attributes any file already there by the installer-recorded
    ownership digest and REFUSES to overwrite one it cannot confirm is its own -- the same
    guard the dashboard-author installer uses. A crash before the digest is recorded fails
    closed (no digest -> foreign -> left untouched), so a user's work is never lost.
    """

    def test_a_user_published_spec_at_the_name_is_left_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        # A hand-authored spec a user published at the once-unreserved name. No managed
        # ownership digest is recorded for it, so it is foreign.
        user_spec = {
            "name": "kirocrew-mate-conductor",
            "prompt": "my own custom conductor",
            "tools": ["fs_read", "fs_write", "execute_bash"],
        }
        path = tmp_path / MATE_CONDUCTOR_AGENT_FILENAME
        path.write_text(json.dumps(user_spec), encoding="utf-8")

        service_agents._install_mate_conductor_agent()

        # The user's spec is byte-for-byte intact: the installer refused to overwrite it.
        assert json.loads(path.read_text(encoding="utf-8")) == user_spec

    def test_an_unreadable_user_file_at_the_name_is_left_untouched(self, tmp_path, monkeypatch):
        """A file present at the stem but unparseable (invalid JSON, non-UTF-8, or over the
        read cap) makes ``_read_spec_capped`` return ``None``. The guard keys off EXISTENCE,
        not parse success, so it still refuses -- a corrupt user file is left byte-for-byte
        intact rather than overwritten. (Overwriting even an unloadable spec is still data
        loss: the user may fix the syntax by hand.)"""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        path = tmp_path / MATE_CONDUCTOR_AGENT_FILENAME
        corrupt = '{"name": "kirocrew-mate-conductor", "prompt": NOT VALID JSON'
        path.write_text(corrupt, encoding="utf-8")

        service_agents._install_mate_conductor_agent()

        # Left exactly as it was: no digest is recorded for it, and it cannot be parsed,
        # so it is foreign and never overwritten.
        assert path.read_text(encoding="utf-8") == corrupt

    def test_its_own_prior_managed_write_is_refreshed_in_place(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        path = tmp_path / MATE_CONDUCTOR_AGENT_FILENAME

        # First install: the path is free, so it writes and records the ownership digest.
        service_agents._install_mate_conductor_agent()
        first = json.loads(path.read_text(encoding="utf-8"))
        assert first["name"] == "kirocrew-mate-conductor"
        assert first["prompt"] == agent._MATE_CONDUCTOR_SYSTEM_PROMPT

        # A second install re-confirms its own prior write by the recorded digest and
        # refreshes in place rather than refusing -- the dispatch-only charter is still there.
        service_agents._install_mate_conductor_agent()
        second = json.loads(path.read_text(encoding="utf-8"))
        assert second["name"] == "kirocrew-mate-conductor"
        assert second["prompt"] == agent._MATE_CONDUCTOR_SYSTEM_PROMPT

    def test_a_user_markdown_sibling_blocks_a_first_time_install(self, tmp_path, monkeypatch):
        """kiro-cli reads ``<stem>.json`` and the v3 engine also reads ``<stem>.md``, so a
        first-time ``.json`` written beside a user's ``.md`` would SHADOW that markdown spec.
        The name was unreserved, so a user could have authored ``kirocrew-mate-conductor.md``.
        The installer must leave it -- and NOT write a ``.json`` -- exactly as the
        dashboard-author installer guards this ``.md``-shadow case."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        md_path = tmp_path / "kirocrew-mate-conductor.md"
        md_body = "# my own conductor charter\nplan everything myself\n"
        md_path.write_text(md_body, encoding="utf-8")

        service_agents._install_mate_conductor_agent()

        # The user's markdown spec is byte-for-byte intact and NO shadowing .json was written.
        assert md_path.read_text(encoding="utf-8") == md_body
        assert not (tmp_path / MATE_CONDUCTOR_AGENT_FILENAME).exists()

    def test_a_markdown_sibling_does_not_freeze_a_confirmed_managed_refresh(
        self, tmp_path, monkeypatch
    ):
        """Once the ``.json`` is this installer's own confirmed write, a ``.md`` appearing at
        the stem must NOT stop the refresh -- refusing would leave the managed spec's
        auto-approvals live after the ceiling revoked them. The ``.md`` is left untouched."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        json_path = tmp_path / MATE_CONDUCTOR_AGENT_FILENAME

        # First install: path free, so it writes and records the ownership digest.
        service_agents._install_mate_conductor_agent()
        assert json_path.exists()

        # A .md appears later, after the .json is confirmed ours.
        md_path = tmp_path / "kirocrew-mate-conductor.md"
        md_body = "# user markdown that appeared later\n"
        md_path.write_text(md_body, encoding="utf-8")

        # The refresh still lands (confirmed .json is refreshed in place) and the .md is kept.
        service_agents._install_mate_conductor_agent()
        refreshed = json.loads(json_path.read_text(encoding="utf-8"))
        assert refreshed["prompt"] == agent._MATE_CONDUCTOR_SYSTEM_PROMPT
        assert md_path.read_text(encoding="utf-8") == md_body
