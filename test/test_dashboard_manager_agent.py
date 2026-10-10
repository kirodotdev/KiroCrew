"""The ``kirocrew-dashboard-manager`` subagent, and the base prompt that routes to it.

Two surfaces that only work together. The crewmate composes its own page, and the
subagent is extra capacity for a long page job; the member base prompt is the only
thing that tells it that subagent exists, and it names it by NAME.

That name is a wire string. kiro-cli resolves an agent by reading
``<agents dir>/<name>.json``, so a prompt naming something the installer does not
write routes the member to an agent that does not resolve -- which fails the turn
rather than degrading the feature, and fails it on an unattended cycle where nobody
is reading. So the central claim here is the cheap one: the name in the prompt, the
name in the spec and the spec's filename are one string.

The rest is what the spec may carry. This agent runs on a request forwarded out of a
chat it never read, which is the reason its surface is small on purpose rather than
by omission.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.agent_files import (
    DASHBOARD_MANAGER_AGENT_FILENAME,
    DASHBOARD_MANAGER_AGENT_NAME,
    OWNED_KIRO_AGENT_FILES,
    REQUIRED_KIRO_AGENT_FILES,
)
from kiro_crew.agent_materialization import managed_mcp, service_agents
from kiro_crew.context import _MEMBER_DASHBOARD_ITEM, _MEMBER_HOW_YOU_WORK


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the real installer into a scratch agents dir and read the spec back."""
    agents = tmp_path / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)
    service_agents._install_dashboard_manager_agent()
    path = agents / DASHBOARD_MANAGER_AGENT_FILENAME
    assert path.exists(), f"the installer wrote no {DASHBOARD_MANAGER_AGENT_FILENAME}"
    return json.loads(path.read_text(encoding="utf-8"))


class TestTheNameIsOneString:
    """The claim the conductor asked for: the prompt names the SHIPPED spec."""

    def test_the_base_prompt_names_the_agent_the_installer_writes(
        self, installed: dict[str, Any]
    ) -> None:
        """Read from both sides, never restated here.

        A test spelling the name itself would pass while the prompt and the spec
        agreed with the test and not with each other.
        """
        assert installed["name"] == DASHBOARD_MANAGER_AGENT_NAME
        assert f"`{DASHBOARD_MANAGER_AGENT_NAME}`" in _MEMBER_HOW_YOU_WORK

    def test_the_name_is_the_filename_stem(self) -> None:
        """kiro-cli resolves an agent by reading ``<agents dir>/<name>.json``.

        So these two are the same string by construction, and a spec whose ``name``
        and filename disagree is one that cannot be reached by the name anybody has.
        """
        assert DASHBOARD_MANAGER_AGENT_FILENAME == f"{DASHBOARD_MANAGER_AGENT_NAME}.json"

    def test_the_page_verbs_the_prompt_names_and_the_grants_are_the_same_set(self) -> None:
        """Both directions, because each one catches a different silent failure.

        A verb GRANTED and unnamed is a tool the crewmate can reach with nothing
        telling it what the tool is for, which is how a page gets handed over out of
        a catalog when somebody asked what the page should show. A verb NAMED and
        ungranted sends the crewmate to a tool that prompts or does not resolve, on
        an unattended cycle where nobody is reading.

        Read off the GRANT tuple rather than spelled here, so a verb added to or
        renamed on the surface and not in the prompt fails. Naming a verb is not
        teaching its flow: item 7 names the three catalog verbs to rule them out of
        the page request, and the equality is what keeps that account complete.
        """
        granted = {
            ref.rsplit("/", 1)[-1]
            for ref in agent_mod._MEMBER_PANEL_GRANTS
            if ref.rsplit("/", 1)[-1].startswith("dashboard_")
        }
        named = set(re.findall(r"`(dashboard_\w+)`", _MEMBER_DASHBOARD_ITEM))
        assert granted, "the grant tuple carries no page verb at all"
        assert named, "item 7 names no page verb at all"
        assert sorted(named) == sorted(granted), {
            "granted but unnamed": sorted(granted - named),
            "named but ungranted": sorted(named - granted),
        }

    def test_the_prompt_names_the_three_verbs_the_page_flow_needs(self) -> None:
        """Read the fields, fill the ones that are yours, go back.

        Spelled against the GRANT tuple rather than as bare strings, so a verb
        renamed on the surface and not in the prompt fails here.
        """
        for verb in ("dashboard_fields", "dashboard_write", "dashboard_rollback"):
            assert f"@kirocrew-panel/{verb}" in agent_mod._MEMBER_PANEL_GRANTS, verb
            assert f"`{verb}`" in _MEMBER_DASHBOARD_ITEM, verb

    def test_the_prompt_makes_the_subagent_optional(self) -> None:
        """It is extra capacity for a long job, never the only way to change a page.

        A crewmate told to hand the request over is a crewmate that cannot answer
        "show me another one" when the hand-off fails -- which is exactly what a
        spec declaring a server it does not configure produces.
        """
        assert "do not need it" in _MEMBER_DASHBOARD_ITEM

    def test_the_prompt_says_the_crewmate_composes_its_own_page(self) -> None:
        """The page is three declarations the crewmate makes, and the prompt is
        where it reads that before it goes looking for a catalog to pick from.

        Each of the three is named, because a crewmate told only "compose a page"
        has no way to know a theme is its to set or that a block may name only a
        field the Model declares.
        """
        assert "yours to compose" in _MEMBER_DASHBOARD_ITEM
        for part in ("fields the page holds", "blocks that draw them", "theme tokens"):
            assert part in _MEMBER_DASHBOARD_ITEM, part

    def test_the_prompt_names_the_data_types_a_field_may_be(self) -> None:
        """Named here rather than left to the skill.

        A crewmate reaching for a type the catalog does not hold spends a refusal
        to learn five words, and this item is injected on every turn anyway.
        """
        for data_type in ("number", "text", "bool", "timestamp", "enum"):
            assert f"`{data_type}`" in _MEMBER_DASHBOARD_ITEM, data_type

    def test_the_prompt_sends_the_crewmate_to_the_manager_skill(self) -> None:
        """The composition is a procedure, and it lives in one place.

        The charter cannot carry the block catalogue or the call that saves a page,
        so a crewmate that does not load the skill composes from memory.
        """
        assert "`dashboard-manager` skill" in _MEMBER_DASHBOARD_ITEM

    def test_the_prompt_keeps_the_ask_step(self) -> None:
        assert "ask first" in _MEMBER_DASHBOARD_ITEM

    def test_item_seven_rides_on_the_unavailable_briefing_variant_too(self) -> None:
        """The tab does not depend on layer 4.

        It is rendered by the gateway out of the instance store, so a platform whose
        briefing reads fail closed has a page like any other -- and a member there
        told nothing about it would answer "change my dashboard" with nothing.
        """
        from kiro_crew.context_assembly.member import (
            _MEMBER_BRIEFING_ITEM_UNAVAILABLE,
            _MEMBER_HOW_YOU_WORK_COMMON,
        )

        unavailable = (
            _MEMBER_HOW_YOU_WORK_COMMON + _MEMBER_BRIEFING_ITEM_UNAVAILABLE + _MEMBER_DASHBOARD_ITEM
        )
        assert DASHBOARD_MANAGER_AGENT_NAME in unavailable


