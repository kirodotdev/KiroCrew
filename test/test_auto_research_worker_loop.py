"""The auto_research worker loop: arming, titling, pinning and stopping its autonudge
loop, suspending research loops while the app is disabled, and installing the
kirocrew-research agent. ``test_auto_research.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from auto_research_helpers import isolate_fixture  # noqa: F401 - fixture

from kiro_crew.apps.builtins.auto_research.handlers import create_campaign

# --- Worker loop launch (autonudge) ---


class TestLoopLaunch:
    @pytest.mark.asyncio
    async def test_launch_clears_tombstone_before_slow_marker_cleanup(self, monkeypatch):
        """A crash after settlement but before loop removal must not let the
        previous run's source tombstone stop a resumed run again."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AUTONUDGE_STOP_REASON

        order = []
        svc = MagicMock()
        svc.get_by_slot.return_value = SimpleNamespace(
            id="stale-stop",
            active=False,
            stopped_reason=AUTONUDGE_STOP_REASON,
        )
        svc.remove = AsyncMock(side_effect=lambda _loop_id: order.append("tombstone"))
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(h, "_clear_worker_done_marker", lambda _cid: order.append("marker"))

        await h._launch_loop(SimpleNamespace(app={}), "a1b2c3d4")

        svc.remove.assert_awaited_once_with("stale-stop")
        assert order == ["tombstone", "marker"]

    @pytest.mark.asyncio
    async def test_launch_arms_autonudge(self, monkeypatch):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = MagicMock()
        state.get_or_create_slot.return_value = SimpleNamespace(key=f"research-{c['id']}")
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        svc.add.assert_awaited_once()
        kw = svc.add.call_args.kwargs
        assert kw["slot_key"] == f"research-{c['id']}"
        assert c["id"] in kw["message"]
        assert state.get_or_create_slot.call_args.kwargs["agent"] == "kirocrew-research"
        # Worker slot is auto-approved so the loop doesn't stall on tool prompts.
        assert state.get_or_create_slot.return_value._trust is True

    @pytest.mark.asyncio
    async def test_launch_pins_campaign_model_on_slot(self, monkeypatch):
        # The campaign's explicit model pick must reach the worker slot both at
        # creation (get_or_create_slot kwarg) and on resume (explicit re-pin:
        # the factory only applies kwargs when it CREATES the slot).
        from kiro_crew.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {
                "question": "Research question about something here",
                "sources": ["web"],
                "model": "test-model-x",
            }
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = MagicMock()
        slot = SimpleNamespace(key=f"research-{c['id']}", model="stale-previous-model")
        state.get_or_create_slot.return_value = slot
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        assert state.get_or_create_slot.call_args.kwargs["model"] == "test-model-x"
        assert slot.model == "test-model-x"

    @pytest.mark.asyncio
    async def test_launch_defaults_to_inherit_model(self, monkeypatch):
        # No explicit pick -> '' is threaded through, meaning the slot inherits
        # the research agent's / backend's default resolution.
        from kiro_crew.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = MagicMock()
        slot = SimpleNamespace(key=f"research-{c['id']}")
        state.get_or_create_slot.return_value = slot
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        assert state.get_or_create_slot.call_args.kwargs["model"] == ""
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_launch_sets_slot_title_from_campaign_name(self, monkeypatch):
        # The worker slot is autonudge-driven (messages are role "nudge", not
        # "user"), so the LLM auto-titler never fires and the slot would show
        # the "New Session…" placeholder. _launch_loop must title it from the
        # campaign's human name, lock _titled, and push a live update.
        from kiro_crew.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {
                "name": "SQLite vs Postgres deep dive",
                "question": "Compare SQLite and PostgreSQL for desktop apps",
                "sources": ["web"],
            }
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = MagicMock()
        slot = SimpleNamespace(key=f"research-{c['id']}")
        state.get_or_create_slot.return_value = slot
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        assert slot.title == "SQLite vs Postgres deep dive"
        assert slot._titled is True
        state.push_slot_title.assert_called_once_with(slot.key, "SQLite vs Postgres deep dive")
        # Title persisted under the slot's canonical history key so it survives
        # a gateway restart.
        state.conversation_log.set_title.assert_called_once()
        assert state.conversation_log.set_title.call_args.args[1] == "SQLite vs Postgres deep dive"

    @pytest.mark.asyncio
    async def test_launch_title_persist_failure_still_arms_worker(self, monkeypatch):
        # Title persistence is best-effort: a set_title I/O failure must NOT
        # propagate and leave the campaign running without its worker armed.
        from kiro_crew.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = MagicMock()
        state.get_or_create_slot.return_value = SimpleNamespace(key=f"research-{c['id']}")
        state.conversation_log.set_title.side_effect = OSError("disk full")
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        # Worker still armed despite the persistence failure.
        svc.add.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_launch_title_fails_closed_without_redactors(self, monkeypatch):
        # The campaign name is user-controlled. If the security redactors are
        # unavailable (_HAS_SECURITY False), the title MUST fall back to the
        # non-user-derived key rather than persisting/broadcasting raw input.
        from kiro_crew.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {
                "name": "http://evil.example/leak?token=abc",
                "question": "Research question about something here",
                "sources": ["web"],
            }
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(h, "_HAS_SECURITY", False)
        state = MagicMock()
        slot = SimpleNamespace(key=f"research-{c['id']}")
        state.get_or_create_slot.return_value = slot
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        assert slot.title == f"research-{c['id']}"
        assert "evil.example" not in slot.title

    @pytest.mark.asyncio
    async def test_launch_writes_brief(self, monkeypatch):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {
                "question": "Compare SQLite and PostgreSQL for desktop apps",
                "sub_questions": ["concurrency model?", "deployment tradeoffs?"],
                "sources": ["web"],
                "success_criteria": "tests pass and build is green",
            }
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = MagicMock()
        state.get_or_create_slot.return_value = SimpleNamespace(key=f"research-{c['id']}")
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        brief = (h._campaign_dir(c["id"]) / "brief.md").read_text(encoding="utf-8")
        assert "Compare SQLite and PostgreSQL" in brief
        assert "concurrency model?" in brief
        assert "Definition of Done" in brief
        assert "tests pass and build is green" in brief

    @pytest.mark.asyncio
    async def test_launch_noop_without_service(self, monkeypatch):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        monkeypatch.setattr(h, "_autonudge_instance", lambda: None)
        await h._launch_loop(SimpleNamespace(app={}), "a1b2c3d4")  # must not raise

    @pytest.mark.asyncio
    async def test_stop_removes_loop(self, monkeypatch):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        svc = MagicMock()
        svc.remove = AsyncMock()
        svc.get_by_slot.return_value = SimpleNamespace(id="loop1")
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("a1b2c3d4", remove=True)
        svc.remove.assert_awaited_once_with("loop1", stop_reason="")

    @pytest.mark.asyncio
    async def test_pause_deactivates_loop(self, monkeypatch):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        svc = MagicMock()
        svc.update = AsyncMock()
        svc.get_by_slot.return_value = SimpleNamespace(id="loop1")
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("a1b2c3d4", remove=False)
        svc.update.assert_awaited_once_with("loop1", active=False)


