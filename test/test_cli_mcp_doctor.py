"""The MCP section of ``kirocrew doctor``.

``_doctor_mcp_tools`` probes only the managed servers, reports tool counts and errors and repairs
missing tool references; the section's checks consult the same ``spec_gate`` that spec emission
does.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
    _healthy_agent_file,
    _write_agent_config,
)


class TestDoctorMcpTools:
    """Tests for the `_doctor_mcp_tools` helper — the MCP section of doctor.

    The helper live-probes only the managed servers (`kirocrew-core`,
    `kirocrew-cron`) via `probe_server`; tests monkey-patch that call so
    no child processes are spawned.
    """

    def _mock_probe(self, results: dict[str, tuple[str, list[str], str]]):
        """Return a patch target for `probe_server` that yields per-name
        results. `results[name] = (status, tools, error)`."""
        from kiro_crew.mcp_discovery import McpServerInfo

        async def fake(target: McpServerInfo) -> McpServerInfo:
            status, tools, error = results.get(target.name, ("ok", [], ""))
            target.status = status
            target.tools = list(tools)
            target.error = error
            return target

        return patch("kiro_crew.cli_doctor.probe_server", side_effect=fake)

    def test_success_shows_tool_counts(self, tmp_path, capsys):
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        # Every managed server must be present or doctor reports it as a config
        # issue, so the fixture is built from the registry doctor iterates rather
        # than a literal pair (see _healthy_agent_file).
        _healthy_agent_file(agent_path)
        issues: list[str] = []
        with self._mock_probe(
            {
                "kirocrew-core": ("ok", ["spawn_run", "learn_add", "task_run"], ""),
                "kirocrew-cron": ("ok", ["cron_add"], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-core: ✅ 3 tools" in out
        assert "@kirocrew-cron: ✅ 1 tool" in out
        assert issues == []

    def test_failure_shows_error_head_and_indented_stderr(self, tmp_path, capsys):
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core", "@kirocrew-cron"],
            allowed=["@kirocrew-core", "@kirocrew-cron"],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        fail_err = (
            "no response\n"
            "stderr: Directory isn't within a workspace: '/home/u/.kirocrew-app' "
            "(Example::Cli::FindupException)"
        )
        with self._mock_probe(
            {
                "kirocrew-core": ("error", [], fail_err),
                "kirocrew-cron": ("ok", [], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        # First line of error becomes the head; subsequent lines indent.
        assert "@kirocrew-core: ❌ no response" in out
        assert "      stderr: Directory isn't within a workspace" in out
        assert "FindupException" in out
        assert "@kirocrew-cron: ✅ 0 tools" in out
        assert "@kirocrew-core probe" in issues
        # Healthy server must not pollute the issue list.
        assert "@kirocrew-cron probe" not in issues

    def test_missing_mcp_server_cannot_auto_fix(self, tmp_path, capsys):
        """A missing `mcpServers` entry is install-specific; doctor reports
        the user needs to re-run setup and does not attempt to probe."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=[],
            allowed=[],
            servers={},
        )
        issues: list[str] = []
        with self._mock_probe({}) as probe_mock:
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-core: ❌ missing from mcpServers" in out
        assert "@kirocrew-cron: ❌ missing from mcpServers" in out
        assert "re-run `kirocrew setup`" in out
        assert "@kirocrew-core config" in issues
        assert "@kirocrew-cron config" in issues
        probe_mock.assert_not_called()

    def test_auto_fix_adds_missing_tools_and_allowed(self, tmp_path, capsys):
        """Missing `tools` / `allowedTools` entries are added to the agent
        config and persisted in-place."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=[],
            allowed=[],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        with self._mock_probe(
            {
                "kirocrew-core": ("ok", [], ""),
                "kirocrew-cron": ("ok", [], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "Auto-fixed agent config" in out
        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert updated["tools"] == ["@kirocrew-cron", "@kirocrew-core"]
        assert updated["allowedTools"] == ["@kirocrew-cron", "@kirocrew-core"]

    def test_auto_fix_skips_write_when_shared_home_declined(self, tmp_path, capsys):
        """An instance that must not own the shared agent home reports the
        auto-fix as skipped and leaves the spec byte-identical."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=[],
            allowed=[],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        before = agent_path.read_bytes()
        issues: list[str] = []
        with (
            patch("kiro_crew.agent._decline_shared_agent_home", return_value=agent_path),
            self._mock_probe(
                {
                    "kirocrew-core": ("ok", [], ""),
                    "kirocrew-cron": ("ok", [], ""),
                }
            ),
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "Auto-fix skipped: shared home" in out
        assert "Auto-fixed agent config" not in out
        assert agent_path.read_bytes() == before
        assert "agent config (auto-fix skipped: shared home)" in issues

    def test_auto_fix_refused_at_the_locked_recheck_is_audited_and_writes_nothing(
        self, tmp_path, capsys
    ):
        """Ownership that changes between the probe and the write is caught under
        the agent-spec lock, and the audit records that final refusal."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=[],
            allowed=[],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        before = agent_path.read_bytes()
        issues: list[str] = []
        sel_mock = MagicMock()
        with (
            patch(
                "kiro_crew.agent._decline_shared_agent_home",
                side_effect=[None, agent_path],
            ),
            patch("kiro_crew.cli_doctor.sel", sel_mock),
            self._mock_probe(
                {
                    "kirocrew-core": ("ok", [], ""),
                    "kirocrew-cron": ("ok", [], ""),
                }
            ),
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "Auto-fix skipped: shared home" in out
        assert agent_path.read_bytes() == before
        assert "agent config (auto-fix skipped: shared home)" in issues
        sel_mock.return_value.log_api_access.assert_called_once_with(
            caller="system",
            operation="agent_home_write",
            outcome="denied",
            source="cli_doctor",
            resources=str(agent_path),
        )

    def test_declined_home_reports_a_forbidden_grant_without_writing(self, tmp_path, capsys):
        """A declined home leaves a ceiling-forbidden grant on disk, logs no
        SEL revoke, and reports the grant for the owning install to repair."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        refs = ["@kirocrew-core", "@kirocrew-cron"]
        _write_agent_config(
            agent_path,
            tools=refs,
            allowed=refs,
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        before = agent_path.read_bytes()
        issues: list[str] = []
        sel_mock = MagicMock()
        with (
            patch("kiro_crew.agent._decline_shared_agent_home", return_value=agent_path),
            patch("kiro_crew.cli_doctor.may_skip_gate_now", return_value=False),
            patch("kiro_crew.cli_doctor.sel", sel_mock),
            self._mock_probe({}),
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert agent_path.read_bytes() == before
        sel_mock.return_value.log_api_access.assert_called_once_with(
            caller="system",
            operation="agent_home_write",
            outcome="denied",
            source="cli_doctor",
            resources=str(agent_path),
        )
        assert "auto-approve withheld" not in out
        assert issues == [
            f"{ref} auto-approve forbidden by ceiling (repair from the owning install)"
            for ref in ("@kirocrew-cron", "@kirocrew-core")
        ] + [
            "@kirocrew-guide config",
            "@kirocrew-mate config",
            "agent config (auto-fix skipped: shared home)",
        ]

    def test_ceiling_revoke_drops_per_tool_grants_too(self, tmp_path, capsys):
        """Withholding auto-approve drops the bare ref and every ``@server/tool``
        grant of that server, duplicates included, and nothing else."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core"],
            allowed=[
                "@kirocrew-core",
                "@kirocrew-core/send_message",
                "@kirocrew-core/send_message",
                "@kirocrew-corex/run",
                "fs_read",
            ],
            servers={"kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]}},
        )
        issues: list[str] = []
        with (
            patch("kiro_crew.agent._decline_shared_agent_home", return_value=None),
            patch("kiro_crew.cli_doctor.may_skip_gate_now", return_value=False),
            patch("kiro_crew.cli_doctor.sel", MagicMock()),
            self._mock_probe({}),
        ):
            _doctor_mcp_tools(agent_path, issues)
        written = json.loads(agent_path.read_text(encoding="utf-8"))
        assert written["allowedTools"] == ["@kirocrew-corex/run", "fs_read"]
        assert "@kirocrew-core" in written["tools"]

    def test_a_confirm_tools_withhold_is_not_reported_as_a_ceiling(self, tmp_path, capsys):
        """The operator's own ``hooks.confirm_tools`` names itself as the cause."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core"],
            allowed=["@kirocrew-core"],
            servers={"kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]}},
        )
        sel_mock = MagicMock()
        with (
            patch("kiro_crew.agent._decline_shared_agent_home", return_value=None),
            patch("kiro_crew.cli_doctor.may_skip_gate_now", return_value=False),
            patch("kiro_crew.cli_doctor.withheld_by_confirm_tools", return_value=True),
            patch("kiro_crew.cli_doctor.sel", sel_mock),
            self._mock_probe({}),
        ):
            _doctor_mcp_tools(agent_path, [])
        out = capsys.readouterr().out
        assert "@kirocrew-core: 🔒 auto-approve withheld by hooks.confirm_tools" in out
        assert "security policy" not in out
        resources = [
            c.kwargs.get("resources", "")
            for c in sel_mock.return_value.log_api_access.call_args_list
            if c.kwargs.get("operation") == "mcp_auto_approve_withheld"
        ]
        assert resources and "(hooks.confirm_tools)" in resources[0]

    def test_declined_home_names_confirm_tools_as_the_cause(self, tmp_path, capsys):
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core"],
            allowed=["@kirocrew-core"],
            servers={"kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]}},
        )
        issues: list[str] = []
        with (
            patch("kiro_crew.agent._decline_shared_agent_home", return_value=agent_path),
            patch("kiro_crew.cli_doctor.may_skip_gate_now", return_value=False),
            patch("kiro_crew.cli_doctor.withheld_by_confirm_tools", return_value=True),
            patch("kiro_crew.cli_doctor.sel", MagicMock()),
            self._mock_probe({}),
        ):
            _doctor_mcp_tools(agent_path, issues)
        assert (
            "@kirocrew-core auto-approve withheld by hooks.confirm_tools "
            "(repair from the owning install)"
        ) in issues

    def test_auto_fix_never_blanket_allows_computer_use(self, tmp_path, capsys):
        """**Doctor must never add ``@kirocrew-computer`` to ``allowedTools``.**

        ``allowedTools`` is kiro-cli's blanket auto-approve list, and an
        auto-approved MCP tool is approved LOCALLY by kiro-cli: it emits no
        permission request and therefore never reaches ``hooks.on_tool_call`` — so
        the deny floor, the governance ceiling and the approval clamp would all be
        skipped for a tool that can click and type into an already-authenticated
        application.  ``agent.py``'s managed spec omits ``autoApprove`` for exactly
        this reason; a diagnostic command silently undoing it would be a complete
        Plane-A bypass.  The ``tools`` entry is still repaired — that only makes the
        server's tools reachable, never pre-approved.

        The spec gate is pinned OPEN: this guard is about what doctor does to a
        server it repairs, and a closed gate (the CI default — feature disabled)
        would skip the repair entirely and assert nothing.
        """
        from kiro_crew import agent
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_path)
        # Start from the state an upgrade leaves behind: servers registered, no
        # tool refs at all, so every ref doctor could add is attributable to it.
        data = json.loads(agent_path.read_text(encoding="utf-8"))
        data["tools"] = []
        data["allowedTools"] = []
        agent_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        gated_open = dict(agent._MANAGED_MCP_SERVERS["kirocrew-computer"])
        gated_open["spec_gate"] = lambda: True
        issues: list[str] = []
        with (
            patch.dict(agent._MANAGED_MCP_SERVERS, {"kirocrew-computer": gated_open}),
            self._mock_probe({}),
        ):
            _doctor_mcp_tools(agent_path, issues)

        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert "@kirocrew-computer" in updated["tools"]
        assert "@kirocrew-computer" not in updated["allowedTools"]
        assert not [t for t in updated["allowedTools"] if t.startswith("@kirocrew-computer/")]
        # The other managed servers are unaffected — the carve-out is scoped.
        assert "@kirocrew-core" in updated["allowedTools"]
        assert "@kirocrew-cron" in updated["allowedTools"]

    def test_auto_fix_never_blanket_allows_the_guide_set(self, tmp_path, capsys):
        """The guide set is always on but granted per tool, so doctor mounts its
        ref and never mints the whole-server grant: that would pre-approve a tool
        added to the server later, before anyone reviewed it."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_path)
        data = json.loads(agent_path.read_text(encoding="utf-8"))
        data["tools"] = []
        data["allowedTools"] = []
        agent_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with self._mock_probe({}):
            _doctor_mcp_tools(agent_path, [])
        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert "@kirocrew-guide" in updated["tools"]
        assert "@kirocrew-guide" not in updated["allowedTools"]
        assert "@kirocrew-core" in updated["allowedTools"]

    def test_auto_fix_preserves_a_user_made_computer_use_grant(self, tmp_path, capsys):
        """Doctor never MINTS the grant, but never REMOVES a user's own either.

        A user who deliberately added the ref owns that decision; silently
        reverting their config on a diagnostic run would be its own surprise. The
        two rules are independent and both matter.

        Gate pinned OPEN like the mint test above: the preservation rule is
        exercised on the path where doctor walks the full entry.
        """
        from kiro_crew import agent
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_path)
        gated_open = dict(agent._MANAGED_MCP_SERVERS["kirocrew-computer"])
        gated_open["spec_gate"] = lambda: True
        issues: list[str] = []
        with (
            patch.dict(agent._MANAGED_MCP_SERVERS, {"kirocrew-computer": gated_open}),
            self._mock_probe({}),
        ):
            _doctor_mcp_tools(agent_path, issues)
        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert "@kirocrew-computer" in updated["allowedTools"]

    def test_probe_exception_does_not_crash(self, tmp_path, capsys):
        """If `probe_server` itself raises (e.g. event-loop oddity), doctor
        prints a warning and returns cleanly instead of propagating."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core", "@kirocrew-cron"],
            allowed=["@kirocrew-core", "@kirocrew-cron"],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        with patch(
            "kiro_crew.cli_doctor.probe_server",
            side_effect=RuntimeError("asyncio is on fire"),
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "probe failed: asyncio is on fire" in out

    def test_only_managed_servers_are_probed(self, tmp_path, capsys):
        """Third-party MCPs in the agent config must not be probed — this
        keeps doctor output focused on Kiro Crew's own servers and avoids
        false negatives for optional MCPs."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core", "@kirocrew-cron", "@builder-mcp"],
            allowed=["@kirocrew-core", "@kirocrew-cron", "@builder-mcp"],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
                "builder-mcp": {"command": "/bin/builder-mcp"},
            },
        )
        issues: list[str] = []
        probed_names: list[str] = []

        async def recording_probe(target):
            probed_names.append(target.name)
            target.status = "ok"
            target.tools = []
            return target

        with patch("kiro_crew.cli_doctor.probe_server", side_effect=recording_probe):
            _doctor_mcp_tools(agent_path, issues)
        assert probed_names == ["kirocrew-cron", "kirocrew-core"]
        out = capsys.readouterr().out
        assert "@builder-mcp" not in out

    def test_malformed_agent_config_does_not_crash(self, tmp_path, capsys):
        """If kirocrew.json is truncated or otherwise unparseable, doctor
        must fall back to an empty config and surface missing-server
        errors cleanly rather than raising out of the MCP section."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        # Truncated mid-write, half-written JSON, totally broken content —
        # the exact failure mode the atomic_write change is meant to
        # prevent from ever landing on disk, but we still need doctor to
        # cope if it encounters one (legacy installs, disk corruption).
        agent_path.write_text('{"tools": ["@kirocrew-c')

        issues: list[str] = []
        with self._mock_probe({}) as probe_mock:
            _doctor_mcp_tools(agent_path, issues)

        out = capsys.readouterr().out
        # Empty config → both managed servers report missing from mcpServers.
        assert "@kirocrew-core: ❌ missing from mcpServers" in out
        assert "@kirocrew-cron: ❌ missing from mcpServers" in out
        # No probe attempted since no server spec survived the parse failure.
        probe_mock.assert_not_called()

    @pytest.mark.parametrize("content", ["[1, 2, 3]", "42", "null", "true", '"a string"'])
    def test_valid_json_non_object_agent_config_does_not_crash(self, tmp_path, capsys, content):
        """A spec that is valid JSON but not an object (a list, a scalar,
        null) parses fine, so the json.loads try/except never fires — but
        every .get() on the result would raise AttributeError. Doctor must
        coerce it to an empty config, say what is wrong, and report cleanly,
        same as the truncated case above — and it must never rewrite the
        user's file with the coerced empty config."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        agent_path.write_text(content)

        issues: list[str] = []
        with self._mock_probe({}) as probe_mock:
            _doctor_mcp_tools(agent_path, issues)

        out = capsys.readouterr().out
        # The defect itself is named, not just its downstream symptoms.
        assert "agent spec is not a JSON object" in out
        # Empty config → both managed servers report missing from mcpServers.
        assert "@kirocrew-core: ❌ missing from mcpServers" in out
        assert "@kirocrew-cron: ❌ missing from mcpServers" in out
        # No probe attempted since no server spec survived the coercion.
        probe_mock.assert_not_called()
        # The no-clobber contract: doctor diagnoses the broken spec, it never
        # persists the coerced empty config over the user's original file.
        assert agent_path.read_text() == content
        assert "Auto-fixed" not in out


class TestDoctorMcpSpecGate:
    """Doctor's MCP checks consult the same ``spec_gate`` emission does.

    Spec emission (``agent.build_agent_config`` / ``_refresh_dynamic_fields``)
    deliberately omits — and retracts — the ``mcpServers`` entry of a managed
    server whose ``spec_gate`` is closed (feature disabled, or no driver for
    this platform). Before this gate, doctor demanded the entry's presence
    unconditionally, so every non-macOS host reported an unfixable
    ``@kirocrew-computer: missing from mcpServers`` and `kirocrew setup` could
    never clear it: the two sides drifted because only one consulted the gate.

    Each test pins the gate through the same registry doctor resolves it from
    (``agent._MANAGED_MCP_SERVERS``), so the resolution seam is exercised too,
    and no test depends on the host's real enable-state or platform.
    """

    def _mock_probe(self, seen: list[str]):
        """Patch ``probe_server`` to record which servers doctor launches."""
        from kiro_crew.mcp_discovery import McpServerInfo

        async def fake(target: McpServerInfo) -> McpServerInfo:
            seen.append(target.name)
            target.status = "ok"
            target.tools = []
            target.error = ""
            return target

        return patch("kiro_crew.cli_doctor.probe_server", side_effect=fake)

    def _pin_gate(self, gate):
        """Return a patch pinning ``kirocrew-computer``'s spec gate to *gate*."""
        from kiro_crew import agent

        pinned = dict(agent._MANAGED_MCP_SERVERS["kirocrew-computer"])
        pinned["spec_gate"] = gate
        return patch.dict(agent._MANAGED_MCP_SERVERS, {"kirocrew-computer": pinned})

    def _config_without_computer(self, path: Path) -> None:
        """Servers as spec emission writes them on a gated-off host: every
        managed always-on server EXCEPT the gated one, refs for all of them
        (emission deliberately leaves the ``tools`` ref alone when it retracts
        the entry, so ref-present-entry-absent is the designed steady state)."""
        from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS

        present = [n for n in ALWAYS_ON_BIN_MCP_SERVERS if n != "kirocrew-computer"]
        _write_agent_config(
            path,
            tools=[f"@{n}" for n in ALWAYS_ON_BIN_MCP_SERVERS],
            allowed=[f"@{n}" for n in present],
            servers={
                n: {"command": sys.executable, "args": [f"mcp-{n.split('-', 1)[1]}"]}
                for n in present
            },
        )

    def test_gate_closed_absent_entry_is_informational_not_an_issue(self, tmp_path, capsys):
        """Gate closed + entry absent = the healthy state on this host.

        The reported state: no hard error, no ``issues`` entry (so doctor
        exits 0), no auto-mount of the ref, and the line says WHY the server is
        absent rather than looking like a silent skip.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        self._config_without_computer(agent_path)
        before = agent_path.read_text(encoding="utf-8")
        issues: list[str] = []
        probed: list[str] = []
        with self._pin_gate(lambda: False), self._mock_probe(probed):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-computer: ℹ️  gated off on this host" in out
        assert "feature disabled or no driver" in out
        assert "@kirocrew-computer: ❌" not in out
        assert issues == []
        # Nothing mounted, minted, or probed for the gated-off server — and the
        # stale ref emission leaves behind is not reported as "half a grant".
        assert "kirocrew-computer" not in probed
        assert "referenced in tools but absent" not in out
        assert agent_path.read_text(encoding="utf-8") == before

    def test_gate_open_absent_entry_keeps_the_hard_error(self, tmp_path, capsys):
        """Gate open + entry absent is still a broken install and MUST fail."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        self._config_without_computer(agent_path)
        issues: list[str] = []
        with self._pin_gate(lambda: True), self._mock_probe([]):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-computer: ❌ missing from mcpServers (re-run `kirocrew setup`)" in out
        assert "@kirocrew-computer config" in issues

    def test_gate_raising_is_treated_as_open(self, tmp_path, capsys):
        """A gate that raises PAST ITS OWN HANDLING must not silence the error.

        This pins the helper's contract-level fail direction — deliberately
        the opposite of emission's ``_gated_off_servers()`` (which treats a
        raising gate as closed, because its open position spawns a backend):
        here "closed" is what suppresses the error, so a gate the helper
        cannot evaluate reports the missing entry. Note the shipped
        computer-use gate catches its own internal errors and ANSWERS False,
        so this path covers registry/contract failures and any future gate
        without an internal handler; the shipped gate's swallowed-error case
        is pinned separately below.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        def explode() -> bool:
            raise RuntimeError("gate unreadable")

        agent_path = tmp_path / "kirocrew.json"
        self._config_without_computer(agent_path)
        issues: list[str] = []
        with self._pin_gate(explode), self._mock_probe([]):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-computer: ❌ missing from mcpServers" in out
        assert "@kirocrew-computer config" in issues
        assert "gated off" not in out

    def test_shipped_gate_swallowing_internal_errors_reads_as_closed(self, tmp_path, capsys):
        """The shipped gate's own fail-closed answer is reported as-is.

        ``agent._computer_use_spec_gate`` catches its internal errors and
        answers False (its documented posture: an unreadable keystone must
        never hand out the desktop), so doctor cannot distinguish "unreadable
        internals" from "policy closed" through the boolean — and reporting
        closed is the emission-CONSISTENT answer: in that same state the entry
        genuinely is omitted from every emitted spec, so the ℹ️ line describes
        what the system actually does. This test pins that DELIVERED semantic
        so the helper's docstring cannot silently overclaim again; if the
        gate's exception contract ever changes, this test and the one above
        say exactly which behavior moved.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        self._config_without_computer(agent_path)
        issues: list[str] = []
        with (
            patch(
                "kiro_crew.computer_use.enable_state.is_enabled",
                side_effect=RuntimeError("keystone unreadable"),
            ),
            self._mock_probe([]),
        ):
            # No _pin_gate: the REAL registry gate runs, swallows the raise,
            # and answers False — the exact production shape.
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-computer: ℹ️  gated off on this host" in out
        assert issues == []

    def test_gate_closed_stale_entry_is_not_mounted_or_probed(self, tmp_path, capsys):
        """A leftover entry from when the gate was open must not deepen the hole.

        Doctor must not mount its ref into ``tools`` (kiro-cli would spawn a
        backend emission decided against), must not probe it (nothing should
        launch), and must not count it as an issue — the next config refresh
        retracts it.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools
        from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS

        agent_path = tmp_path / "kirocrew.json"
        present = list(ALWAYS_ON_BIN_MCP_SERVERS)
        _write_agent_config(
            agent_path,
            tools=[f"@{n}" for n in present if n != "kirocrew-computer"],
            allowed=[],
            servers={
                n: {"command": sys.executable, "args": [f"mcp-{n.split('-', 1)[1]}"]}
                for n in present
            },
        )
        issues: list[str] = []
        probed: list[str] = []
        with self._pin_gate(lambda: False), self._mock_probe(probed):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "stale mcpServers entry" in out
        assert issues == []
        assert "kirocrew-computer" not in probed
        # The other always-on servers were still probed — the skip is scoped.
        assert "kirocrew-core" in probed and "kirocrew-cron" in probed
        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert "@kirocrew-computer" not in updated["tools"]
        assert "@kirocrew-computer" not in updated["allowedTools"]

    def test_governance_denominator_skips_an_absent_gated_off_server(
        self, tmp_path, monkeypatch, capsys
    ):
        """The MCP Governance section applies the same rule to its marker count.

        On a governed non-macOS host the gated-off server has no entry to mark,
        so counting it as expected would re-create the same unfixable
        "re-run `kirocrew setup --agent-only`" loop in this section.
        """
        from kiro_crew import cli_doctor
        from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS

        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: True)

        class _Agent:
            mcp_registry_mode = True

        class _Cfg:
            agent = _Agent()

        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
        spec_path = tmp_path / "kirocrew.json"
        spec_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        n: {
                            "command": "kirocrew",
                            "args": [f"mcp-{n.split('-', 1)[1]}"],
                            "type": "registry",
                        }
                        for n in ALWAYS_ON_BIN_MCP_SERVERS
                        if n != "kirocrew-computer"
                    }
                }
            ),
            encoding="utf-8",
        )
        issues: list[str] = []
        with self._pin_gate(lambda: False):
            cli_doctor._doctor_mcp_governance(spec_path, issues)
        out = capsys.readouterr().out
        assert issues == []
        assert "markers missing" not in out
        # The gate-open direction keeps the failure: a genuinely missing
        # always-on server still reads as unmarked.
        issues2: list[str] = []
        with self._pin_gate(lambda: True):
            cli_doctor._doctor_mcp_governance(spec_path, issues2)
        assert issues2 == ["MCP registry markers"]
