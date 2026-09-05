"""Registration ratchet for the ``kirocrew-panel`` MCP server.

The property this file exists to pin: a default agent pays NOTHING for this
capability. Not a tool-list entry, not a line of context, not a spawned backend.
It is an assignable set that a long-running agent's own spec names, and every
other agent must be unable to tell it exists.

Modelled on ``test_mcp_dashboard_registration.py`` — the same registries have to
agree, and the same silence has to hold for an agent that was never granted the
set.
"""

from __future__ import annotations

import importlib

from kiro_crew import agent, mcp_cleanup, mcp_discovery, onboarding_import

PANEL_SERVER = "kirocrew-panel"
PANEL_SUBCOMMAND = "mcp-panel"
PANEL_MODULE = "kiro_crew.mcp_panel"
PANEL_REF = f"@{PANEL_SERVER}"


class TestRegistryParity:
    """Every registry that names a managed server must name this one.

    Each miss is a distinct silent failure: a spec that references a server no
    launcher knows, a cleanup pass that treats an opt-in grant as purgeable
    residue, or an import that drops the user's grant on the floor.
    """

    def test_named_in_every_managed_registry(self) -> None:
        assert PANEL_SERVER in agent._MANAGED_MCP_SERVERS
        assert PANEL_SERVER in mcp_cleanup.KIROCREW_BIN_MCP_SERVERS
        assert mcp_discovery._MANAGED_SERVER_SUBCOMMANDS.get(PANEL_SERVER) == PANEL_SUBCOMMAND
        assert PANEL_SERVER in mcp_discovery._MANAGED_SERVER_NAMES
        assert mcp_discovery._MANAGED_SERVER_TOOL_MODULES.get(PANEL_SERVER) == PANEL_MODULE
        assert PANEL_SERVER in onboarding_import._managed_mcp_names()

    def test_it_is_marked_as_an_assignable_set(self) -> None:
        """``opt_in`` is the whole zero-cost mechanism, so it is pinned directly."""
        assert agent._MANAGED_MCP_SERVERS[PANEL_SERVER].get("opt_in") is True

    def test_the_cleanup_split_tracks_the_opt_in_flag(self) -> None:
        assert PANEL_SERVER in mcp_cleanup.OPT_IN_BIN_MCP_SERVERS
        assert PANEL_SERVER not in mcp_cleanup.ALWAYS_ON_BIN_MCP_SERVERS

    def test_the_name_alone_is_not_ownership(self) -> None:
        """An opt-in name is never purged from the user's global mcp.json.

        Kiro Crew never writes an opt-in server there, so anything found under
        that name is necessarily the user's own and must be left alone.
        """
        assert PANEL_SERVER not in mcp_cleanup.STALE_MANAGED_MCP_SERVERS

    def test_it_carries_no_auto_approve(self) -> None:
        """An autoApproved MCP tool never reaches ``hooks.on_tool_call``.

        That is where the deny floor, the sensitive-path check and the
        governance ceiling live. This tool's input derives from text the agent
        read unattended, so a blanket local approval must never be minted here;
        an agent that needs a prompt-free write names the individual verbs in
        its own spec, which is the deliberate, narrower path.
        """
        assert "autoApprove" not in agent._MANAGED_MCP_SERVERS[PANEL_SERVER]