class TestTheSpecsSurfaceIsSmallOnPurpose:
    """This agent runs on a request forwarded out of a chat it never read."""

    def test_it_mounts_the_panel_server_and_nothing_else(self, installed: dict[str, Any]) -> None:
        assert installed["tools"] == ["fs_read", "@kirocrew-panel"]

    def test_every_declared_server_is_also_configured(self, installed: dict[str, Any]) -> None:
        """THE POD BUG: declared in ``tools``, absent from ``mcpServers``.

        kiro-cli answered "MCP servers unusable in this session - declared by the
        agent spec but not configured: @kirocrew-panel", mounted no tool, and this
        agent was dispatched with nothing to answer with. No local error: the grant
        was simply dead, and the only place it showed was a running pod.

        Asked as a RULE over every ``@`` ref the spec declares rather than about the
        one name, because the next server added to ``tools`` would reproduce this
        exactly. ``includeMcpJson`` is off here, so ``mcpServers`` is the only place
        a declared server can be configured from.
        """
        declared = {ref[1:].split("/", 1)[0] for ref in installed["tools"] if ref.startswith("@")}
        assert declared, "the fixture no longer declares an MCP server"
        configured = set(installed["mcpServers"])
        assert declared <= configured, (
            f"declared but not configured: {sorted(declared - configured)} -- kiro-cli "
            "mounts no tool for these and the grant is dead"
        )

    def test_the_configured_entry_can_actually_launch_the_server(
        self, installed: dict[str, Any]
    ) -> None:
        """An entry with no command is as dead as an absent one.

        Compared against ``_managed_opt_in_entry``, which is the one helper that
        knows the two fields a hand-built opt-in entry forgets -- the registry
        ``type`` a registry-mode client needs to not DROP the entry, and the
        ``KIROCREW_HOME`` pin without which the shim reads the default data home
        while the gateway runs under an override.
        """
        entry = installed["mcpServers"]["kirocrew-panel"]
        assert entry == managed_mcp._managed_opt_in_entry("mcp-panel")
        assert entry["command"], "no command, so nothing launches"
        assert any("mcp-panel" in str(arg) for arg in entry["args"]), entry["args"]

    def test_it_has_no_file_write_and_no_shell(self, installed: dict[str, Any]) -> None:
        """Every page it produces goes through the store's own write path.

        That path checks the Model, the View and the theme against the catalogues
        before anything lands. A file-writing tool would let this agent drop a page
        on disk directly, skipping both that check and the person's yes.
        """
        mounted = set(installed["tools"]) | set(installed["allowedTools"])
        for forbidden in ("fs_write", "code", "execute_bash"):
            assert forbidden not in mounted, forbidden

    def test_it_mounts_neither_core_nor_the_dashboard_server(
        self, installed: dict[str, Any]
    ) -> None:
        """It neither dispatches work nor reads anybody's sessions."""
        assert "@kirocrew-core" not in installed["tools"]
        assert "@kirocrew-dashboard" not in installed["tools"]

    def test_its_grants_are_the_crewmates_own_tuple_reused(self, installed: dict[str, Any]) -> None:
        """REUSED, not copied.

        Two tuples would drift into a surface this agent may call and its caller may
        not, and the caller is the one the ownership argument for those grants was
        written about.

        Read through the governance ceiling, which is the second half of the same
        argument: ``allowedTools`` is the one list whose entries never reach the
        PreToolUse gate, so a ref the ceiling withholds from the crewmate has to be
        withheld here too. Asserting the raw tuple would pass while this agent held an
        auto-approve its caller does not -- which is the drift, arriving from the other
        direction.
        """
        expected = [
            ref
            for ref in ("fs_read", *agent_mod._MEMBER_PANEL_GRANTS)
            if agent_mod._may_auto_approve(ref)
        ]
        assert installed["allowedTools"] == expected

    def test_it_pins_include_mcp_json_off(self, installed: dict[str, Any]) -> None:
        """kiro-cli defaults this True and would spawn every server in the user's
        mcp.json for a session that must mount one."""
        assert installed["includeMcpJson"] is False

    def test_the_prompt_carries_the_ask_step_and_the_fold_rule(self) -> None:
        """The charter, not the procedure -- but these two do not live in a skill.

        The ASK is the only thing standing between an agent and a page the reader
        never saw, and the fold rule is what stops a page that is true once.
        """
        prompt = agent_mod._DASHBOARD_MANAGER_SYSTEM_PROMPT
        assert "ASK" in prompt
        assert "Never type one" in prompt
        assert "dashboard" in prompt.lower()

    def test_the_prompt_teaches_the_three_declarations_a_page_is(self) -> None:
        """The whole job, so an agent dispatched with nothing else can start.

        A Model whose fields declare a type and a source, a View of blocks over
        those fields, and a theme. Each is asserted by name: a prompt naming two of
        the three leaves the agent to guess the shape of the one it cannot see, and
        the guess costs a refusal from the store's own write check.
        """
        prompt = agent_mod._DASHBOARD_MANAGER_SYSTEM_PROMPT
        for part in ("**Model**", "**View**", "**theme**"):
            assert part in prompt, part
        assert '`kind="dashboard"`' in prompt, "the page is an artifact and the kind is named"
        assert '{"agentic": true}' in prompt
        assert '{"fold": <name>, "path": <dotted' in prompt

    def test_the_prompt_names_the_data_types_from_the_catalog(self) -> None:
        """Five names, and no sixth invented beside them.

        An agent that reaches for a type the catalog does not hold earns a refusal
        from the write path, which is a cycle this list costs nothing to save.
        """
        prompt = agent_mod._DASHBOARD_MANAGER_SYSTEM_PROMPT
        for data_type in ("number", "text", "bool", "timestamp", "enum"):
            assert f"`{data_type}`" in prompt, data_type

    def test_the_prompt_sends_nobody_to_a_catalog_to_pick_from(self) -> None:
        """The product offers no page to choose, so a prompt that sends an agent
        looking for one spends a cycle on a search that answers nothing -- and
        leaves it with no answer for the person who asked.

        Asserted as the ABSENCE of the whole vocabulary rather than of one phrase,
        because any one of these words is enough to start the agent down that path.
        """
        prompt = agent_mod._DASHBOARD_MANAGER_SYSTEM_PROMPT.lower()
        for word in ("template", "preview", "catalog of pages", "builtin"):
            assert word not in prompt, word
        assert "data-dashboard-field" not in prompt, "still teaching html authoring"

    def test_the_prompt_says_the_theme_is_the_agents_to_set(self) -> None:
        """Nothing about the look is fixed by the product.

        An agent that believes the style is the host's writes a page that reads as
        every other page, and asks nobody about a decision that was its to make.
        """
        # Whitespace collapsed, so the claim survives a reflow of the paragraph it
        # sits in rather than pinning where the line happens to wrap.
        prompt = " ".join(agent_mod._DASHBOARD_MANAGER_SYSTEM_PROMPT.split())
        assert "None of the look is fixed by the product" in prompt
        assert "the tokens are where you set it" in prompt

    def test_the_prompt_forbids_doing_the_crewmates_work(self) -> None:
        """A subagent that answered the question the page is about would be a second
        crewmate with a smaller surface, which is not what anybody dispatched."""
        assert "You do not do the crewmate's work." in agent_mod._DASHBOARD_MANAGER_SYSTEM_PROMPT


class TestItIsOwnedButNotRequired:
    def test_the_filename_is_in_the_owned_allowlist(self) -> None:
        """The convergence sweep rewrites only files Kiro Crew generates."""
        assert DASHBOARD_MANAGER_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_it_is_not_required_for_the_product_to_work(self) -> None:
        """With the spec absent a crewmate still has ``dashboard_fields`` and
        ``dashboard_write``, so the page it has keeps working and only CHANGING the
        page is unavailable. Its installer degrades to a debug line for that reason."""
        assert DASHBOARD_MANAGER_AGENT_FILENAME not in REQUIRED_KIRO_AGENT_FILES