class TestSuspendResearchLoopsWhileDisabled:
    """Disabling the app must deactivate research autonudge loops AND clear their
    slot trust, so a disabled app grants no standing auto-approval past the 24h cap
    (the watchdog's per-campaign trust expiry is skipped while disabled)."""

    @pytest.mark.asyncio
    async def test_deactivates_research_loops_and_clears_trust(self, monkeypatch):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        svc = MagicMock()
        svc.update = AsyncMock()
        research_loop = SimpleNamespace(id="loopR", slot_key="research-a1b2c3d4", active=True)
        other_loop = SimpleNamespace(id="loopX", slot_key="chat:main", active=True)
        svc.list_all.return_value = [research_loop, other_loop]
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        research_slot = SimpleNamespace(_trust=True)
        state = SimpleNamespace(_slots={"research-a1b2c3d4": research_slot})

        await h._suspend_research_loops_while_disabled(state)

        # Only the research loop is deactivated; the unrelated chat loop is left alone.
        svc.update.assert_awaited_once_with("loopR", active=False)
        # The research slot's standing trust is revoked.
        assert research_slot._trust is False

    @pytest.mark.asyncio
    async def test_idempotent_when_already_inactive_and_untrusted(self, monkeypatch):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        svc = MagicMock()
        svc.update = AsyncMock()
        svc.list_all.return_value = [
            SimpleNamespace(id="loopR", slot_key="research-a1b2c3d4", active=False)
        ]
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = SimpleNamespace(_slots={"research-a1b2c3d4": SimpleNamespace(_trust=False)})

        await h._suspend_research_loops_while_disabled(state)

        # Nothing to do: an already-inactive loop is not re-updated.
        svc.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_autonudge_service_is_safe(self, monkeypatch):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        monkeypatch.setattr(h, "_autonudge_instance", lambda: None)
        # Must not raise when the service is unavailable.
        await h._suspend_research_loops_while_disabled(SimpleNamespace(_slots={}))


