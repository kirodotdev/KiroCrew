"""Roster serializers redact record strings before dashboard JSON (#8447).

Free-text string fields on the crew/agent record (``description``,
``triggers``, ``workspace``, ...) are agent-writable through ``config.json``
and were echoed verbatim by ``GET /api/agents`` (a full dataclass spread) and
``GET /api/members`` (an explicit allowlist with no redaction pass over the
record values). Both now funnel each serialized record through
``_shared.redact_record_strings`` — a field-generic chokepoint delegating to
``_redact_memory_field``, the shared recursive scrubber — so a credential or
exfiltration URL planted in ANY record string reaches the browser redacted,
and fields added to the record later (including nested structures such as
avatar trait axes) are covered with no second patch.

The two planted shapes are chosen so each kills one half of the chain in
isolation: a bare AWS key id is invisible to ``redact_exfiltration_urls``
(it only rewrites URLs), and a long-query URL with no credential marker is
invisible to ``redact_credentials`` — so a fix that wires only one half fails
the other half's test.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig

#: Canonical AWS example access-key id — caught ONLY by ``redact_credentials``.
CRED = "AKIAIOSFODNN7EXAMPLE"
#: Long-query URL with no credential marker — caught ONLY by
#: ``redact_exfiltration_urls`` (query-length heuristic; the all-lowercase run
#: fails the bare-secret pass's mixed-case gate).
EXFIL = "https://exfil.example.com/?d=" + "a" * 210

CRED_MARKER = "[REDACTED: credential]"
EXFIL_MARKER = "[REDACTED: suspicious URL to exfil.example.com]"


def _fake_config(agents: dict, default: str = "alpha"):
    return SimpleNamespace(agents=agents, default_agent=default)


# ── GET /api/agents ──


def _make_agents_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agents

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


async def _get_agents(state, agents: dict) -> tuple[dict, str]:
    with patch(
        "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
        return_value=_fake_config(agents),
    ):
        async with TestClient(TestServer(_make_agents_app(state))) as client:
            resp = await client.get("/api/agents")
            assert resp.status == 200
            raw = await resp.text()
            data = await resp.json()
    return data, raw


class TestAgentsEndpointRedaction:
    @pytest.mark.asyncio
    async def test_credential_in_description_is_redacted(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = {"alpha": KiroCrewAgentConfig(kiro_agent="alpha", description=CRED)}

        data, raw = await _get_agents(state, cfg)

        assert CRED not in raw
        rows = {r["name"]: r for r in data["agents"]}
        assert rows["alpha"]["description"] == CRED_MARKER

    @pytest.mark.asyncio
    async def test_exfiltration_url_in_workspace_is_redacted(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = {"alpha": KiroCrewAgentConfig(kiro_agent="alpha", workspace=EXFIL)}

        data, raw = await _get_agents(state, cfg)

        assert EXFIL not in raw
        rows = {r["name"]: r for r in data["agents"]}
        assert rows["alpha"]["workspace"] == EXFIL_MARKER

    @pytest.mark.asyncio
    async def test_default_agent_is_redacted_too(self, tmp_path):
        """`default_agent` is the same class of agent-writable config string
        as the row fields — a credential-shaped default alias must not ship
        raw beside redacted rows (GPT review finding on #8465, round 5)."""
        state = _make_state(tmp_path)
        cfg = {"alpha": KiroCrewAgentConfig(kiro_agent="alpha")}

        with patch(
            "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
            return_value=_fake_config(cfg, default=CRED),
        ):
            async with TestClient(TestServer(_make_agents_app(state))) as client:
                resp = await client.get("/api/agents")
                assert resp.status == 200
                raw = await resp.text()
                data = await resp.json()

        assert CRED not in raw
        assert data["default_agent"] == CRED_MARKER

    @pytest.mark.asyncio
    async def test_project_scope_rows_go_through_the_chokepoint(self, tmp_path):
        """The project-scope row source must be covered too, not just cfg.agents.

        Project rows spread ``dataclasses.asdict(KiroCrewAgentConfig())`` — the
        record's DEFAULTS. Today those defaults are benign constants, so the
        poisoned-default stand-in below is what makes the row source observable:
        a chokepoint that wraps only the ``cfg.agents`` rows leaves this row
        verbatim and the test goes red.
        """

        @dataclasses.dataclass
        class _PoisonedDefaults(KiroCrewAgentConfig):
            description: str = CRED
            workspace: str = EXFIL

        state = _make_state(tmp_path)
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
                return_value=_fake_config({"alpha": KiroCrewAgentConfig(kiro_agent="alpha")}),
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.active_project_dir",
                return_value="/tmp/some-project",
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.project_agent_names",
                return_value=frozenset({"proj-agent"}),
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.KiroCrewAgentConfig",
                _PoisonedDefaults,
            ),
        ):
            async with TestClient(TestServer(_make_agents_app(state))) as client:
                resp = await client.get("/api/agents")
                assert resp.status == 200
                raw = await resp.text()
                data = await resp.json()

        assert CRED not in raw
        assert EXFIL not in raw
        rows = {r["name"]: r for r in data["agents"]}
        assert rows["proj-agent"]["scope"] == "project"
        assert rows["proj-agent"]["description"] == CRED_MARKER
        assert rows["proj-agent"]["workspace"] == EXFIL_MARKER


