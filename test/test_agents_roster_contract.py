"""``GET /api/agents`` ships an explicit allowlist, never the whole record.

Building each row with ``{**dataclasses.asdict(agent_cfg)}`` would make its
response contract "every field ``KiroCrewAgentConfig`` has, plus every field
anyone adds later", automatically — a field added by someone who never looked
at this endpoint would ship to the browser by omission. Both row sources use an
explicit allowlist instead, mirroring the rule ``handlers/members.py`` already
documents for ``GET /api/members``.

These tests are the half that keeps it converted. The key set is pinned as a
literal, and a separate ratchet compares that literal against the live record
so a field added to ``KiroCrewAgentConfig`` fails here rather than silently
appearing in the response — which is the whole point of the change: the default
becomes "nothing unless someone adds it".
"""

from __future__ import annotations

import dataclasses
import json
import types
import unittest.mock
from pathlib import Path
from typing import cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections import KiroCrewAgentConfig, _safe_avatar
from kiro_crew.dashboard.handlers.agents import (
    _agent_roster_row,
    _carries_mask,
    _name_would_be_masked,
    _roster_mask,
)

# The EXACT key set every row carries, from BOTH sources (the ``cfg.agents``
# rows and the project-scope rows). ``name`` and ``scope`` are handler-added;
# the rest are allowlisted record fields. Changing this set is a
# network-boundary contract change: check the frontend consumers first
# (``website/src/components/AgentSelector.tsx`` declares the ``KiroCrewAgent``
# interface the dashboard reads).
ROSTER_ROW_KEYS = frozenset(
    {
        "name",
        "scope",
        "kiro_agent",
        "workspace",
        "memory_store",
        "model",
        "reasoning_effort",
        "display_name",
        "description",
        "triggers",
        "source",
        "session_color",
        "avatar",
    }
)

# Record fields deliberately withheld, each verified to have no consumer in
# ``website/src``: the two watchdog windows are backend scheduling knobs the
# roster does not render, ``telegram_account`` is deprecated and inert, and
# ``starred`` is a Crew Members roster preference that only ``GET /api/members``
# renders (the crew manager has no star affordance).
# ``member_id`` is execution attribution, not a template-picker field.
WITHHELD_RECORD_FIELDS = frozenset(
    {
        "member_id",
        "watchdog_tool_stall_suspect_secs",
        "watchdog_tool_stall_hard_cap_secs",
        "telegram_account",
        "starred",
    }
)


def _make_app() -> web.Application:
    """Minimal aiohttp app with just the roster endpoint."""
    from kiro_crew.dashboard.handlers import api_kirocrew_agents

    app = web.Application()
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


def _seed_config_with_every_field_set() -> dict:
    """A config whose agent sets EVERY record field, withheld ones included.

    A withheld field left at its default is indistinguishable from a withheld
    field that is absent, so the fixture gives each one a distinctive value: an
    assertion on its absence then actually proves the allowlist rather than the
    default happening to be falsy.
    """
    return {
        "agents": {
            "roster-probe": {
                "kiro_agent": "kirocrew",
                "workspace": "probe-ws",
                "memory_store": "probe-ms",
                "model": "claude-opus-5",
                "reasoning_effort": "high",
                "description": "probe description",
                "triggers": "probe triggers",
                "source": "kirocrew",
                "session_color": "#abcdef",
                # Withheld — must NOT appear in the response.
                "member_id": "member-roster-probe",
                "watchdog_tool_stall_suspect_secs": 111.0,
                "watchdog_tool_stall_hard_cap_secs": 222.0,
                "telegram_account": "probe-telegram-binding",
            },
        },
        "default_agent": "roster-probe",
        "workspaces": {"default": {"dir": "workspace"}, "probe-ws": {"dir": "workspace"}},
    }