class TestTheDeclarationSurfacesBeyondTheRegistries:
    """The surfaces this file's first version MISSED, pinned so the next one cannot.

    ``TestRegistryParity`` above pinned seven registries and reported "7 of 7,
    set-equality holds" -- and the change still shipped with four other
    declaration surfaces empty, each caught by a suite nobody thought to run.
    Registering a managed server is not seven edits, it is eleven, and a
    completeness claim that measures only the seven is the thing that went wrong.

    So each test here asserts a DERIVED property ("this module has a
    classification") rather than a literal, which is what makes it fire for the
    NEXT server instead of only documenting this one.
    """

    def test_every_managed_server_appears_in_the_architecture_spec(self) -> None:
        """AGENTS.md requires same-commit spec updates, and this is the check for it.

        ``docs/architecture/mcp.md`` carries the server/tool table an operator reads
        to learn what a managed server IS; ``mcp-shareability.md`` reasons about the
        managed set by name. Both went stale when this server was added -- only the
        feature-map row was touched -- and no suite noticed, because a prose table is
        exactly the surface no registry parity test covers.

        Derived from ``_MANAGED_MCP_SERVERS`` rather than checking for
        ``kirocrew-panel``, so the NEXT server fails here instead of shipping the
        same omission a third time.
        """
        from pathlib import Path

        from kiro_crew.agent import _MANAGED_MCP_SERVERS

        repo = Path(__file__).resolve().parent.parent
        spec = repo / "docs" / "architecture" / "mcp.md"
        assert spec.is_file(), f"the architecture spec moved: {spec}"
        text = spec.read_text(encoding="utf-8")

        assert _MANAGED_MCP_SERVERS, "no managed servers -- this test would be vacuous"
        missing = sorted(name for name in _MANAGED_MCP_SERVERS if f"`{name}`" not in text)
        assert not missing, (
            f"{missing} are registered managed servers but are absent from "
            f"{spec.relative_to(repo)}; AGENTS.md requires the spec update in the "
            "same commit as the code"
        )

    def test_the_shareability_spec_does_not_undercount_the_managed_set(self) -> None:
        """A spec that counts the set in prose goes stale silently.

        ``mcp-shareability.md`` states how many managed servers advertise the
        caller-identity block. The sentence was written when there were four and
        stayed at four -- a wrong statement about a security property, in the
        document that reasons about it. Asserted as: no number word BELOW the real
        count may qualify "managed servers".
        """
        import re
        from pathlib import Path

        from kiro_crew.agent import _MANAGED_MCP_SERVERS

        repo = Path(__file__).resolve().parent.parent
        spec = repo / "docs" / "system-specs" / "modules" / "mcp-shareability.md"
        assert spec.is_file(), f"the shareability spec moved: {spec}"
        text = spec.read_text(encoding="utf-8")

        words = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight"]
        actual = len(_MANAGED_MCP_SERVERS)
        stale = [
            w
            for i, w in enumerate(words)
            if i < actual and re.search(rf"\b{w}\s+managed servers\b", text, re.I)
        ]
        assert not stale, (
            f"{spec.name} says {stale} managed servers but the registry has {actual}; "
            "the count in prose is now wrong about a security property"
        )

    def test_the_schema_registry_is_visible_in_the_posture_report(self) -> None:
        """A validated tool invisible in the posture report reads as unvalidated.

        ``validation`` gained ``MCP_PANEL_SCHEMAS`` and
        ``security_posture._SCHEMA_REGISTRY_NAMES`` did not, so the tools were
        gated but absent from the report an operator reads to see what is gated.
        Derived from ``validation``'s own registries, so a NEW ``MCP_*_SCHEMAS``
        fails here too.
        """
        from kiro_crew import security_posture, validation

        registries = {n for n in dir(validation) if n.startswith("MCP_") and n.endswith("_SCHEMAS")}
        assert registries, "no MCP_*_SCHEMAS found in validation -- this test would be vacuous"
        undeclared = registries - set(security_posture._SCHEMA_REGISTRY_NAMES)
        assert not undeclared, (
            f"{sorted(undeclared)} validate tools but are missing from "
            "security_posture._SCHEMA_REGISTRY_NAMES, so those tools are gated but "
            "invisible in the posture report"
        )

    def test_the_server_module_has_a_redaction_classification(self) -> None:
        """Every module calling a redactor is a declared sink or an allowlisted one.

        The omission-detecting half of ``test_security_posture``. Asserted here as
        well because THAT test names the module only once it already fails, and
        this file is where a reader looks for what registering a server costs.
        """
        from kiro_crew import security_posture

        module = f"{PANEL_MODULE.rsplit('.', 1)[-1]}.py"
        sinks = {entry[1] for entry in security_posture._REDACTION_SINKS}
        assert (
            module in sinks or module in security_posture.NON_EGRESS_REDACTION_MODULES
        ), f"{module} calls a redactor with no declared classification"

    def test_the_store_module_is_a_declared_redaction_sink(self) -> None:
        """The panel CONTENT is scrubbed in the store, so the store is the sink.

        Pinned separately from the server's classification because the two are
        deliberately different buckets, and collapsing them would lose the
        distinction between relaying content and scrubbing it.
        """
        from kiro_crew import security_posture

        sinks = {entry[1] for entry in security_posture._REDACTION_SINKS}
        assert "agent_panel.py" in sinks

    def test_every_fenced_crew_path_has_a_sandbox_disposition(self) -> None:
        """A tool-gate fence is not a control on its own.

        ``_CREW_SECRET_LEAVES`` stops the agent's FILE TOOLS from writing a path;
        any spawned shell bypasses it entirely unless the OS sandbox also has a
        disposition for that path. The template fence shipped with none, and
        ``test_sandbox_governance_mask`` failed on the two crew-home spellings it
        produced. Derived over the whole fence list, so the next fenced leaf is
        covered without editing this test.
        """
        from kiro_crew import agent_panel, sandbox, security

        placed = (
            set(sandbox._CREW_HIDDEN_LEAVES)
            | set(sandbox._CREW_READONLY_LEAVES)
            | set(sandbox._CREW_SANDBOX_VISIBLE_LEAVES)
        )
        assert placed, "no sandbox leaf sets found -- this test would be vacuous"
        assert agent_panel.TEMPLATES_DIRNAME in security._CREW_SECRET_LEAVES
        assert agent_panel.TEMPLATES_DIRNAME in placed, (
            f"{agent_panel.TEMPLATES_DIRNAME} is fenced from the agent's file tools but has "
            "no OS-sandbox disposition, so a spawned shell can still write it"
        )

    def test_the_template_directory_is_not_writable_in_the_sandbox(self) -> None:
        """And the disposition must be the RESTRICTIVE one.

        Read-only or masked both satisfy the reconciliation; landing on the
        read-write exception list would satisfy it too and quietly undo the fence,
        which is the failure this asserts against rather than merely detecting a
        blank.
        """
        from kiro_crew import agent_panel, sandbox

        assert agent_panel.TEMPLATES_DIRNAME not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES


class TestTheDefaultAgentIsNotGrantedTheSet:
    """The constraint: zero context, zero tool-list entries, unless granted."""

    def test_a_fresh_spec_does_not_reference_the_server(self) -> None:
        config = agent.build_agent_config()
        assert PANEL_SERVER not in config.get("mcpServers", {})
        assert PANEL_REF not in config.get("tools", [])
        assert PANEL_REF not in config.get("allowedTools", [])

    def test_no_panel_tool_leaks_into_the_default_grant_list(self) -> None:
        config = agent.build_agent_config()
        granted = config.get("allowedTools", [])
        assert not [g for g in granted if str(g).startswith(PANEL_REF)]

    def test_the_shipped_default_tools_do_not_name_it(self) -> None:
        """The packaged default must not carry the ref either.

        ``tools`` is user-owned and merged add-only, so a ref that ever shipped
        in the default would become permanent on every existing install.
        """
        import json

        from kiro_crew.config.loader import config_package_dir

        defaults = json.loads((config_package_dir() / "defaults.json").read_text(encoding="utf-8"))
        agent_defaults = defaults.get("agent", {})
        assert PANEL_REF not in (agent_defaults.get("tools") or [])
        assert PANEL_REF not in (agent_defaults.get("allowedTools") or [])

    def test_a_refresh_does_not_introduce_the_server(self) -> None:
        """A refresh keeps an existing grant current but never mints one."""
        config: dict = {"mcpServers": {}, "tools": []}
        agent._refresh_dynamic_fields(config)
        assert PANEL_SERVER not in config["mcpServers"]

    def test_a_refresh_keeps_an_existing_grant_current(self) -> None:
        """An entry already on disk is a grant the user made — it stays and updates."""
        config: dict = {
            "mcpServers": {PANEL_SERVER: {"command": "stale", "args": ["stale"]}},
            "tools": [PANEL_REF],
        }
        agent._refresh_dynamic_fields(config)
        assert config["mcpServers"][PANEL_SERVER]["args"] == [PANEL_SUBCOMMAND]