# ── GET /api/members ──


def _make_members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


async def _get_members(state, agents: dict) -> tuple[dict, str]:
    with patch(
        "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
        return_value=_fake_config(agents, default="code-reviewer"),
    ):
        async with TestClient(TestServer(_make_members_app(state))) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200
            raw = await resp.text()
            data = await resp.json()
    return data, raw


class TestMembersEndpointRedaction:
    @pytest.mark.asyncio
    async def test_credential_in_workspace_is_redacted(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = {"code-reviewer": KiroCrewAgentConfig(kiro_agent="code-reviewer", workspace=CRED)}

        data, raw = await _get_members(state, cfg)

        assert CRED not in raw
        rows = {r["name"]: r for r in data["members"]}
        assert rows["code-reviewer"]["workspace"] == CRED_MARKER

    @pytest.mark.asyncio
    async def test_exfiltration_url_in_memory_store_is_redacted(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = {"code-reviewer": KiroCrewAgentConfig(kiro_agent="code-reviewer", memory_store=EXFIL)}

        data, raw = await _get_members(state, cfg)

        assert EXFIL not in raw
        rows = {r["name"]: r for r in data["members"]}
        assert rows["code-reviewer"]["memory_store"] == EXFIL_MARKER

    @pytest.mark.asyncio
    async def test_allowlist_contract_is_preserved(self, tmp_path):
        """Redaction is a pass over the allowlisted VALUES, never a spread.

        The explicit allowlist is a deliberate network-boundary contract
        (its own comment argues against a spread); the fix must not have
        converted it into one.
        """
        state = _make_state(tmp_path)
        cfg = {
            "code-reviewer": KiroCrewAgentConfig(
                kiro_agent="code-reviewer", description=CRED, triggers=CRED
            )
        }

        data, _raw = await _get_members(state, cfg)

        rows = {r["name"]: r for r in data["members"]}
        # Non-allowlisted record fields still do not ship at all.
        assert "description" not in rows["code-reviewer"]
        assert "triggers" not in rows["code-reviewer"]


# ── The chokepoint helper itself ──


class TestRedactRecordStrings:
    def test_nested_values_are_redacted(self):
        """The recursion is what keeps the chokepoint covering the record once
        a later change nests structures under a record key (e.g. avatar trait
        axes) — dropping it silently uncovers those fields."""
        from kiro_crew.dashboard.handlers._shared import redact_record_strings

        out = redact_record_strings({"avatar": {"traits": [CRED, "benign"]}, "notes": [EXFIL]})

        assert out["avatar"]["traits"][0] == CRED_MARKER
        assert out["avatar"]["traits"][1] == "benign"
        assert out["notes"][0] == EXFIL_MARKER

    def test_nested_dict_keys_are_redacted(self):
        """Dict KEYS serialize into JSON exactly like values, so a
        credential-shaped key inside an agent-writable object-valued field
        would ship verbatim if only values were walked (GPT review finding on
        #8465). Keys of nested dicts go through the same chain; benign keys
        pass unchanged. A key collision from over-redaction (two secret keys
        folding onto one marker) is the fail-safe direction at this boundary."""
        from kiro_crew.dashboard.handlers._shared import redact_record_strings

        out = redact_record_strings({"avatar": {CRED: "x", "shade": EXFIL}})

        assert out["avatar"] == {CRED_MARKER: "x", "shade": EXFIL_MARKER}

    def test_memory_field_scrubber_keys_unchanged_by_default(self):
        """The shared scrubber's other callers (memory.py, cron.py) keep their
        exact pre-#8465 behavior: keys pass through unless the roster
        chokepoint's opt-in is set."""
        from kiro_crew.dashboard.handlers._shared import _redact_memory_field

        out = _redact_memory_field({CRED: "x"})
        assert out == {CRED: "x"}


# ── PUT /api/agents/{name}: redacted-echo write-back guard ──


def _make_crud_app(tmp_path) -> web.Application:
    from dashboard_owner_helpers import as_owner

    from kiro_crew.dashboard.handlers.agents import (
        api_kirocrew_agent_update,
        api_kirocrew_agents,
    )

    app = web.Application()
    app["state"] = _make_state(tmp_path)
    app.router.add_get("/api/agents", api_kirocrew_agents)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return as_owner(app)


def _seed_config_file(tmp_path, agent_fields: dict):
    import json

    seed = {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
            "test-agent": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
                **agent_fields,
            },
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}},
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    return cfg_file