class TestRosterRowKeySet:
    """The response's exact key set, measured at the endpoint."""

    @pytest.mark.asyncio
    async def test_global_row_ships_exactly_the_allowlist(self, tmp_path: Path) -> None:
        """A ``cfg.agents`` row carries the allowlist and nothing else."""
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/agents")
                assert resp.status == 200
                row = {a["name"]: a for a in (await resp.json())["agents"]}["roster-probe"]

        assert set(row) == ROSTER_ROW_KEYS
        # The allowlisted values still arrive — an allowlist that shipped
        # the right KEYS with empty values would pass a key-set assertion
        # while breaking every consumer.
        assert row["scope"] == "global"
        assert row["workspace"] == "probe-ws"
        assert row["memory_store"] == "probe-ms"
        assert row["model"] == "claude-opus-5"
        assert row["reasoning_effort"] == "high"
        assert row["description"] == "probe description"
        assert row["triggers"] == "probe triggers"
        assert row["session_color"] == "#abcdef"
        # And the withheld fields are gone even though the config set them
        # to distinctive non-default values.
        assert not (set(row) & WITHHELD_RECORD_FIELDS)
        assert "probe-telegram-binding" not in json.dumps(row)

    @pytest.mark.asyncio
    async def test_project_row_ships_the_same_key_set(self, monkeypatch, tmp_path: Path) -> None:
        """A project-scope row carries the SAME keys as a global row.

        The two sources are separate spreads, so they could drift
        into different key sets; pinning both is what makes one allowlist the
        answer for the whole response.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: "/probe/project",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            lambda project_dir, **kw: frozenset({"project-only-agent"}),
        )
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            app = _make_app()
            # Truthy state with no conversation log: the handler then takes
            # the project-scan branch and skips the usage re-sort.
            app["state"] = types.SimpleNamespace(conversation_log=None)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/agents")
                assert resp.status == 200
                rows = {a["name"]: a for a in (await resp.json())["agents"]}

        assert "project-only-agent" in rows, "project scan produced no row to check"
        project_row = rows["project-only-agent"]
        assert set(project_row) == ROSTER_ROW_KEYS
        assert set(project_row) == set(rows["roster-probe"])
        assert project_row["scope"] == "project"
        assert not (set(project_row) & WITHHELD_RECORD_FIELDS)

    @pytest.mark.asyncio
    async def test_project_path_query_scopes_discovery_with_no_slot(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``?project_path=<dir>`` drives project-agent discovery even when the
        session has NO active project.

        This is the folder create/settings modal's case: there is no chat slot
        yet, so ``active_project_dir`` answers ``None``, and the ONLY signal of
        which directory to scan is the query param. Regression guard for the
        bug where the folder modal's agent picker never listed project agents
        because the handler read only the slot's project.
        """
        seen: dict[str, str] = {}

        def _fake_resolve(raw: str) -> tuple[str, bool]:
            # Stand in for the realpath/sensitivity/isdir core so the test needs
            # no real directory: echo the raw path back as a valid, non-sensitive
            # directory.
            return raw, False

        def _fake_names(project_dir, **kw):
            seen["dir"] = str(project_dir)
            return frozenset({"project-only-agent"})

        # No slot -> no active project. The query param is the only source.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: None,
        )
        # ?project_path= is owner-only; this caller is the owner.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents._resolve_roster_project_path",
            _fake_resolve,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            _fake_names,
        )
        # Beneath ``tmp_path``, not the OS temp dir: a run killed mid-test must not
        # leave a stray config behind (anchor no-test-side-effects). The explicit
        # unlink below stays -- destruction registered in the same scope as creation.
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/draft/folder/dir")
                    assert resp.status == 200
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}

            # The query-param directory reached discovery, not the (None) slot.
            assert seen.get("dir") == "/draft/folder/dir"
            assert "project-only-agent" in rows
            assert rows["project-only-agent"]["scope"] == "project"
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_app_token_caller_gets_the_same_keys_with_scrubbed_values(
        self, tmp_path: Path
    ) -> None:
        """End to end: the caller class is resolved from the request, not passed in.

        The row-level tests cover ``redact=`` directly; this one covers the
        WIRING -- that the handler reads the caller class off the request at all.
        A middleware sets ``request["app"]``, which is what ``token_auth`` does
        for a verified app token and what ``members.py::_deny_app_caller`` reads.
        """
        probe = "AKIAIOSFODNN7EXAMPLE"
        seed = _seed_config_with_every_field_set()
        seed["agents"]["roster-probe"]["description"] = f"see {probe}"
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")

        @web.middleware
        async def _as_app(request: web.Request, handler):  # type: ignore[no-untyped-def]
            request["app"] = "probe-app"
            return await handler(request)

        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            app = web.Application(middlewares=[_as_app])
            from kiro_crew.dashboard.handlers import api_kirocrew_agents

            app.router.add_get("/api/agents", api_kirocrew_agents)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/agents")
                assert resp.status == 200
                body = await resp.json()
                row = {a["name"]: a for a in body["agents"]}["roster-probe"]

        assert set(row) == ROSTER_ROW_KEYS, "key set must not depend on caller class"
        assert probe not in json.dumps(row), "app token received an unscrubbed value"

    @pytest.mark.asyncio
    async def test_sensitive_project_path_is_denied_and_scans_nothing(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A sensitive ``?project_path=`` is refused: no scan, no project rows,
        and the global roster still ships (the whole response never fails).
        """
        scanned: list[str] = []

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: None,
        )
        # Owner caller — reaches the sensitivity gate (a non-owner would be
        # refused earlier by the owner gate; that is a separate test).
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        # Denied: resolved path empty, denied flag true.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents._resolve_roster_project_path",
            lambda raw: ("", True),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            lambda project_dir, **kw: scanned.append(str(project_dir)) or frozenset(),
        )
        # Beneath ``tmp_path``, not the OS temp dir: a run killed mid-test must not
        # leave a stray config behind (anchor no-test-side-effects). The explicit
        # unlink below stays -- destruction registered in the same scope as creation.
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/home/u/.aws")
                    assert resp.status == 200
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}

            # No scan happened, no project rows, but the global roster survived.
            assert scanned == []
            assert all(r["scope"] == "global" for r in rows.values())
            assert "roster-probe" in rows
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_project_path_is_ignored_for_a_non_owner_caller(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``?project_path=`` is owner-only. A non-owner caller's param is
        ignored — the scan is NEVER pointed at the caller-supplied directory,
        so a non-owner cannot enumerate agent names under an arbitrary path.
        Falls back to the slot-derived project exactly as before the param.
        """
        scanned: list[str] = []
        events: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._sel", lambda: _FakeSel())
        # NOT the owner -> the query param must be ignored.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        # Slot-derived project is None, so with the param ignored there is no
        # scan at all.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: None,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            lambda project_dir, **kw: scanned.append(str(project_dir)) or frozenset({"leaked"}),
        )
        # Beneath ``tmp_path``, not the OS temp dir: a run killed mid-test must not
        # leave a stray config behind (anchor no-test-side-effects). The explicit
        # unlink below stays -- destruction registered in the same scope as creation.
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/some/other/dir")
                    assert resp.status == 200
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}

            # The caller-supplied dir was NEVER scanned, and no project row leaked.
            assert scanned == []
            assert "leaked" not in rows
            assert all(r["scope"] == "global" for r in rows.values())
            # F1: the non-owner attempt to point the scan at an arbitrary dir is
            # itself audited as a denied event, not silently ignored.
            audits = [e for e in events if e.get("operation") == "api_kirocrew_agents"]
            assert audits, "non-owner project_path attempt emitted no SEL event"
            assert audits[0]["outcome"] == "denied"
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_non_owner_project_path_does_not_leak_the_active_slots_project(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A non-owner's ``?project_path=B`` is refused (per the test above),
        but when the caller's chat SLOT is on an unrelated project A, the
        refusal must NOT fall through to serving A's roster. A request that
        named a directory asked for that directory's scope; an unresolved or
        refused path ships global-only rows, never a silent substitution of a
        different project's agents. The active-slot fallback fires only when
        no ``project_path`` was supplied at all, so a non-owner's B request
        cannot fall into the same branch as no-path-at-all and leak slot
        project A's agents to a caller who asked about B.
        """
        scanned: list[str] = []

        class _FakeSel:
            def log_api_access(self, **kw):
                pass

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._sel", lambda: _FakeSel())
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        # The active chat slot IS on a different, real project (A) — this is
        # the condition the mocked-to-None test above cannot exercise.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: "/slot/project-A",
        )

        def _fake_project_agent_names(project_dir, **kw):
            scanned.append(str(project_dir))
            if project_dir == "/slot/project-A":
                return frozenset({"leaked-from-A"})
            return frozenset()

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            _fake_project_agent_names,
        )
        # Beneath ``tmp_path``, not the OS temp dir: a run killed mid-test must not
        # leave a stray config behind (anchor no-test-side-effects). The explicit
        # unlink below stays -- destruction registered in the same scope as creation.
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/some/other/dir-B")
                    assert resp.status == 200
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}

            # Neither the caller-supplied dir B nor the slot's project A was
            # scanned: a refused/unresolved supplied path ships global-only.
            assert scanned == []
            assert "leaked-from-A" not in rows
            assert all(r["scope"] == "global" for r in rows.values())
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_owner_directed_scan_is_sel_audited(self, monkeypatch, tmp_path: Path) -> None:
        """An owner directing the roster scan at an arbitrary ``?project_path=``
        directory is a security-relevant action, so the honored (non-denied)
        scan emits a SEL audit event with ``outcome="allowed"`` — not only the
        denied path. Without it the audit trail would record refusals but never
        the scans that succeeded.
        """
        events: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._sel", lambda: _FakeSel())
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: None,
        )
        # A valid, non-sensitive directory (denied=False) -> the honored branch.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents._resolve_roster_project_path",
            lambda raw: (raw, False),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            lambda project_dir, **kw: frozenset(),
        )
        # Beneath ``tmp_path``, not the OS temp dir: a run killed mid-test must not
        # leave a stray config behind (anchor no-test-side-effects). The explicit
        # unlink below stays -- destruction registered in the same scope as creation.
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/draft/dir")
                    assert resp.status == 200

            audits = [e for e in events if e.get("operation") == "api_kirocrew_agents"]
            assert audits, "the honored owner scan emitted no SEL audit event"
            assert audits[0]["outcome"] == "allowed"
            assert audits[0]["source"] == "dashboard"
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_allowed_scan_fails_closed_when_audit_write_fails(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """If the SEL audit write FAILS on an allowed owner scan, the scan is
        REFUSED (fail-closed) and the refusal is reported as an ERROR the client
        can distinguish: the caller-supplied directory is never read, and the
        response is 503 rather than a 200 carrying global rows only. An unaudited
        read of a security-relevant scan is not permitted — the audit trail is the
        point of gating it — and a refusal that arrives shaped like success is
        indistinguishable from "this directory declares no agents", which is the
        very bug the endpoint exists to fix.
        """
        scanned: list[str] = []
        audit_calls: list[dict] = []

        class _FailingSel:
            def log_api_access(self, **kw):
                audit_calls.append(kw)
                # Model the REAL component: only a critical write raises; a
                # queued (non-critical) write swallows the failure. If the
                # handler did not pass critical=True, this would not raise and
                # the fail-closed branch would be unreachable (the bug GPT/Opus
                # flagged).
                if kw.get("critical"):
                    raise RuntimeError("audit sink down")

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._sel", lambda: _FailingSel())
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: None,
        )
        # A valid, non-sensitive directory -> would be the "allowed" scan path.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents._resolve_roster_project_path",
            lambda raw: (raw, False),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            lambda project_dir, **kw: scanned.append(str(project_dir))
            or frozenset({"project-only-agent"}),
        )
        # Beneath ``tmp_path``, not the OS temp dir: a run killed mid-test must not
        # leave a stray config behind (anchor no-test-side-effects). The explicit
        # unlink below stays -- destruction registered in the same scope as creation.
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/draft/dir")
                    # 503, NOT a 200 carrying global rows: the client cannot tell
                    # "this directory declares no agents" from "the scan was
                    # refused" if the refusal ships as success, so a wedged audit
                    # sink would silently empty the picker and could block Save on
                    # a valid project agent. Answering with an error is what lets
                    # the modal render the refusal state it already has.
                    assert resp.status == 503

            # Audit failed -> scan refused: the dir was never read.
            assert scanned == []
            # The allowed-scan audit MUST be critical (synchronous/raising), or
            # the fail-closed branch is unreachable against the real SEL writer.
            allowed = [
                c
                for c in audit_calls
                if c.get("operation") == "api_kirocrew_agents" and c.get("outcome") == "allowed"
            ]
            assert allowed, "no allowed-scan audit attempted"
            assert allowed[0].get("critical") is True
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_owner_empty_path_is_not_allowed_audited_and_no_slot_fallback(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """An owner ``?project_path=`` that resolves EMPTY (a typo / nonexistent
        dir: ``("", False)`` — not denied, just no directory) must NOT record an
        ``allowed`` audit event (no scan ran), and must NOT fall back to the
        active chat slot's project (which would present an unrelated project's
        agents as this folder's roster). Global rows only.
        """
        events: list[dict] = []
        slot_scanned: list[str] = []

        class _FakeSel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._sel", lambda: _FakeSel())
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        # A slot project EXISTS — the test proves it is NOT used when the owner
        # supplied a (honored) path that happened to resolve empty.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: "/slot/project",
        )
        # Nonexistent path: honored (not denied) but resolves to "".
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents._resolve_roster_project_path",
            lambda raw: ("", False),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            lambda project_dir, **kw: slot_scanned.append(str(project_dir))
            or frozenset({"slot-agent"}),
        )
        # Beneath ``tmp_path``, not the OS temp dir: a run killed mid-test must not
        # leave a stray config behind (anchor no-test-side-effects). The explicit
        # unlink below stays -- destruction registered in the same scope as creation.
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/does/not/exist")
                    assert resp.status == 200
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}

            # No scan of the slot project, and its agent never leaks in.
            assert slot_scanned == []
            assert "slot-agent" not in rows
            assert all(r["scope"] == "global" for r in rows.values())
            # No "allowed" event for a scan that never ran.
            audits = [e for e in events if e.get("operation") == "api_kirocrew_agents"]
            assert not any(e["outcome"] == "allowed" for e in audits)
        finally:
            tmp.unlink(missing_ok=True)


class TestRosterRowIsAnAllowlistNotASpread:
    """The property that survives the record growing."""

    def test_record_field_added_later_is_not_shipped_by_omission(self) -> None:
        """Every ``KiroCrewAgentConfig`` field is either allowlisted or withheld.

        This is the ratchet. It fails when a field is ADDED to the record, and
        that failure is the feature: the author has to decide whether the
        browser should see it, instead of a spread deciding for them.
        """
        record_fields = {f.name for f in dataclasses.fields(KiroCrewAgentConfig)}
        unclassified = record_fields - ROSTER_ROW_KEYS - WITHHELD_RECORD_FIELDS
        assert not unclassified, (
            f"KiroCrewAgentConfig grew {sorted(unclassified)}. GET /api/agents is an "
            "explicit allowlist (#8454), so decide deliberately: add the field to "
            "_agent_roster_row AND ROSTER_ROW_KEYS if the dashboard needs it, or to "
            "WITHHELD_RECORD_FIELDS if it must not leave the process."
        )
        # The withheld set must name real fields — a typo there would silently
        # stop classifying anything and let the next added field through.
        assert WITHHELD_RECORD_FIELDS <= record_fields
        # And the allowlist must not claim a record field that does not exist.
        assert ROSTER_ROW_KEYS - {"name", "scope"} <= record_fields

    def test_an_attribute_the_allowlist_does_not_name_is_dropped(self) -> None:
        """Behavioral proof, not just a literal comparison.

        A record carrying an extra attribute — the shape of a field added
        later — serializes to the same key set. Under the old spread this row
        would have carried ``secret_future_field``.
        """

        def _clone(f: dataclasses.Field) -> dataclasses.Field:
            """Rebuild ONE field, preserving how its default is supplied.

            A probe that REBUILDS the class it inspects can manufacture a failure
            that does not exist in the class. ``avatar`` uses
            ``default_factory=dict`` -- a mutable default must -- so its
            ``f.default`` is ``dataclasses.MISSING``; passing that straight to
            ``field(default=...)`` produces a field with NO default, and a
            non-default field after defaulted ones is a hard
            ``TypeError: non-default argument 'avatar' follows default argument``
            at class-creation time. That error was this probe's, not the record's:
            ``KiroCrewAgentConfig()`` instantiates fine on a clean tree.
            """
            if f.default_factory is not dataclasses.MISSING:
                return dataclasses.field(default_factory=f.default_factory)
            return dataclasses.field(default=f.default)

        grown = dataclasses.make_dataclass(
            "GrownAgentConfig",
            [(f.name, f.type, _clone(f)) for f in dataclasses.fields(KiroCrewAgentConfig)]
            + [("secret_future_field", str, dataclasses.field(default="must-not-ship"))],
        )
        row = _agent_roster_row("grown", "global", cast(KiroCrewAgentConfig, grown()), redact=False)
        assert set(row) == ROSTER_ROW_KEYS
        assert "secret_future_field" not in row
        assert "must-not-ship" not in json.dumps(row)


class TestUnshowableValuesAreMasked:
    """The read half: nothing that cannot be shown verbatim leaves as content."""

    PROBE = "AKIAIOSFODNN7EXAMPLE"
    UNCOERCED_BY_LOADER = ("kiro_agent", "workspace", "memory_store", "description", "source")
    # ``avatar`` is the ONE structured value a row ships, so the uniform
    # string sweeps below do not apply to it: it is shape-allowlisted by
    # ``_safe_avatar`` with masking confined to user-authored ``traits``
    # values, and is pinned separately -- in BOTH directions -- by
    # ``TestAvatarIsShapeAllowlistedNotMasked``. Excluded here rather than
    # softening these assertions, so the rule for every other field stays
    # "the sentinel, exactly".
    RECORD_FIELDS_SHIPPED = tuple(sorted(ROSTER_ROW_KEYS - {"name", "scope", "avatar"}))

    def _full(self) -> KiroCrewAgentConfig:
        return KiroCrewAgentConfig(
            kiro_agent=self.PROBE,
            workspace=f"ws-{self.PROBE}",
            memory_store=f"ms-{self.PROBE}",
            triggers=f"use when {self.PROBE}",
            model=self.PROBE,
            reasoning_effort=self.PROBE,
            session_color=self.PROBE,
            display_name=f"label {self.PROBE}",
            description=f"see {self.PROBE}",
            source=self.PROBE,
        )

    @pytest.mark.parametrize("redact", [False, True])
    def test_no_record_field_ships_a_credential_shaped_value(self, redact: bool) -> None:
        """Uniform across callers: an earlier revision exempted the fields the
        agents page writes back, which encoded a claim about the CLIENT that this
        side could not enforce (the PUT accepts `description` and `source` too)."""
        row = _agent_roster_row("probe", "global", self._full(), redact=redact)
        for field in self.RECORD_FIELDS_SHIPPED:
            assert self.PROBE not in row[field], f"{field} shipped unmasked"
            assert _carries_mask(row[field]), f"{field} should be the sentinel"

    @pytest.mark.parametrize("redact", [False, True])
    @pytest.mark.parametrize("field", UNCOERCED_BY_LOADER)
    def test_a_non_string_is_masked_not_emptied(self, field: str, redact: bool) -> None:
        """The loader lets an object through five declared-`str` fields.

        Masking rather than coercing to `""` is what lets the write-side rule
        PRESERVE it: an echoed `""` would read as a genuine edit and overwrite the
        stored value, which was a real defect in an earlier revision.
        """
        cfg = KiroCrewAgentConfig()
        object.__setattr__(cfg, field, {"nested": "object"})
        row = _agent_roster_row("probe", "global", cfg, redact=redact)
        assert _carries_mask(row[field])
        assert "nested" not in json.dumps(row)

    def test_every_value_is_a_string_except_the_one_structured_field(self) -> None:
        """`dict[str, object]` is honest about exactly one field, not a loophole.

        Every value is a `str` but `avatar`, which is a `dict` the dashboard needs
        verbatim. Asserting the exception BY NAME means a second structured field
        cannot appear without this test failing.
        """
        cfg = KiroCrewAgentConfig()
        object.__setattr__(cfg, "description", {"nested": "object"})
        for redact in (False, True):
            row = _agent_roster_row("probe", "global", cfg, redact=redact)
            assert isinstance(row["avatar"], dict)
            non_str = {k for k, v in row.items() if not isinstance(v, str)}
            assert non_str == {"avatar"}, f"unexpected structured value(s): {non_str}"

    def test_owner_keeps_name_addressable_but_app_token_does_not_need_it(self) -> None:
        """``name`` survives only where something can actually address it.

        A GLOBAL row's name is its only handle -- it addresses
        ``/api/agents/{name}`` for edit and delete and keys the usage sort -- and
        masking it for a non-app caller would buy nothing, since the same names
        are readable unmasked from ``GET /api/config/kirocrew`` as the ``agents``
        map's KEYS (``_masked_config_dict`` masks only schema-``sensitive``
        VALUES).

        A PROJECT row is masked for every caller. That argument does not transfer
        to it: project names come from a filesystem scan and appear in no config.
        Nothing can address one either -- both mutating routes 404 on a name
        absent from ``cfg.agents``, and a scanned project agent never is -- so no
        opaque replacement identifier is needed.
        """
        # Global + owner: verbatim, because it is addressable.
        g = _agent_roster_row(f"crew-{self.PROBE}", "global", KiroCrewAgentConfig(), redact=False)
        assert g["name"] == f"crew-{self.PROBE}"
        assert g["scope"] == "global"
        # Global + app token: masked, because an app cannot reach those routes.
        ga = _agent_roster_row(f"crew-{self.PROBE}", "global", KiroCrewAgentConfig(), redact=True)
        assert _carries_mask(ga["name"])
        # Project: masked for BOTH callers, because nothing addresses a project row.
        for redact in (False, True):
            p = _agent_roster_row(
                f"crew-{self.PROBE}", "project", KiroCrewAgentConfig(), redact=redact
            )
            assert _carries_mask(p["name"]), f"project name unmasked at redact={redact}"
            assert p["scope"] == "project"
        # An ordinary name is byte-identical everywhere, so normal rosters and
        # normal project agents stay selectable.
        for scope in ("global", "project"):
            for redact in (False, True):
                row = _agent_roster_row("my-agent", scope, KiroCrewAgentConfig(), redact=redact)
                assert row["name"] == "my-agent"

    @pytest.mark.parametrize("redact", [False, True])
    def test_benign_content_is_never_altered(self, redact: bool) -> None:
        """A mask that swallows legitimate text is a rendering bug, not hardening."""
        cfg = KiroCrewAgentConfig(description="a plain crew", triggers="use for triage")
        row = _agent_roster_row("kirocrew", "global", cfg, redact=redact)
        assert row["description"] == "a plain crew"
        assert row["triggers"] == "use for triage"
        assert row["name"] == "kirocrew"

    def test_both_caller_classes_ship_the_same_key_set(self) -> None:
        """Only values differ. A caller-dependent KEY set would be a second contract."""
        cfg = KiroCrewAgentConfig(description="d", triggers="t")
        owner = _agent_roster_row("probe", "global", cfg, redact=False)
        app = _agent_roster_row("probe", "global", cfg, redact=True)
        assert set(owner) == set(app) == ROSTER_ROW_KEYS


class TestAvatarIsShapeAllowlistedNotMasked:
    """The one structured field: masked where it can carry text, intact elsewhere.

    Both directions are asserted on purpose. A test that only checks masking
    passes just as well against a blanket mask -- and a blanket here would break
    the feature: a masked ``file`` makes the per-crew avatar endpoint resolve
    nothing, and a masked ``kind`` loses ghost-vs-image.
    """

    PROBE = "AKIAIOSFODNN7EXAMPLE"
    FILE_PIN = "0123456789abcdef.png"

    def test_a_credential_shaped_trait_value_is_masked(self) -> None:
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {"kind": "ghost", "traits": {"eyes": self.PROBE}},
                    }
                ),
            ),
            redact=False,
        )
        avatar = cast(dict, row["avatar"])
        assert _carries_mask(avatar["traits"]["eyes"]), "a user-authored trait was not masked"
        assert self.PROBE not in json.dumps(row)

    def test_a_credential_shaped_expression_value_is_masked(self) -> None:
        """The per-state axes carry user text too, so they mask like traits.

        ``expressions`` is legal on every tier and its ``eyes``/``mouth`` values
        are free strings (32-char truncation is the only pin), so they are the
        one reaction leaf that can carry what a trait can, and they go through
        ``_roster_mask`` the same way. A pack record carries the same key, so the
        mask is checked on both tiers.
        """
        for record in (
            {"kind": "ghost", "expressions": {"working": {"eyes": self.PROBE}}},
            {"kind": "pack", "id": "aurora", "expressions": {"error": {"mouth": self.PROBE}}},
        ):
            row = _agent_roster_row(
                "probe",
                "global",
                cast(
                    KiroCrewAgentConfig,
                    types.SimpleNamespace(
                        **{
                            **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                            "avatar": record,
                        }
                    ),
                ),
                redact=False,
            )
            avatar = cast(dict, row["avatar"])
            state = next(iter(record["expressions"]))
            axis = next(iter(record["expressions"][state]))
            assert _carries_mask(avatar["expressions"][state][axis]), record["kind"]
            assert self.PROBE not in json.dumps(row), record["kind"]

    def test_the_pinned_reaction_names_survive_intact(self) -> None:
        """The direction that rots. A reaction NAMES a shipped animation or preset.

        ``_safe_motions`` and ``_safe_sounds`` pin both to a closed vocabulary, so
        neither is user-authored text: masking one would break the reaction and
        buy nothing, the same reason the regex-pinned ``file`` is left alone. A
        credential-shaped value cannot survive validation to reach the roster at
        all -- it is dropped, which is stronger than masking it.

        The two keys differ in WHERE they are legal, not in how they are handled:
        ``motions`` is ghost-only, ``sounds`` is legal on every tier.
        """
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {
                            "kind": "ghost",
                            "motions": {"done": "bounce", "error": self.PROBE},
                            "sounds": {"working": "chime"},
                        },
                    }
                ),
            ),
            redact=False,
        )
        avatar = cast(dict, row["avatar"])
        assert avatar["motions"] == {"done": "bounce"}
        assert avatar["sounds"] == {"working": "chime"}
        assert self.PROBE not in json.dumps(row)

    def test_the_pinned_file_and_kind_survive_intact(self) -> None:
        """The direction that rots. `file` is regex-pinned, so it needs no mask.

        A value constrained by ``_AVATAR_FILE_PIN_RE`` is safer than a masked one:
        the pin refuses a bad value, where masking destroys a good one.
        """
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {"kind": "image", "v": 17, "file": self.FILE_PIN},
                    }
                ),
            ),
            redact=False,
        )
        avatar = cast(dict, row["avatar"])
        assert avatar["kind"] == "image", "the dashboard could not tell ghost from image"
        assert avatar["file"] == self.FILE_PIN, "the avatar endpoint would resolve nothing"
        assert avatar["v"] == 17
        assert not _carries_mask(avatar["file"])

    def test_the_pin_that_makes_targeted_masking_safe_still_holds(self) -> None:
        """The PRECONDITION, not the consequence -- and this one can redden.

        No test can separate "mask only `traits`" from "mask every leaf": while
        `_safe_avatar` pins each non-`traits` leaf to a shape the redactors do not
        alter, the two behave identically, so a mutation masking everything leaves
        the suite green. The targeted rule is therefore documentation.

        What actually protects `file` is the PIN. So assert the pin. This fails the
        day someone loosens the validator -- which is exactly the day the targeted
        masking stops being documentation and starts being load-bearing, and the
        day `_roster_avatar`'s docstring claim ("a blanket would behave the same")
        stops being true.

        Measured shapes, not assumed: a `file` failing the pin is DROPPED while
        `kind` is kept; a junk or missing `kind` collapses the whole override; a
        non-hex `tile` collapses it too, which is what keeps `javascript:` out of
        the SVG markup `tile` is interpolated into.
        """
        good = {"kind": "image", "v": 17, "file": self.FILE_PIN}
        assert _safe_avatar(good)["file"] == self.FILE_PIN

        for bad_file in ("notadigest.png", "../../etc/passwd", "0123456789abcdef.svg", "0123.png"):
            got = _safe_avatar({"kind": "image", "file": bad_file})
            assert "file" not in got, f"the pin let {bad_file!r} through"
            assert got.get("kind") == "image"

        for bad_kind in (
            {"kind": "nonsense", "traits": {"eyes": "wide"}},
            {"traits": {"eyes": "wide"}},
        ):
            assert _safe_avatar(bad_kind) == {}, "kind is no longer held to its literal set"

        assert _safe_avatar({"kind": "ghost", "traits": {"tile": "javascript:alert(1)"}}) == {}
        assert (
            _safe_avatar({"kind": "ghost", "traits": {"tile": "#a1b2c3"}})["traits"]["tile"]
            == "#a1b2c3"
        )

        # And the roster inherits the pin rather than re-implementing it.
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {"kind": "image", "file": "../../etc/passwd"},
                    }
                ),
            ),
            redact=False,
        )
        assert "file" not in cast(dict, row["avatar"])

    def test_junk_collapses_rather_than_shipping_verbatim(self) -> None:
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {"kind": "nonsense", "evil": self.PROBE},
                    }
                ),
            ),
            redact=False,
        )
        assert self.PROBE not in json.dumps(row)
        assert "evil" not in cast(dict, row["avatar"])


class TestCredentialShapedNamesAreRefusedAtCreation:
    """The hazard is closed where the name comes to exist, not where it is read.

    GPT 5.6 asked for `_roster_mask(name)` on every caller, owner included. That
    masks the field the owner needs to SELECT, RENAME and tell crews apart, and it
    closes one read site out of N -- a stored credential-shaped name still reaches
    logs, error messages and telemetry. Refusing it at creation closes the source.
    """

    PROBE = "AKIAIOSFODNN7EXAMPLE"

    def test_the_create_rule_is_keyed_to_the_read_rule(self) -> None:
        """One detector, so the two halves cannot drift apart.

        Anything the roster would mask is refused at creation; anything it ships
        verbatim is accepted. That equivalence is the invariant, not two lists.
        """
        for candidate in (self.PROBE, f"https://x.example/?token={self.PROBE}"):
            assert _name_would_be_masked(candidate) is True
            assert _roster_mask(candidate) != candidate
        for benign in ("oncall", "kirocrew", "crew-7", "release manager"):
            assert _name_would_be_masked(benign) is False
            assert _roster_mask(benign) == benign

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad", ["Issue Radar", "-radar", "radar-", "Радар", "ra/dar", "a" * 65]
    )
    async def test_creation_refuses_a_name_the_roster_would_skip(
        self, tmp_path: Path, bad: str
    ) -> None:
        """``GET /api/members`` drops any row failing ``_AGENT_NAME_RE``; a name that
        fails it must never be persisted, or the crew exists and no roster can
        show or open it. Refused at the source, for every client of this route."""
        seed = _seed_config_with_every_field_set()
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

            @web.middleware
            async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
                request["app"] = ""
                request["user"] = "owner-1"
                return await handler(request)

            app = web.Application(middlewares=[_owner])
            app["state"] = types.SimpleNamespace(owner_id="owner-1", conversation_log=None)
            app.router.add_post("/api/agents", api_kirocrew_agents_create)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/agents", json={"name": bad, "kiro_agent": "kirocrew"}
                )
                assert resp.status == 400, await resp.text()
                payload = await resp.json()
                assert payload["code"] == "invalid_agent_name"
                assert bad not in json.loads(tmp.read_text(encoding="utf-8")).get("agents", {})

    @pytest.mark.asyncio
    async def test_creation_refuses_a_credential_shaped_name(self, tmp_path: Path) -> None:
        seed = _seed_config_with_every_field_set()
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

            # POST is owner-gated (`_require_owner`), so the caller must BE the
            # owner for the request to reach the name check at all.
            @web.middleware
            async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
                request["app"] = ""
                request["user"] = "owner-1"
                return await handler(request)

            app = web.Application(middlewares=[_owner])
            app["state"] = types.SimpleNamespace(owner_id="owner-1", conversation_log=None)
            app.router.add_post("/api/agents", api_kirocrew_agents_create)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/agents",
                    json={"name": self.PROBE, "kiro_agent": "kirocrew"},
                )
                assert resp.status == 400, await resp.text()
                payload = await resp.json()
                assert payload["code"] == "credential_shaped_name"
                # The refusal must not reflect the value into the response or
                # the request log -- that is the disclosure being prevented.
                assert self.PROBE not in json.dumps(payload)
        # And nothing was stored under it.
        assert self.PROBE not in json.loads(tmp.read_text()).get("agents", {})


class TestCallerClassIsTheOwnerPredicate:
    """Who counts as the owner is delegated, not hand-rolled per caller class."""

    PROBE = "AKIAIOSFODNN7EXAMPLE"

    def _app(self, *, user: str, owner_id: str) -> web.Application:
        """An app whose requests carry a dashboard identity and an owner_id.

        `is_owner_dashboard_request` requires `app == ""` (a dashboard token, not
        an app token), a non-empty `user`, and a match against `state.owner_id`.
        """

        @web.middleware
        async def _identity(request: web.Request, handler):  # type: ignore[no-untyped-def]
            request["app"] = ""
            request["user"] = user
            return await handler(request)

        from kiro_crew.dashboard.handlers import api_kirocrew_agents

        app = web.Application(middlewares=[_identity])
        app["state"] = types.SimpleNamespace(owner_id=owner_id, conversation_log=None)
        app.router.add_get("/api/agents", api_kirocrew_agents)
        return app

    async def _name_for(self, app: web.Application, tmp: Path) -> str:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/agents")
                assert resp.status == 200
                rows = (await resp.json())["agents"]
        return str(rows[0]["name"])

    def _seed(self, tmp_path: Path) -> Path:
        seed = _seed_config_with_every_field_set()
        seed["agents"] = {f"crew-{self.PROBE}": seed["agents"]["roster-probe"]}
        seed["default_agent"] = f"crew-{self.PROBE}"
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        return tmp

    @pytest.mark.asyncio
    async def test_a_non_owner_dashboard_session_gets_the_name_masked(self, tmp_path: Path) -> None:
        """The hole an app-token-only check leaves open.

        `request.get("app", "")` asks "is this an app?", and a non-owner DASHBOARD
        session answers no -- an allow-listed messaging user running `!dashboard`
        holds a dashboard token with `app == ""`. That caller is not the trust
        root, so it must not receive a credential-shaped crew name raw.
        """
        tmp = self._seed(tmp_path)
        name = await self._name_for(self._app(user="someone-else", owner_id="owner-1"), tmp)
        assert _carries_mask(name), "a non-owner dashboard session saw the raw name"

    @pytest.mark.asyncio
    async def test_the_owner_still_gets_an_addressable_global_name(self, tmp_path: Path) -> None:
        """The owner keeps the row's only handle, or edit and delete break."""
        tmp = self._seed(tmp_path)
        name = await self._name_for(self._app(user="owner-1", owner_id="owner-1"), tmp)
        assert name == f"crew-{self.PROBE}"

    @pytest.mark.asyncio
    async def test_a_stateless_app_fails_closed(self, tmp_path: Path) -> None:
        """No state means no owner can be resolved, so mask rather than show.

        `is_owner_dashboard_request` subscripts `app["state"]`. For a disclosure
        control, "unknown caller" must mean "mask".
        """
        tmp = self._seed(tmp_path)
        name = await self._name_for(_make_app(), tmp)
        assert _carries_mask(name)

    def test_a_mask_nested_in_a_structured_field_is_refused(self) -> None:
        """The corruption a top-level-only check let through.

        `avatar` is a dict whose `traits` values are masked, so an echoed avatar
        carries the sentinel one level DOWN. A flat check sees a `dict`, answers
        "not a mask", and lets `_safe_avatar` persist the sentinel over the stored
        trait. Any string anywhere inside the value must count.
        """
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        assert _carries_mask({"kind": "ghost", "traits": {"eyes": _SENSITIVE_MASK}}) is True
        assert _carries_mask({"kind": "ghost", "traits": {"eyes": f"{_SENSITIVE_MASK} x"}}) is True
        assert _carries_mask([{"traits": {"eyes": _SENSITIVE_MASK}}]) is True
        # A clean structured value is still a genuine edit.
        assert _carries_mask({"kind": "image", "v": 17, "file": "0123456789abcdef.png"}) is False
        assert _carries_mask({"kind": "ghost", "traits": {"eyes": "wide", "blush": True}}) is False

    @pytest.mark.asyncio
    async def test_an_echoed_avatar_does_not_overwrite_a_masked_trait(self, tmp_path: Path) -> None:
        """End to end: the sentinel never reaches config.json through the nest."""
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        seed = _seed_config_with_every_field_set()
        stored = f"eyes-{self.PROBE}"
        seed["agents"]["roster-probe"]["avatar"] = {"kind": "ghost", "traits": {"eyes": stored}}
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp),
            unittest.mock.patch(
                "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
                lambda request: True,
            ),
        ):
            from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

            app = web.Application()
            app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
            async with TestClient(TestServer(app)) as client:
                put = await client.put(
                    "/api/agents/roster-probe",
                    json={"avatar": {"kind": "ghost", "traits": {"eyes": _SENSITIVE_MASK}}},
                )
                assert put.status == 200, await put.text()
        after = json.loads(tmp.read_text())["agents"]["roster-probe"]["avatar"]
        assert _SENSITIVE_MASK not in json.dumps(after), "the nested sentinel was persisted"
        assert after["traits"]["eyes"] == stored