class TestWhatThisSetGrants:
    """Assignment is per SERVER, so the set is the unit of authorization.

    Pinned so that adding a capability to this server is a deliberate edit here
    rather than a silent widening of a set the user already granted. A tool that
    is not about publishing a panel belongs in a server of its own with the gate
    it actually needs.
    """

    GRANTED_TOOLS = {"panel_publish", "panel_templates"}

    def test_the_set_is_exactly_the_panel_tools(self) -> None:
        mcp_panel = importlib.import_module(PANEL_MODULE)
        assert {t["name"] for t in mcp_panel._tool_definitions()} == self.GRANTED_TOOLS

    def test_the_advertised_list_is_the_set(self) -> None:
        mcp_panel = importlib.import_module(PANEL_MODULE)
        assert {t["name"] for t in mcp_panel._list_tools()} == self.GRANTED_TOOLS

    def test_every_tool_is_named_for_what_it_does(self) -> None:
        mcp_panel = importlib.import_module(PANEL_MODULE)
        names = {t["name"] for t in mcp_panel._tool_definitions()}
        assert names == {n for n in names if n.startswith("panel_")}, (
            f"{sorted(n for n in names if not n.startswith('panel_'))} is not panel "
            "publishing — name the class it belongs to before adding it here"
        )

    def test_every_tool_has_a_registered_schema(self) -> None:
        """A tool absent from its server's registry has its args passed through raw."""
        from kiro_crew.validation import MCP_PANEL_SCHEMAS

        mcp_panel = importlib.import_module(PANEL_MODULE)
        assert {t["name"] for t in mcp_panel._tool_definitions()} == set(MCP_PANEL_SCHEMAS)

    def test_no_tool_takes_a_session_argument(self) -> None:
        """The target is derived from the vetted caller, never from arguments.

        A ``session``/``slot`` argument would be a way for one agent to publish
        into another agent's panel.
        """
        mcp_panel = importlib.import_module(PANEL_MODULE)
        for tool in mcp_panel._tool_definitions():
            props = set(tool["inputSchema"].get("properties") or {})
            assert not (
                props & {"session", "session_key", "slot", "slot_key", "target"}
            ), f"{tool['name']} accepts a caller-supplied target"


class TestCallerIdentity:
    def test_the_constant_matches_the_discovery_classification(self) -> None:
        """Discovery reads the name set, not the module, so the two must agree."""
        mcp_panel = importlib.import_module(PANEL_MODULE)
        assert mcp_panel.ADVERTISE_CALLER_IDENTITY is True
        assert PANEL_SERVER in mcp_discovery._MANAGED_SERVERS_CALLER_AWARE

    def test_the_cli_can_launch_it(self) -> None:
        """The spec's command is only useful if the CLI actually dispatches it."""
        from kiro_crew import cli

        source = importlib.import_module("kiro_crew.cli").__file__
        assert source is not None
        text = open(source, encoding="utf-8").read()
        assert f'sub.add_parser("{PANEL_SUBCOMMAND}")' in text
        assert f'args.command == "{PANEL_SUBCOMMAND}"' in text
        assert cli is not None