class TestUpdateRedactedEchoGuard:
    """A roster-redacted value must not round-trip back INTO the config.

    ``GET /api/agents`` now serves record strings redacted (#8447), and the
    crew edit sheet prefills its form from that roster and PUTs the prefill
    fields back unconditionally on save. Without a guard, saving an edit that
    never touched a redacted field overwrites the real ``config.json`` value
    with the ``[REDACTED: ...]`` marker — silent data loss (GPT review
    finding on #8465, round 2).
    """

    @pytest.mark.asyncio
    async def test_redacted_echo_does_not_overwrite_config(self, tmp_path):
        import json
        from unittest.mock import patch as upatch

        cfg_file = _seed_config_file(tmp_path, {"triggers": CRED})
        with upatch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_make_crud_app(tmp_path))) as client:
                # What the edit form was prefilled with: the roster's view.
                resp = await client.get("/api/agents")
                row = {a["name"]: a for a in (await resp.json())["agents"]}["test-agent"]
                assert row["triggers"] == CRED_MARKER  # precondition: it redacts

                # The form saves, echoing the redacted prefill back.
                resp = await client.put(
                    "/api/agents/test-agent", json={"triggers": row["triggers"]}
                )
                assert resp.status == 200

        stored = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert stored["agents"]["test-agent"]["triggers"] == CRED

    @pytest.mark.asyncio
    async def test_deliberate_new_value_is_still_written(self, tmp_path):
        import json
        from unittest.mock import patch as upatch

        cfg_file = _seed_config_file(tmp_path, {"triggers": CRED})
        with upatch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_make_crud_app(tmp_path))) as client:
                resp = await client.put("/api/agents/test-agent", json={"triggers": "sev1, sev2"})
                assert resp.status == 200

        stored = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert stored["agents"]["test-agent"]["triggers"] == "sev1, sev2"

    @pytest.mark.asyncio
    async def test_marker_text_over_benign_value_is_written(self, tmp_path):
        """The guard drops ONLY the exact echo of the current stored value.

        A user pasting text that merely contains the marker over a BENIGN
        stored value is a deliberate edit — dropping any marker-bearing input
        wholesale would make such fields silently uneditable."""
        import json
        from unittest.mock import patch as upatch

        cfg_file = _seed_config_file(tmp_path, {"description": "benign"})
        pasted = "note: was [REDACTED: credential] upstream"
        with upatch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_make_crud_app(tmp_path))) as client:
                resp = await client.put("/api/agents/test-agent", json={"description": pasted})
                assert resp.status == 200

        stored = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert stored["agents"]["test-agent"]["description"] == pasted

    @pytest.mark.asyncio
    async def test_mixed_save_writes_edits_and_preserves_echoes(self, tmp_path):
        """The crew sheet PUTs every form field on save — the edited field
        must land while the untouched redacted prefill is preserved."""
        import json
        from unittest.mock import patch as upatch

        cfg_file = _seed_config_file(tmp_path, {"workspace": CRED})
        with upatch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_make_crud_app(tmp_path))) as client:
                resp = await client.get("/api/agents")
                row = {a["name"]: a for a in (await resp.json())["agents"]}["test-agent"]
                resp = await client.put(
                    "/api/agents/test-agent",
                    json={
                        "workspace": row["workspace"],  # untouched redacted prefill
                        "memory_store": "new-store",  # the actual edit
                    },
                )
                assert resp.status == 200

        stored = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert stored["agents"]["test-agent"]["workspace"] == CRED
        assert stored["agents"]["test-agent"]["memory_store"] == "new-store"

    @pytest.mark.asyncio
    async def test_stale_marker_echo_never_overwrites_a_newer_value(self, tmp_path):
        """The stale-save race (GPT round 3): between a sheet's GET and its
        save, a concurrent editor stores a NEW benign value. The stale sheet
        then echoes the marker, which no longer exact-matches the redaction
        of the CURRENT value — so the exact-echo guard alone lets it through
        and the newer value is destroyed. A pure-marker string is never a
        deliberate stored value, so the PUT refuses it outright."""
        import json
        from unittest.mock import patch as upatch

        cfg_file = _seed_config_file(tmp_path, {"triggers": CRED})
        with upatch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_make_crud_app(tmp_path))) as client:
                # The stale sheet's prefill: the roster's marker for CRED.
                resp = await client.get("/api/agents")
                row = {a["name"]: a for a in (await resp.json())["agents"]}["test-agent"]
                stale_prefill = row["triggers"]
                assert stale_prefill == CRED_MARKER

                # A concurrent editor stores a newer benign value.
                resp = await client.put(
                    "/api/agents/test-agent", json={"triggers": "newer benign value"}
                )
                assert resp.status == 200

                # The stale sheet saves, echoing its marker prefill.
                resp = await client.put("/api/agents/test-agent", json={"triggers": stale_prefill})
                assert resp.status == 200

        stored = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert stored["agents"]["test-agent"]["triggers"] == "newer benign value"

    @pytest.mark.asyncio
    async def test_stale_marker_with_interior_bracket_is_also_refused(self, tmp_path):
        """Exfil markers can contain an interior `]` — a bracketed IPv6 host
        renders as `[REDACTED: suspicious URL to [2001:db8::1]]` — so the
        pure-marker floor must anchor only on the marker prefix/suffix, not
        on `]` being absent from the interior (Opus review finding on #8465).
        Same stale-save race as above, with the IPv6-shaped marker."""
        import json
        from unittest.mock import patch as upatch

        exfil6 = "https://[2001:db8::1]/?d=" + "a" * 210
        cfg_file = _seed_config_file(tmp_path, {"workspace": exfil6})
        with upatch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_make_crud_app(tmp_path))) as client:
                resp = await client.get("/api/agents")
                row = {a["name"]: a for a in (await resp.json())["agents"]}["test-agent"]
                stale_prefill = row["workspace"]
                assert stale_prefill == "[REDACTED: suspicious URL to [2001:db8::1]]"

                resp = await client.put(
                    "/api/agents/test-agent", json={"workspace": "newer benign value"}
                )
                assert resp.status == 200

                resp = await client.put("/api/agents/test-agent", json={"workspace": stale_prefill})
                assert resp.status == 200

        stored = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert stored["agents"]["test-agent"]["workspace"] == "newer benign value"

    @pytest.mark.asyncio
    async def test_echo_probe_config_load_runs_off_the_event_loop(self, tmp_path):
        """The pre-lock echo probe's config load must not run on the event
        loop (AUTOSDE no-blocking-call-on-event-loop; GPT round 4): a slow
        config read would freeze every dashboard request. Pinned by recording
        the thread each load runs on — the probe (first load in the handler)
        must be off-loop, the same asyncio.to_thread shape api_members uses."""
        import threading
        from unittest.mock import patch as upatch

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = _seed_config_file(tmp_path, {"triggers": "benign"})
        loop_thread = threading.get_ident()
        load_threads: list[int] = []
        real_load = KiroCrewConfig.load

        def _recording_load(*a, **kw):
            load_threads.append(threading.get_ident())
            return real_load(*a, **kw)

        with (
            upatch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            upatch.object(KiroCrewConfig, "load", staticmethod(_recording_load)),
        ):
            async with TestClient(TestServer(_make_crud_app(tmp_path))) as client:
                resp = await client.put("/api/agents/test-agent", json={"triggers": "sev1"})
                assert resp.status == 200

        assert load_threads, "PUT never loaded the config"
        # The FIRST load in the handler is the pre-lock echo probe.
        assert load_threads[0] != loop_thread

    def test_non_string_values_pass_through(self):
        from kiro_crew.dashboard.handlers._shared import redact_record_strings

        record = {"count": 3, "ratio": 0.5, "flag": True, "none": None, "s": "hi"}
        assert redact_record_strings(record) == record

    def test_both_halves_run(self):
        from kiro_crew.dashboard.handlers._shared import redact_record_strings

        out = redact_record_strings({"a": CRED, "b": EXFIL})
        assert out == {"a": CRED_MARKER, "b": EXFIL_MARKER}