class TestMaskIsTreatedAsUnchangedOnWrite:
    """The write half, without which the read half destroys stored config."""

    PROBE = "AKIAIOSFODNN7EXAMPLE"

    @pytest.fixture(autouse=True)
    def _as_owner(self, monkeypatch):
        """Run past the owner gate.

        These tests exercise the write-side rule, not the owner boundary -- that
        has its own enumerate-the-invariant coverage in
        `test_agents_endpoints_owner_auth.py`. Same patch `test_config_api.py`
        uses for the mutating agent endpoints.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    def test_the_sentinel_is_the_one_the_config_endpoint_uses(self) -> None:
        """Not a private copy: drift would silently break the round-trip rule."""
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        assert _carries_mask(_SENSITIVE_MASK)
        row = _agent_roster_row(
            "probe", "global", KiroCrewAgentConfig(description=f"see {self.PROBE}"), redact=False
        )
        assert row["description"] == _SENSITIVE_MASK

    def test_real_content_is_not_treated_as_the_mask(self) -> None:
        assert _carries_mask("plain text") is False
        assert _carries_mask("") is False
        assert _carries_mask({"a": 1}) is False

    def test_a_mask_the_operator_appended_to_is_still_refused(self) -> None:
        """The editor renders the mask into a text input, so it can be typed past.

        An exact-match rule closes only the echo-it-back case. Appending to the
        mask produces a string that is not the sentinel, so equality would persist
        the redaction glyphs plus the addition over the stored original -- the data
        loss this rule exists to stop.
        """
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        assert _carries_mask(_SENSITIVE_MASK) is True
        assert _carries_mask(f"{_SENSITIVE_MASK} and also X") is True
        assert _carries_mask(f"prefix {_SENSITIVE_MASK}") is True
        assert _carries_mask(f"a {_SENSITIVE_MASK} b") is True

    @pytest.mark.asyncio
    async def test_appending_to_a_masked_field_does_not_overwrite_it(self, tmp_path: Path) -> None:
        """End to end: glyphs never reach config.json, and the original survives.

        This is GPT 5.6's finding on `a34a51a88`'s successor as an executable test:
        masked trigger -> editor appends text -> PUT must NOT persist the sentinel
        plus the text.
        """
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        seed = _seed_config_with_every_field_set()
        stored = f"use when {self.PROBE}"
        seed["agents"]["roster-probe"]["triggers"] = stored
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

            app = web.Application()
            app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
            async with TestClient(TestServer(app)) as client:
                put = await client.put(
                    "/api/agents/roster-probe",
                    json={"triggers": f"{_SENSITIVE_MASK} and also triage"},
                )
                assert put.status == 200, await put.text()
        after = json.loads(tmp.read_text())["agents"]["roster-probe"]
        assert after["triggers"] == stored, "the appended mask was persisted"
        assert _SENSITIVE_MASK not in after["triggers"]

    @pytest.mark.asyncio
    async def test_end_to_end_read_then_write_preserves_the_stored_value(
        self, tmp_path: Path
    ) -> None:
        """The whole point, over HTTP: GET the roster, PUT the row back.

        This is the round-trip defect as an executable test. The agents page seeds
        its edit sheet from a roster row and `saveEdit` returns every field
        unconditionally, so without the write-side rule the mask would be
        persisted over the operator's stored value on the next save of an
        unrelated field.
        """
        seed = _seed_config_with_every_field_set()
        stored_triggers = f"use when {self.PROBE}"
        seed["agents"]["roster-probe"]["triggers"] = stored_triggers
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            from kiro_crew.dashboard.handlers import (
                api_kirocrew_agent_update,
                api_kirocrew_agents,
            )

            app = web.Application()
            app.router.add_get("/api/agents", api_kirocrew_agents)
            app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/agents")
                rows = {a["name"]: a for a in (await resp.json())["agents"]}
                row = rows["roster-probe"]
                assert _carries_mask(row["triggers"]), "the value should arrive masked"
                # Echo the row back the way `saveEdit` does: every field, always.
                put = await client.put(
                    "/api/agents/roster-probe",
                    json={
                        "kiro_agent": row["kiro_agent"],
                        "workspace": row["workspace"],
                        "memory_store": row["memory_store"],
                        "triggers": row["triggers"],
                        "model": row["model"],
                        "reasoning_effort": row["reasoning_effort"],
                        "session_color": row["session_color"],
                    },
                )
                assert put.status == 200, await put.text()

            stored = json.loads(tmp.read_text())["agents"]["roster-probe"]
            assert stored["triggers"] == stored_triggers, "the mask was persisted"

    @pytest.mark.asyncio
    async def test_a_stale_view_cannot_corrupt_the_config(self, tmp_path: Path) -> None:
        """The failure mode a recomputed-equality rule had and a sentinel does not.

        If the stored value changes between the GET and the PUT (an agent editing
        `config.json`, a second dashboard tab), a rule that recognised the view by
        recomputing the redaction of the CURRENT value would stop matching and
        write the redacted text in as though the operator had typed it. The
        sentinel does not depend on the stored value, so the stale echo is still
        recognised and dropped.
        """
        seed = _seed_config_with_every_field_set()
        seed["agents"]["roster-probe"]["triggers"] = f"first {self.PROBE}"
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            from kiro_crew.dashboard.handlers import (
                api_kirocrew_agent_update,
                api_kirocrew_agents,
            )

            app = web.Application()
            app.router.add_get("/api/agents", api_kirocrew_agents)
            app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
            async with TestClient(TestServer(app)) as client:
                rows = {
                    a["name"]: a for a in (await (await client.get("/api/agents")).json())["agents"]
                }
                stale = rows["roster-probe"]["triggers"]
                # Someone else rewrites the stored value AFTER the read.
                on_disk = json.loads(tmp.read_text())
                on_disk["agents"]["roster-probe"]["triggers"] = f"second {self.PROBE} changed"
                tmp.write_text(json.dumps(on_disk))
                put = await client.put("/api/agents/roster-probe", json={"triggers": stale})
                assert put.status == 200, await put.text()

            stored = json.loads(tmp.read_text())["agents"]["roster-probe"]
            assert stored["triggers"] == f"second {self.PROBE} changed"

    @pytest.mark.asyncio
    async def test_an_echoed_mask_cannot_reject_an_unrelated_edit(self, tmp_path: Path) -> None:
        """The mask filter runs BEFORE the validated fields are validated.

        ``model`` and ``reasoning_effort`` are checked before the config load and
        reject a bad value with 400. If the mask filter ran after them, a client
        echoing a masked ``reasoning_effort`` back would have its whole save
        rejected -- failing an edit to some unrelated field. Dropping masked
        entries first means a mask is never validated as if it were content.
        """
        seed = _seed_config_with_every_field_set()
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            from kiro_crew.dashboard.handlers import api_kirocrew_agent_update
            from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

            app = web.Application()
            app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
            async with TestClient(TestServer(app)) as client:
                put = await client.put(
                    "/api/agents/roster-probe",
                    # The mask in a VALIDATED field, alongside a real edit.
                    json={
                        "reasoning_effort": _SENSITIVE_MASK,
                        "model": _SENSITIVE_MASK,
                        "triggers": "a genuine new value",
                    },
                )
                assert put.status == 200, await put.text()
            stored = json.loads(tmp.read_text())["agents"]["roster-probe"]
            # The real edit landed...
            assert stored["triggers"] == "a genuine new value"
            # ...and the masked fields kept their stored values.
            assert stored["reasoning_effort"] == "high"
            assert stored["model"] == "claude-opus-5"

    @pytest.mark.asyncio
    async def test_a_real_edit_still_writes_through(self, tmp_path: Path) -> None:
        """The rule must not swallow an actual change, or editing is broken."""
        seed = _seed_config_with_every_field_set()
        seed["agents"]["roster-probe"]["triggers"] = f"use when {self.PROBE}"
        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(seed), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

            app = web.Application()
            app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
            async with TestClient(TestServer(app)) as client:
                put = await client.put(
                    "/api/agents/roster-probe", json={"triggers": "an actual new value"}
                )
                assert put.status == 200, await put.text()
            stored = json.loads(tmp.read_text())["agents"]["roster-probe"]
            assert stored["triggers"] == "an actual new value"


class TestResolveRosterProjectPathSubdirSensitivity:
    """The roster path resolver validates only the ROOT; the sensitive-subdir
    refusal is the scan machinery's.

    The value actually scanned is ``<root>/.kiro/agents``, and a symlinked scope
    there is refused AND SEL-audited by
    :func:`agent_discovery._pinned_scan_dir_fd` on the path that runs
    (``project_agent_names``/``project_agent_files``) -- pinned independently by
    ``test_agent_discovery.py`` ::
    ``test_project_agents_dir_symlinked_into_sensitive_tree_is_not_scanned``. So
    this resolver does NOT re-resolve the subdirs: a benign root whose
    ``.kiro/agents`` symlinks into a credential tree resolves here as the benign
    root it is, and the enumeration is refused where it happens.
    """

    def test_symlinked_kiro_agents_subdir_resolves_at_the_root(self, tmp_path, monkeypatch) -> None:
        import os

        from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path

        secret_tree = tmp_path / "creds_home"
        secret_tree.mkdir()
        proj = tmp_path / "repo"
        (proj / ".kiro").mkdir(parents=True)
        agents_subdir = proj / ".kiro" / "agents"
        real_realpath = os.path.realpath
        probes: list[str] = []

        def guarded_realpath(path) -> str:
            probes.append(os.fspath(path))
            if os.fspath(path) == os.fspath(agents_subdir):
                return real_realpath(secret_tree)
            return real_realpath(path)

        monkeypatch.setattr(os.path, "realpath", guarded_realpath)

        # Only the resolved credential tree is sensitive; the repo ROOT is NOT.
        # The resolver checks the root alone, so it clears -- the sensitive
        # ``.kiro/agents`` is the scan machinery's to refuse and audit, not this
        # resolver's.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            lambda p: os.fspath(p) == real_realpath(secret_tree),
        )

        resolved, denied = _resolve_roster_project_path(str(proj))
        assert (resolved, denied) == (real_realpath(str(proj)), False)
        assert os.fspath(agents_subdir) not in probes, "the root resolver probed the linked subdir"

    def test_project_dir_without_a_kiro_dir_still_resolves(self, tmp_path, monkeypatch) -> None:
        """A project dir carrying no ``.kiro`` resolves: absence is not a refusal.

        The resolver validates the ROOT. A directory that simply has no agent
        tree yet is the ordinary case for a folder pointed at a plain
        repository, and it must return the resolved root so the caller ships
        GLOBAL rows -- never ``denied``, which is reserved for a sensitive
        target.
        """
        import os

        from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path

        proj = tmp_path / "plain-repo"
        proj.mkdir()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            lambda p: False,
        )

        resolved, denied = _resolve_roster_project_path(str(proj))
        assert (resolved, denied) == (os.path.realpath(str(proj)), False)

    def test_benign_project_dir_still_resolves(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path

        proj = tmp_path / "repo"
        (proj / ".kiro" / "agents").mkdir(parents=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            lambda p: False,
        )
        resolved, denied = _resolve_roster_project_path(str(proj))
        import os

        assert denied is False
        assert resolved == os.path.realpath(str(proj))


class TestResolveRosterProjectPathBasics:
    """``_resolve_roster_project_path`` validates only the ROOT: expand ``~``,
    realpath, ``is_sensitive_path``, ``isdir``. Pinning the directory chain and
    refusing an unverifiable scan now both happen inside the scan machinery
    itself (:mod:`kiro_crew.pinned_fs`, reached through
    ``project_agent_names``), so this resolver is a plain, platform-independent
    validation with nothing left to hold or release.
    """

    def test_disallowed_raw_unc_is_denied_before_realpath(self, monkeypatch) -> None:
        """A caller-chosen UNC host must not reach the resolving SMB probe."""
        import kiro_crew.agent_discovery as discovery_mod
        import kiro_crew.dashboard.handlers.agents as agents_mod

        raw = r"\\attacker\share\repo"
        realpath_probes: list[str] = []
        monkeypatch.setattr(discovery_mod, "_WINDOWS", True)
        monkeypatch.setattr(
            discovery_mod,
            "is_unc_shape",
            lambda spelling: str(spelling).startswith(("\\\\", "//")),
        )
        monkeypatch.setattr(discovery_mod, "unc_probe_allowed", lambda _spelling: False)
        monkeypatch.setattr(agents_mod.os.path, "expanduser", lambda spelling: spelling)
        monkeypatch.setattr(
            agents_mod.os.path,
            "realpath",
            lambda spelling: realpath_probes.append(spelling) or "/unexpected/probe",
        )
        monkeypatch.setattr(agents_mod, "is_sensitive_path", lambda _spelling: False)
        monkeypatch.setattr(agents_mod.os.path, "isdir", lambda _spelling: False)

        result = agents_mod._resolve_roster_project_path(raw)
        monkeypatch.undo()

        assert realpath_probes == [], f"realpath probed a disallowed UNC host: {realpath_probes}"
        assert result == ("", True)

    def test_mocked_windows_linked_ancestor_is_denied_before_realpath(self, monkeypatch) -> None:
        """Mocked-platform test: a local-looking path with a Windows junction
        ancestor targeting an SMB share is denied before ``realpath`` follows it.
        """
        import kiro_crew.agent_discovery as discovery_mod
        import kiro_crew.dashboard.handlers.agents as agents_mod

        raw = r"C:\work\junction\repo"
        linked_ancestor = r"C:\work\junction"
        realpath_probes: list[str] = []
        discovery_os = types.SimpleNamespace(**vars(discovery_mod.os))
        discovery_os.readlink = lambda path: (
            r"\\attacker\share" if path == linked_ancestor else discovery_mod.os.readlink(path)
        )
        monkeypatch.setattr(discovery_mod, "os", discovery_os)
        monkeypatch.setattr(discovery_mod, "_WINDOWS", True)
        monkeypatch.setattr(
            discovery_mod, "iter_linked_ancestors", lambda _path: iter([linked_ancestor])
        )
        agents_path = types.SimpleNamespace(**vars(agents_mod.os.path))
        agents_path.expanduser = lambda spelling: spelling
        agents_path.realpath = lambda spelling: realpath_probes.append(spelling) or spelling
        agents_os = types.SimpleNamespace(**vars(agents_mod.os))
        agents_os.path = agents_path
        monkeypatch.setattr(agents_mod, "os", agents_os)

        result = agents_mod._resolve_roster_project_path(raw)

        assert realpath_probes == []
        assert result == ("", True)

    def test_disallowed_resolved_unc_is_denied_before_stat(self, monkeypatch) -> None:
        """A local spelling resolving to an untrusted share stops before stat."""
        import kiro_crew.agent_discovery as discovery_mod
        import kiro_crew.dashboard.handlers.agents as agents_mod

        raw = r"C:\work\repo"
        resolved_unc = r"\\attacker\share\repo"
        sensitivity_probes: list[str] = []
        stat_probes: list[str] = []
        monkeypatch.setattr(discovery_mod, "_WINDOWS", True)
        monkeypatch.setattr(
            discovery_mod,
            "is_unc_shape",
            lambda spelling: str(spelling).startswith(("\\\\", "//")),
        )
        monkeypatch.setattr(discovery_mod, "unc_probe_allowed", lambda _spelling: False)
        monkeypatch.setattr(agents_mod.os.path, "expanduser", lambda spelling: spelling)
        monkeypatch.setattr(agents_mod.os.path, "realpath", lambda _spelling: resolved_unc)
        monkeypatch.setattr(
            agents_mod,
            "is_sensitive_path",
            lambda spelling: sensitivity_probes.append(spelling) or False,
        )
        monkeypatch.setattr(
            agents_mod.os.path,
            "isdir",
            lambda spelling: stat_probes.append(spelling) or False,
        )

        result = agents_mod._resolve_roster_project_path(raw)
        monkeypatch.undo()

        assert (
            sensitivity_probes == []
        ), f"sensitivity probe reached a disallowed UNC host: {sensitivity_probes}"
        assert stat_probes == [], f"stat reached a disallowed UNC host: {stat_probes}"
        assert result == ("", True)

    def test_benign_project_dir_resolves(self, tmp_path, monkeypatch) -> None:
        import os

        from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path

        proj = tmp_path / "repo"
        (proj / ".kiro" / "agents").mkdir(parents=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            lambda p: False,
        )
        resolved, denied = _resolve_roster_project_path(str(proj))
        assert denied is False
        assert resolved == os.path.realpath(str(proj))

    def test_sensitive_project_dir_is_denied(self, tmp_path, monkeypatch) -> None:
        import os

        from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path

        secret = tmp_path / "secret"
        secret.mkdir()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            lambda p: os.path.realpath(str(p)) == os.path.realpath(str(secret)),
        )
        resolved, denied = _resolve_roster_project_path(str(secret))
        assert (resolved, denied) == ("", True)

    def test_missing_project_dir_resolves_as_absent_not_denied(self, tmp_path, monkeypatch) -> None:
        """A directory that simply does not exist yet is the ordinary case for
        a folder pointed at a fresh path, not a security event."""
        from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            lambda p: False,
        )
        resolved, denied = _resolve_roster_project_path(str(tmp_path / "does-not-exist"))
        assert (resolved, denied) == ("", False)

    def test_tilde_is_expanded_before_resolving(self, tmp_path, monkeypatch) -> None:
        import os

        from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path

        home = tmp_path / "home"
        (home / "repo").mkdir(parents=True)
        monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(home), 1))
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            lambda p: False,
        )
        resolved, denied = _resolve_roster_project_path("~/repo")
        assert denied is False
        assert resolved == os.path.realpath(str(home / "repo"))

    def test_symlinked_kiro_agents_subdir_resolves_at_the_root(self, tmp_path, monkeypatch) -> None:
        """The resolver validates the ROOT alone; a symlinked ``.kiro/agents``
        subdir into a sensitive tree is refused by the SCAN machinery, not
        re-validated here (see
        ``test_agent_discovery.py::test_a_sensitive_resolved_target_is_refused``).
        """
        import os

        from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path

        secret_tree = tmp_path / "creds_home"
        secret_tree.mkdir()
        proj = tmp_path / "repo"
        (proj / ".kiro").mkdir(parents=True)
        agents_subdir = proj / ".kiro" / "agents"
        real_realpath = os.path.realpath
        probes: list[str] = []

        def guarded_realpath(path) -> str:
            probes.append(os.fspath(path))
            if os.fspath(path) == os.fspath(agents_subdir):
                return real_realpath(secret_tree)
            return real_realpath(path)

        monkeypatch.setattr(os.path, "realpath", guarded_realpath)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            lambda p: os.fspath(p) == real_realpath(secret_tree),
        )

        resolved, denied = _resolve_roster_project_path(str(proj))
        assert (resolved, denied) == (real_realpath(str(proj)), False)
        assert os.fspath(agents_subdir) not in probes, "the root resolver probed the linked subdir"


class TestApiKirocrewAgentsUnverifiableScan:
    """A scan the platform cannot pin, or that hits a check-to-use swap the
    pin refuses (:class:`kiro_crew.agent_discovery.ScanUnverifiable` /
    :class:`kiro_crew.pinned_fs.PinnedPathRefusal`), must surface as 503 --
    never as a 200 with an empty roster indistinguishable from a project that
    genuinely declares no agents.
    """

    @pytest.mark.asyncio
    async def test_owner_supplied_path_scan_unverifiable_is_503(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from kiro_crew.agent_discovery import ScanUnverifiable

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents._resolve_roster_project_path",
            lambda raw: (raw, False),
        )

        def _boom(*_a, **_kw):
            raise ScanUnverifiable("cannot pin a directory descriptor on this platform")

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents.project_agent_names", _boom)

        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/some/project")
                    # 503, not 200: an unverifiable scan must never read as "this
                    # project declares no agents" -- see the module docstring.
                    assert resp.status == 503
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_owner_supplied_path_pinned_path_refusal_is_503(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The same 503 for the OTHER refusal shape: a check-to-use swap the
        pin itself catches (an ancestor became a link between the resolve and
        the open), surfaced through :mod:`kiro_crew.pinned_fs`'s own exception.
        """
        from kiro_crew.pinned_fs import PinnedPathRefusal

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents._resolve_roster_project_path",
            lambda raw: (raw, False),
        )

        def _boom(*_a, **_kw):
            raise PinnedPathRefusal("an ancestor was swapped for a link")

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents.project_agent_names", _boom)

        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/some/project")
                    assert resp.status == 503
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_owner_scan_on_an_unpinnable_platform_degrades_not_503(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """On a platform that can NEVER pin a directory descriptor
        (``supports_pinned_walk()`` is False -- Windows has no
        ``O_DIRECTORY``/``O_NOFOLLOW``/``dir_fd``), the owner scan must NOT ask
        for the unverifiable signal. ``raise_unverifiable=True`` there makes the
        chokepoint raise :class:`ScanUnverifiable` on EVERY request, so every
        folder with a project dir answers 503 -- a permanent ``rosterScanError``
        banner whose Retry can never succeed, and ``unvalidatableAgent`` blocking
        Save for any project pick. It must degrade to the empty-roster default,
        exactly as the slot-derived fallback always has.
        """
        captured: dict[str, object] = {}

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents._resolve_roster_project_path",
            lambda raw: (raw, False),
        )
        # Exactly Windows' capability verdict: the platform can never pin.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.supports_pinned_walk",
            lambda: False,
        )

        def _names(project_dir, **kw):
            # Faithful chokepoint behaviour: it raises ONLY when asked to
            # (``raise_unverifiable=True``) and cannot pin; otherwise it degrades
            # to a (possibly empty) name set. So a handler that still passes True
            # here on an unpinnable platform would 503 forever.
            captured["raise_unverifiable"] = kw.get("raise_unverifiable")
            if kw.get("raise_unverifiable"):
                from kiro_crew.agent_discovery import ScanUnverifiable

                raise ScanUnverifiable("cannot pin a directory descriptor on this platform")
            return frozenset({"project-only-agent"})

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            _names,
        )

        tmp = tmp_path / "config.json"
        tmp.write_text(json.dumps(_seed_config_with_every_field_set()))
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents?project_path=/some/project")
                    # 200, not 503: an unpinnable platform degrades rather than
                    # refusing on every request.
                    assert resp.status == 200
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}
        finally:
            tmp.unlink(missing_ok=True)

        # The whole fix: the handler asked the chokepoint NOT to raise, because a
        # platform that can never pin has nothing to gain from a signal that
        # would fire unconditionally.
        assert captured.get("raise_unverifiable") is False
        # The degraded scan's rows still ship.
        assert "project-only-agent" in rows