# --- kirocrew-research core agent install ---


class TestResearchAgentInstall:
    def test_installs_kirocrew_research(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        monkeypatch.setattr(
            agent,
            "build_agent_config",
            lambda: {"name": "kirocrew", "prompt": "file://x", "mcpServers": {}, "tools": []},
        )
        agent._install_research_agent()
        data = json.loads((tmp_path / "kirocrew-research.json").read_text(encoding="utf-8"))
        assert data["name"] == "kirocrew-research"
        assert "research" in data["prompt"].lower()

    @staticmethod
    def _floor_builtins() -> set[str]:
        """The builtins whose always-on gate floor forbids a blanket grant.

        Derived from the governance maps rather than hardcoding the four names,
        so the assertion keeps holding if the template (or the map) changes.
        """
        from kiro_crew.platform.governance import _FLOOR_GATE_SCOPES, BUILTIN_TOOL_SCOPES

        return {
            name
            for name, scopes in BUILTIN_TOOL_SCOPES.items()
            if _FLOOR_GATE_SCOPES.intersection(scopes)
        }

    def _install_real(self, monkeypatch, tmp_path) -> dict:
        """Install with the REAL build_agent_config — the fix under test lives
        inside it, so stubbing it (as the identity test above does) would
        bypass exactly the path under test."""
        from kiro_crew import agent

        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        agent._install_research_agent()
        return json.loads((tmp_path / "kirocrew-research.json").read_text(encoding="utf-8"))

    def test_no_floor_scoped_builtin_is_blanket_approved(self, monkeypatch, tmp_path):
        """The installed research spec must not auto-approve a floor builtin.

        The shipped template's ``allowedTools`` starts with floor-gated
        builtins (``fs_read``, ``code``, …). ``code`` is the consequential one:
        auto-approved, its calls never reach ``hooks.on_tool_call``, so the
        sensitive-path check, the write-protected-config check, the governance
        ceiling and the SEL deny record are all skipped — for the least
        supervised agent in the product.
        """
        data = self._install_real(monkeypatch, tmp_path)
        leaked = self._floor_builtins().intersection(data.get("allowedTools", []))
        assert not leaked, f"floor-gated builtins on the blanket auto-approve list: {leaked}"

    def test_floor_builtins_stay_mounted(self, monkeypatch, tmp_path):
        """The regression guard proving capability was not removed.

        The withhold removes the BLANKET grant only: the tool stays in
        ``tools`` (mounted), and read-only file tools still auto-approve via
        hooks AFTER the floor runs — so a stock install gains no new prompt
        and the unattended research loop is not handed a stall.
        """
        from kiro_crew import agent

        data = self._install_real(monkeypatch, tmp_path)
        shipped_tools = set(agent.get_shipped_tools()["tools"])
        expected_mounted = self._floor_builtins().intersection(shipped_tools)
        assert expected_mounted, "template stopped mounting floor builtins; test needs updating"
        missing = expected_mounted - set(data.get("tools", []))
        assert not missing, f"filter must not unmount tools, only ungrant them: {missing}"

    def test_withhold_leaves_the_standard_sel_record(self, monkeypatch, tmp_path):
        """Withholding is a permission DECISION — same event, same feed as the
        other ``allowedTools`` writers (``mcp_auto_approve_withheld``)."""
        from kiro_crew import agent

        events: list[dict] = []
        monkeypatch.setattr(
            agent,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        self._install_real(monkeypatch, tmp_path)
        withheld = [e for e in events if e.get("operation") == "mcp_auto_approve_withheld"]
        assert withheld, "the floor withhold must leave a SEL record"
        # The record names what lost its grant, so an operator can explain a prompt.
        assert any("code" in e.get("resources", "") for e in withheld)

    def test_audit_failure_never_fails_the_install(self, monkeypatch, tmp_path):
        """Best-effort audit: SEL being unavailable must not break the install."""
        from kiro_crew import agent

        def _broken_sel():
            raise RuntimeError("SEL unavailable")

        monkeypatch.setattr(agent, "sel", _broken_sel)
        data = self._install_real(monkeypatch, tmp_path)
        assert data["name"] == "kirocrew-research"
        assert not self._floor_builtins().intersection(data.get("allowedTools", []))