class TestRedactedFieldsManifest:
    """GET /api/agents marks WHICH fields it redacted.

    The manifest is what lets an edit client implement
    don't-echo-what-you-didn't-edit: without it, a stale form that still
    holds a marker cannot be told apart from a deliberate edit, and the
    backend echo guard alone cannot close the stale-save race (GPT review
    finding on #8465, round 3 — a concurrent benign update between the
    form's GET and its save would be overwritten by the echoed marker).
    """

    def test_manifest_names_exactly_the_changed_fields(self):
        from kiro_crew.dashboard.handlers._shared import redact_record_strings_marked

        out = redact_record_strings_marked(
            {"name": "a", "description": CRED, "workspace": EXFIL, "model": ""}
        )

        assert out["redacted_fields"] == ["description", "workspace"]
        assert out["description"] == CRED_MARKER

    def test_manifest_is_empty_for_a_clean_record(self):
        from kiro_crew.dashboard.handlers._shared import redact_record_strings_marked

        out = redact_record_strings_marked({"name": "a", "description": "benign"})
        assert out["redacted_fields"] == []

    @pytest.mark.asyncio
    async def test_agents_rows_carry_the_manifest(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = {"alpha": KiroCrewAgentConfig(kiro_agent="alpha", description=CRED)}

        data, _raw = await _get_agents(state, cfg)

        rows = {r["name"]: r for r in data["agents"]}
        assert rows["alpha"]["redacted_fields"] == ["description"]

    @pytest.mark.asyncio
    async def test_project_rows_carry_the_manifest_too(self, tmp_path):
        import dataclasses

        @dataclasses.dataclass
        class _PoisonedDefaults(KiroCrewAgentConfig):
            description: str = CRED

        state = _make_state(tmp_path)
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
                return_value=_fake_config({"alpha": KiroCrewAgentConfig(kiro_agent="alpha")}),
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.active_project_dir",
                return_value="/tmp/some-project",
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.project_agent_names",
                return_value=frozenset({"proj-agent"}),
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.KiroCrewAgentConfig",
                _PoisonedDefaults,
            ),
        ):
            async with TestClient(TestServer(_make_agents_app(state))) as client:
                resp = await client.get("/api/agents")
                assert resp.status == 200
                data = await resp.json()

        rows = {r["name"]: r for r in data["agents"]}
        assert rows["proj-agent"]["redacted_fields"] == ["description"]
        assert rows["alpha"]["redacted_fields"] == []
