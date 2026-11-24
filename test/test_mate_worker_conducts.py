# SPDX-License-Identifier: Apache-2.0
"""What a Bot's conducting worker receives on its projected spec
(rfc-bot-workers-conduct.md, section 5.3; Phase 1 exit criterion 2).

``mate_worker.worker_session_spec`` is the per-session projection every reader of a
worker's spec comes through. For a worker the Bot's own thread opened, while the
Bot's ``worker_conducts`` switch is on, it layers the conducting kit
(``mate_worker_conducts.conducting_worker_kit``) on the governed merge: the
conductor's work-ledger grants, the ``worker.conducts`` prompt piece rendered from the
shared conductor rules, and the ``goal-conductor`` skill only where the spec already
maps skills. With the switch off, or for a worker that is not thread-born, the spec
is byte for byte what it is today.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import agent, crewmate_prompts, mate_worker, members
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_PI
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mate_worker_conducts import CONDUCTS_PIECE, conducting_worker_kit

THREAD = "member-pixel"
WORKER = "dashboard:chat-7-1785396512"
GRANDCHILD = "dashboard:chat-8-1785396600"

BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "You are the worker.",
    "tools": ["fs_read", "@kirocrew-core", "@kirocrew-work"],
    "allowedTools": ["fs_read", "@kirocrew-core", *agent._WORKER_WORK_GRANTS],
    "resources": ["file://.kiro/steering/**/*.md"],
    "mcpServers": {
        "kirocrew-core": {"command": "kirocrew", "args": ["mcp", "core"]},
        "kirocrew-work": {"command": "kirocrew", "args": ["mcp-work"]},
    },
}

_WORKER_EXECUTION = ExecutionContext(
    "pixel-id",
    MemoryStoreRef("member-pixel", "pixel-id"),
    "template",
    mate_worker.MATE_WORKER_AGENT,
    "persistent",
)


def _dump(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True)


@pytest.fixture
def bot(monkeypatch, tmp_path):
    """``pixel`` with a worker on ``WORKER`` and a grandchild on ``GRANDCHILD``, both
    known as its workers, with no own file yet (the no-file path of
    ``resolve_worker_spec``; ``dashboard_file`` below writes one). The switch starts
    off."""
    from kiro_crew.config import paths

    record = KiroCrewAgentConfig(kiro_agent="pixel", member_id="pixel-id", worker_conducts=False)
    cfg = KiroCrewConfig()
    cfg.agents["pixel"] = record
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key, **_: _WORKER_EXECUTION if key in (WORKER, GRANDCHILD) else None,
    )
    user, mates = tmp_path / "agents", tmp_path / "mates"
    user.mkdir()
    mates.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "_claimed_file", lambda member, config=None: "")
    monkeypatch.setattr(mate_worker, "_binding_alias", lambda member: "pixel")
    monkeypatch.setattr(mate_worker, "_revoked_or_refuse", lambda member, fid: [])
    members.note_crewmate_worker_session(WORKER, "pixel-id")
    members.note_crewmate_worker_session(GRANDCHILD, "pixel-id")
    members.note_worker_lineage(WORKER, THREAD)  # the thread opened it
    members.note_worker_lineage(GRANDCHILD, WORKER)  # the worker opened it
    return record


#: The dashboard grants a Bot's own file can carry today: what Add abilities copies
#: from a template that mounts the server (the team lead's), plus one more.
_FILE_DASHBOARD_GRANTS = [*agent._CONDUCTOR_DASHBOARD_GRANTS, "@kirocrew-dashboard/session_send"]


@pytest.fixture
def dashboard_file(bot):
    """pixel's own file declares ``kirocrew-dashboard`` with a transport and its own
    grants: the shape Add abilities writes from a template that mounts the server
    (``mate_worker_kit`` copies ``mcpServers``, ``tools`` and ``allowedTools``), the
    one path that puts the dashboard server on a Bot's worker today."""
    from kiro_crew.config import paths

    spec = {
        "name": "pixel",
        "prompt": "Follow the release SOP.",
        "tools": ["@kirocrew-dashboard"],
        "allowedTools": list(_FILE_DASHBOARD_GRANTS),
        "mcpServers": {"kirocrew-dashboard": {"command": "kirocrew", "args": ["mcp-dashboard"]}},
    }
    (paths.mate_agents_dir() / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    assert mate_worker.mate_file_for_member("pixel-id") == "pixel"
    return bot


def _dashboard_allows(out: dict) -> set[str]:
    allowed: set[str] = set()
    for rule in (out.get("permissions") or {}).get("rules") or []:
        if rule.get("effect") == "allow" and rule.get("capability") == "mcp":
            allowed.update(
                m for m in rule.get("match") or [] if str(m).startswith("kirocrew-dashboard/")
            )
    return allowed


def _bare(refs) -> set[str]:
    return {ref.removeprefix("@") for ref in refs}


def _spec(key: str = WORKER) -> dict:
    return mate_worker.worker_session_spec(dict(BASE), key)


class TestOffIsTodaysSpecByteForByte:
    def test_the_switch_off_leaves_a_thread_born_worker_unchanged(self, bot):
        plain = _dump(mate_worker.resolve_worker_spec(dict(BASE), "pixel-id"))
        assert _dump(_spec()) == plain
        assert members.is_thread_born_worker_session(WORKER)
        assert CONDUCTS_PIECE not in _dump(_spec())
        assert "work_ledger_record" not in _dump(_spec())

    def test_the_switch_on_leaves_a_grandchild_unchanged(self, bot):
        bot.worker_conducts = True
        plain = _dump(mate_worker.resolve_worker_spec(dict(BASE), "pixel-id"))
        assert _dump(_spec(GRANDCHILD)) == plain

    def test_a_plain_call_passes_no_kit_argument(self, bot, monkeypatch):
        """The call shape a plain worker's projection makes is the one it made."""
        seen: list[dict] = []

        def _resolve(base, member_id, **kwargs):
            seen.append(kwargs)
            return dict(base)

        monkeypatch.setattr(mate_worker, "resolve_worker_spec", _resolve)
        _spec()
        assert seen == [{}]


class TestOnTheWorkerGetsTheKit:
    @pytest.fixture(autouse=True)
    def _on(self, bot):
        bot.worker_conducts = True

    def test_the_conductor_grants_join_allowed_tools(self):
        spec = _spec()
        plain = mate_worker.resolve_worker_spec(dict(BASE), "pixel-id")["allowedTools"]
        # Today's grants first and in order, then the conductor's ledger grants and
        # its conductor-mode dashboard verbs, each once.
        kit = (*agent._LEDGER_CONDUCTOR_WORK_GRANTS, *agent._CONDUCTOR_DASHBOARD_GRANTS)
        added = [g for g in kit if g not in plain]
        assert spec["allowedTools"] == [*plain, *added]
        assert set(kit) <= set(spec["allowedTools"])
        # No worker-excluded grant comes with them, and none of the thread's writes.
        assert not set(spec["allowedTools"]) & agent._WORKER_EXCLUDED_GRANTS
        assert "@kirocrew-dashboard/session_send" not in spec["allowedTools"]

    def test_the_piece_is_appended_after_the_whole_prompt(self):
        spec = _spec()
        plain = mate_worker.resolve_worker_spec(dict(BASE), "pixel-id")["prompt"]
        assert spec["prompt"].startswith(plain)
        from kiro_crew.context_assembly.member import ledger_rules_block

        assert spec["prompt"].endswith(ledger_rules_block(CONDUCTS_PIECE))
        assert "You may conduct." in spec["prompt"]

    def test_every_shared_conductor_rule_is_in_the_prompt(self):
        prompt = _spec()["prompt"]
        missing = sorted(
            name
            for name, rule in agent._LEDGER_CONDUCTOR_RULES.items()
            if rule.strip() not in prompt
        )
        assert missing == []

    def test_the_rest_of_the_spec_is_untouched(self):
        spec = _spec()
        plain = mate_worker.resolve_worker_spec(dict(BASE), "pixel-id")
        for key in ("name", "tools", "mcpServers", "resources"):
            assert spec[key] == plain[key], key
        # Nothing mounts the dashboard server on the spec: that entry is the
        # session array's (the member path), never a spec declaration.
        assert "kirocrew-dashboard" not in spec["mcpServers"]
        assert "@kirocrew-dashboard" not in spec["tools"]

    def test_an_unmapped_worker_gets_no_skill_mapping(self):
        assert not any(r.startswith("skill://") for r in _spec()["resources"])

    def test_a_mapped_worker_gets_goal_conductor_mapped_too(self, monkeypatch):
        from kiro_crew.agent_materialization import skill_resources

        mapped = {**BASE, "resources": [*BASE["resources"], "skill://release/SKILL.md"]}
        spec = mate_worker.worker_session_spec(mapped, WORKER)
        for uri in skill_resources.prompt_skill_uris("goal-conductor"):
            assert uri in spec["resources"]
        assert "skill://release/SKILL.md" in spec["resources"]

    def test_a_mapped_bot_file_keeps_its_knowledge_base(self, bot):
        """A knowledge base is an object entry in ``resources``: mapping the conductor
        skill beside the Bot file's own skill keeps it, so KAS still carries it."""
        from kiro_crew.agent_materialization import skill_resources
        from kiro_crew.config import paths

        kb = {"type": "knowledgeBase", "source": "file://docs/release", "name": "release-notes"}
        own = {"name": "pixel", "resources": ["skill://release/SKILL.md", kb]}
        (paths.mate_agents_dir() / "pixel.json").write_text(json.dumps(own), encoding="utf-8")
        assert mate_worker.mate_file_for_member("pixel-id") == "pixel"
        bot.worker_conducts = False
        assert kb in _spec()["resources"]  # the Bot file's entry reaches the merge
        bot.worker_conducts = True
        resources = _spec()["resources"]
        assert kb in resources
        assert "skill://release/SKILL.md" in resources
        for uri in skill_resources.prompt_skill_uris("goal-conductor"):
            assert uri in resources

    def test_the_grants_cross_the_ceiling(self, monkeypatch):
        from kiro_crew.platform import governance

        monkeypatch.setattr(
            governance, "may_skip_gate_now", lambda ref: ref != "@kirocrew-work/accept_eval"
        )
        spec = _spec()
        assert "@kirocrew-work/accept_eval" not in spec["allowedTools"]
        assert "@kirocrew-work/work_ledger_record" in spec["allowedTools"]
        # Mounted still: the server stays in tools, so the withheld verb prompts.
        assert "@kirocrew-work" in spec["tools"]

    def test_a_revocation_still_strips_a_kit_grant(self, monkeypatch):
        """Everything else is untouched: an approval the owner took away stays away."""
        monkeypatch.setattr(
            mate_worker,
            "_revoked_or_refuse",
            lambda member, fid: ["@kirocrew-work/work_ledger_record"],
        )
        spec = _spec()
        assert "@kirocrew-work/work_ledger_record" not in spec["allowedTools"]
        assert "@kirocrew-work/work_ledger_read" in spec["allowedTools"]


class TestABotFileThatAlreadyMountsTheDashboardServer:
    """The one existing path that puts ``kirocrew-dashboard`` on a Bot's worker: the
    Bot's own file declares it (Add abilities from a template that mounts it).
    Exit criterion 2 holds for it: with the switch off that worker keeps exactly
    today's spec and today's grants; with it on, the conductor-mode set joins the
    file's own and the file's additions stay untouched."""

    def test_the_path_is_real(self, dashboard_file):
        """The file's server, selector and grants reach the governed merge, as the
        RFC's current-state section says they do."""
        from kiro_crew.acp.kas_agents import to_client_custom_agent

        spec = _spec()
        assert "kirocrew-dashboard" in spec["mcpServers"]
        assert "@kirocrew-dashboard" in spec["tools"]
        assert set(_FILE_DASHBOARD_GRANTS) <= set(spec["allowedTools"])
        out = to_client_custom_agent("kirocrew-mate-worker", spec, "p", session_key=WORKER)
        assert _dashboard_allows(out) == _bare(_FILE_DASHBOARD_GRANTS)

    def test_the_switch_off_keeps_todays_spec_and_grants_byte_for_byte(self, dashboard_file):
        from kiro_crew.acp.kas_agents import to_client_custom_agent

        plain = mate_worker.resolve_worker_spec(dict(BASE), "pixel-id")
        spec = _spec()
        assert _dump(spec) == _dump(plain)
        assert CONDUCTS_PIECE not in _dump(spec)
        assert "work_ledger_record" not in _dump(spec)
        # The projection a worker gets today (no member flag) grants exactly the
        # file's own verbs: the key-aware grant pick lives inside the member block,
        # which a worker reaches on no path while its Bot's switch is off.
        keyed = to_client_custom_agent("kirocrew-mate-worker", dict(spec), "p", session_key=WORKER)
        assert _dashboard_allows(keyed) == _bare(_FILE_DASHBOARD_GRANTS)

    def test_the_switch_on_adds_the_conductor_set_and_keeps_the_files_additions(
        self, dashboard_file
    ):
        from kiro_crew.acp.kas_agents import to_client_custom_agent

        dashboard_file.worker_conducts = True
        spec = _spec()
        # The file's additions are untouched: its server, selector and every grant.
        assert "kirocrew-dashboard" in spec["mcpServers"]
        assert set(_FILE_DASHBOARD_GRANTS) <= set(spec["allowedTools"])
        assert set(agent._LEDGER_CONDUCTOR_WORK_GRANTS) <= set(spec["allowedTools"])
        assert CONDUCTS_PIECE in crewmate_prompts.keys() and "You may conduct." in spec["prompt"]
        # Mounted through the member path, the conductor set the kit carries is
        # granted beside the file's own: the file's session_send stays the Bot's choice.
        out = to_client_custom_agent(
            "kirocrew-mate-worker", spec, "p", member_dispatch=True, session_key=WORKER
        )
        assert _dashboard_allows(out) == _bare(_FILE_DASHBOARD_GRANTS) | _bare(
            agent._CONDUCTOR_DASHBOARD_GRANTS
        )

    def test_a_grandchild_keeps_todays_spec_whatever_the_switch(self, dashboard_file):
        dashboard_file.worker_conducts = True
        plain = mate_worker.resolve_worker_spec(dict(BASE), "pixel-id")
        assert _dump(_spec(GRANDCHILD)) == _dump(plain)


@pytest.fixture
def own_file(bot):
    """pixel's own file with no server of its own, so its worker takes the own-file
    path of ``resolve_worker_spec``, where the Bot's held limits apply."""
    from kiro_crew.config import paths

    spec = {"name": "pixel", "prompt": "Follow the release SOP."}
    (paths.mate_agents_dir() / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    assert mate_worker.mate_file_for_member("pixel-id") == "pixel"
    return bot


_HELD_MCP = {
    "migrated": ["pixel"],
    "held_rules": {
        "pixel": {
            "member_id": "pixel-id",
            "rules": [
                {"text": "its permissions rule to ask before any MCP call", "capability": "mcp"}
            ],
        }
    },
}


def _project(spec: dict) -> dict:
    """The KAS agent a conducting worker's start projects: mounted, keyed by the worker."""
    from kiro_crew.acp.kas_agents import to_client_custom_agent

    return to_client_custom_agent(
        "kirocrew-mate-worker", spec, "p", member_dispatch=True, session_key=WORKER
    )


class TestTheBotsLimitsReachTheDashboardVerbs:
    """The conductor-mode dashboard verbs ride the worker's spec, so the Bot's own
    limits strip them as they strip every grant: under a held limit they ask, and a
    verb the owner revoked stays revoked. KAS runs a pre-approved call with no
    permission request, so a verb left pre-approved would never reach the gate that
    asks a person."""

    @pytest.fixture(autouse=True)
    def _on(self, bot):
        bot.worker_conducts = True

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(json.dumps(_HELD_MCP), id="held-mcp"),
            # Unreadable, the record holds every capability.
            pytest.param("{not json", id="unreadable-record"),
        ],
    )
    def test_a_held_limit_leaves_no_dashboard_verb_pre_approved(self, own_file, record):
        from kiro_crew.config import paths

        (paths.mate_agents_dir().parent / "migrated.json").write_text(record, encoding="utf-8")
        spec = _spec()
        assert not [ref for ref in spec["allowedTools"] if ref.startswith("@kirocrew-dashboard/")]
        out = _project(spec)
        # Still mounted, so each call reaches the gate, which asks a person.
        assert "@kirocrew-dashboard" in out["tools"]
        assert _dashboard_allows(out) == set()

    @pytest.mark.parametrize("with_file", [False, True], ids=["no-file", "own-file"])
    def test_a_revoked_verb_stays_revoked(self, bot, request, monkeypatch, with_file):
        if with_file:
            request.getfixturevalue("own_file")
        monkeypatch.setattr(
            mate_worker,
            "_revoked_or_refuse",
            lambda member, fid: ["@kirocrew-dashboard/session_create"],
        )
        out = _project(_spec())
        assert _dashboard_allows(out) == _bare(agent._CONDUCTOR_DASHBOARD_GRANTS) - {
            "kirocrew-dashboard/session_create"
        }


class TestTheKasKitFollowsTheStartsMount:
    """KAS's projection knows whether the session's start took the session-control
    mount and hands it down (``mate_worker_projection(..., mounted=member_dispatch)``),
    so the worker spec's kit follows that mount. A load that mounts nothing, the
    re-inject of a worker that started plain with its Bot's switch turned on since,
    carries no conductor verbs; a start that mounted is not judged again against the
    switch; and a worker a worker opened never gets the kit."""

    @staticmethod
    async def _projected(monkeypatch, tmp_path, *, member_dispatch: bool, key: str) -> dict:
        from kiro_crew.acp import kas_agents as kas_agents_mod
        from kiro_crew.acp.harness import harness_for
        from kiro_crew.config import paths
        from kiro_crew.mcp_gateway import session_servers

        monkeypatch.setattr(agent, "require_fork_governance", lambda agent, work_dir: None)
        monkeypatch.setattr(agent, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(
            session_servers, "injection_server_names", lambda overlay, agent, **kw: frozenset()
        )
        name = mate_worker.MATE_WORKER_AGENT
        (paths.kiro_agents_dir() / f"{name}.json").write_text(json.dumps(BASE), encoding="utf-8")
        projected: list[dict] = []

        def _build(agents_dir, agent_id, spec, **_kw):
            projected.append(spec)
            return [{"id": agent_id}]

        monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _build)
        work = tmp_path / "work"
        work.mkdir()
        await harness_for("kas").session_extras(
            name, work_dir=str(work), member_dispatch=member_dispatch, session_key=key
        )
        (spec,) = projected
        return spec

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("switch", "member_dispatch", "key", "kit"),
        [
            pytest.param(True, False, WORKER, False, id="a-load-that-mounts-nothing"),
            pytest.param(True, True, WORKER, True, id="a-start-that-mounted"),
            pytest.param(False, True, WORKER, True, id="mounted-then-a-save-turned-it-off"),
            pytest.param(True, True, GRANDCHILD, False, id="a-workers-worker"),
        ],
    )
    async def test_the_kit_follows_the_mount(
        self, bot, monkeypatch, tmp_path, switch, member_dispatch, key, kit
    ):
        bot.worker_conducts = switch
        spec = await self._projected(
            monkeypatch, tmp_path, member_dispatch=member_dispatch, key=key
        )
        conductor_verbs = {*agent._CONDUCTOR_DASHBOARD_GRANTS, "@kirocrew-work/work_ledger_record"}
        assert ("You may conduct." in spec["prompt"]) is kit
        assert (conductor_verbs <= set(spec["allowedTools"])) is kit
        if not kit:
            assert not conductor_verbs & set(spec["allowedTools"])


_CONDUCTOR_VERBS = {*agent._CONDUCTOR_DASHBOARD_GRANTS, "@kirocrew-work/work_ledger_record"}


def _has_kit(spec: dict | None) -> bool:
    """Whether *spec* carries the conducting kit, which comes whole or not at all."""
    spec = spec or {}
    piece = "You may conduct." in (spec.get("prompt") or "")
    allowed = set(spec.get("allowedTools") or [])
    assert piece is (_CONDUCTOR_VERBS <= allowed)
    assert piece or not _CONDUCTOR_VERBS & allowed
    return piece


@pytest.fixture
def composer(bot, monkeypatch, tmp_path):
    """pixel's worker composed by a per-chat client the way its start composes it:
    the stock worker base on disk, pixel's switch on, and a global settings file a
    test may switch a server off in. Yields ``(compose, settings)``; ``compose(backend,
    client=None)`` composes a new client on *backend*, or *client* again (a respawn
    inside its turn), and returns it."""
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.config import paths

    name = mate_worker.MATE_WORKER_AGENT
    (paths.mate_agents_dir() / f"{name}.json").write_text(json.dumps(BASE), encoding="utf-8")
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    settings: dict = {}
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **_kw: settings)
    bot.worker_conducts = True
    clients: list = []

    def compose(backend: str, client=None):
        if client is None:
            work = tmp_path / f"work-{len(clients)}"
            work.mkdir()
            client = AcpClient(work_dir=work, agent=name, acp_backend=backend)
            client._session_key = WORKER
            client.member_context = True
            clients.append(client)
            if backend == ACP_BACKEND_CLAUDE:
                client._write_claude_local_settings()
                assert client._permission_surface_governed
        client._resolve_session_mcp_servers()
        return client

    yield compose, settings
    for client in clients:
        try:
            if client.backend == ACP_BACKEND_CLAUDE:
                asyncio.run(client._discard_claude_settings_seed())
        finally:
            client._reset_state()


class TestTheKitFollowsTheClientsMount:
    """A per-chat client decides what its start mounts before it reads the worker's
    spec (``mate_worker_conducts.start_mounts_session_control``) and hands that answer
    to the projection, so the conducting kit rides only a start that holds session
    control. A mount the parse then withholds is projected again with no kit. KAS's
    projection follows its start's mount the same way (``TestTheKasKitFollowsTheStartsMount``)."""

    BACKENDS = pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_PI])

    @BACKENDS
    def test_a_start_that_mounts_keeps_its_kit(self, composer, backend):
        compose, _settings = composer
        client = compose(backend)
        assert client._member_dispatch_mounted is True
        assert _has_kit(client._session_agent_spec)

    @BACKENDS
    def test_a_switched_off_dashboard_server_leaves_no_kit(self, composer, backend):
        from kiro_crew.members import MEMBER_DISPATCH_SERVER

        compose, settings = composer
        settings["mcpServers"] = {MEMBER_DISPATCH_SERVER: {"disabled": True}}
        client = compose(backend)
        assert client._member_dispatch_mounted is False
        assert not _has_kit(client._session_agent_spec)

    @BACKENDS
    def test_a_respawn_after_a_switch_on_mid_turn_leaves_no_kit(self, composer, bot, backend):
        compose, _settings = composer
        bot.worker_conducts = False
        client = compose(backend)  # its start: a plain worker
        assert client._member_dispatch_mounted is False
        assert not _has_kit(client._session_agent_spec)
        bot.worker_conducts = True  # the owner turns it on while the turn runs
        compose(backend, client)  # a respawn inside that turn composes again
        assert client._member_dispatch_mounted is False
        assert not _has_kit(client._session_agent_spec)

    @BACKENDS
    def test_a_dashboard_server_the_bot_file_switches_off_leaves_no_kit(self, composer, backend):
        """pixel's own file mounts the dashboard server (Add abilities from a template
        that does) and its owner then switched it off. The start's own answer reads the
        spec built for the session, that file included, so the start mounts nothing and
        the check before the claim keeps the person's mode for it as well."""
        from kiro_crew.acp import session_mcp
        from kiro_crew.config import paths
        from kiro_crew.mate_worker_conducts import start_mounts_session_control
        from kiro_crew.members import MEMBER_DISPATCH_SERVER

        compose, _settings = composer
        own = {
            "name": "pixel",
            "mcpServers": {
                MEMBER_DISPATCH_SERVER: {
                    "command": "kirocrew",
                    "args": ["mcp-dashboard"],
                    "disabled": True,
                }
            },
        }
        (paths.mate_agents_dir() / "pixel.json").write_text(json.dumps(own), encoding="utf-8")
        assert mate_worker.mate_file_for_member("pixel-id") == "pixel"
        client = compose(backend)
        assert client._member_dispatch_mounted is False
        assert not _has_kit(client._session_agent_spec)
        agent_name = mate_worker.MATE_WORKER_AGENT
        assert not start_mounts_session_control(WORKER, backend, agent_name, client._work_dir)
        # The session's key is what brings the Bot's file into the read.
        work = client._work_dir
        assert not session_mcp.session_mcp_server_is_disabled(MEMBER_DISPATCH_SERVER, agent_name)
        assert session_mcp.session_mcp_server_is_disabled(
            MEMBER_DISPATCH_SERVER, agent_name, work_dir=work, session_key=WORKER
        )

    @BACKENDS
    def test_the_append_mounts_on_the_starts_own_answer(self, composer, monkeypatch, backend):
        """One decision per composition: the start's answer says plain while the switch
        reads on (a save between two reads), and the append mounts on that answer, so
        the start is plain, mount and kit alike, rather than mounted with no kit."""
        from kiro_crew import mate_worker_conducts

        compose, _settings = composer
        monkeypatch.setattr(
            mate_worker_conducts, "start_mounts_session_control", lambda *_a, **_k: False
        )
        assert members.mounts_session_control(WORKER)
        client = compose(backend)
        assert client._member_dispatch_mounted is False
        assert not _has_kit(client._session_agent_spec)

    @BACKENDS
    def test_a_mount_the_composition_cannot_make_is_projected_again_without_the_kit(
        self, composer, monkeypatch, backend
    ):
        """The start's answer said mount; the composition could not (here the dashboard
        server's launch does not resolve), so the spec the session consumes is the one
        with no kit."""
        compose, _settings = composer
        monkeypatch.setattr(members, "member_dispatch_session_server", lambda *_a, **_k: None)
        client = compose(backend)
        assert client._member_dispatch_mounted is False
        assert not _has_kit(client._session_agent_spec)

    def test_a_narrowed_dashboard_server_on_pi_is_projected_again_without_the_kit(self, composer):
        """pi has no per-tool deny, so its client withholds a dashboard server with a
        tool switched off: a withhold only the composition makes, after the start's
        answer said mount, so the spec it consumes is projected again with no kit."""
        from kiro_crew.mate_worker_conducts import start_mounts_session_control
        from kiro_crew.members import MEMBER_DISPATCH_SERVER

        compose, settings = composer
        settings["mcpServers"] = {MEMBER_DISPATCH_SERVER: {"disabledTools": ["session_create"]}}
        client = compose(ACP_BACKEND_PI)
        agent_name = mate_worker.MATE_WORKER_AGENT
        assert start_mounts_session_control(WORKER, ACP_BACKEND_PI, agent_name, client._work_dir)
        assert client._member_dispatch_mounted is False
        assert not _has_kit(client._session_agent_spec)


class TestTheMirrorsHandTheMountDown:
    """The mirrors that host a worker pass the start's mount answer to the projection
    (``session_control_mounted``): claude through its keyword arguments, codex by name,
    which the runtime fills on ``session/new`` and ``session/load``."""

    @pytest.fixture(autouse=True)
    def _base(self, bot, _floor_monkeypatch):
        from kiro_crew.acp import session_mcp
        from kiro_crew.config import paths

        name = mate_worker.MATE_WORKER_AGENT
        (paths.mate_agents_dir() / f"{name}.json").write_text(json.dumps(BASE), encoding="utf-8")
        _floor_monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
        _floor_monkeypatch.setattr(session_mcp, "_global_settings", lambda **_kw: {})
        bot.worker_conducts = True

    @staticmethod
    def _claude(mounted):
        from kiro_crew.providers.mirrors.claude_code import ClaudeCodeMirror

        return ClaudeCodeMirror().session_projection(
            mate_worker.MATE_WORKER_AGENT,
            permission_surface_owned=True,
            session_key=WORKER,
            session_control_mounted=mounted,
        )

    @staticmethod
    def _codex(mounted):
        from kiro_crew.providers.mirrors.codex import CodexMirror

        return CodexMirror().session_projection(
            mate_worker.MATE_WORKER_AGENT, session_key=WORKER, session_control_mounted=mounted
        )

    @pytest.mark.parametrize("mirror", ["_claude", "_codex"])
    @pytest.mark.parametrize(("mounted", "kit"), [(True, True), (False, False), (None, True)])
    def test_the_projection_follows_the_answer(self, mirror, mounted, kit):
        assert _has_kit(getattr(self, mirror)(mounted).agent_spec) is kit


class TestTheContextBlockFollowsTheMount:
    """claude, codex and pi read the worker's prompt from the ``[AGENT SYSTEM PROMPT]``
    block, so the conducting rules ride it only for a session whose start mounted
    session control (its live ``member_dispatch_mounted``); a turn with no session
    evidence reads the switch."""

    @pytest.fixture(autouse=True)
    def _block(self, bot, _floor_monkeypatch):
        from kiro_crew.config import paths
        from kiro_crew.members_parts import member_sessions

        name = mate_worker.MATE_WORKER_AGENT
        (paths.mate_agents_dir() / f"{name}.json").write_text(json.dumps(BASE), encoding="utf-8")
        _floor_monkeypatch.setattr(member_sessions, "_CREWMATE_WORKER_PROMPT_IN_CONTEXT", set())
        member_sessions.note_crewmate_worker_prompt_channel(WORKER, True)
        bot.worker_conducts = True

    @pytest.mark.parametrize(("mounted", "piece"), [(True, True), (False, False), (None, True)])
    def test_the_block_follows_the_live_sessions_mount(self, mounted, piece):
        from kiro_crew.context_assembly.agent_spec import _crewmate_worker_prompt

        block = _crewmate_worker_prompt(
            mate_worker.MATE_WORKER_AGENT, "The contract.", WORKER, mounted
        )
        assert ("You may conduct." in block) is piece

    @pytest.mark.parametrize("mounted", [True, False])
    def test_the_resolved_contract_carries_the_mount_through(self, tmp_path, monkeypatch, mounted):
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        contract = tmp_path / "prompt.md"
        contract.write_text("The contract.", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: contract)
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        block = builder._resolve_agent_prompt(
            mate_worker.MATE_WORKER_AGENT,
            project=None,
            mode="",
            session_key=WORKER,
            private_owner=False,
            session_start=True,
            member_dispatch_mounted=mounted,
        )
        assert ("You may conduct." in block) is mounted


class TestTheKitAlone:
    def test_it_is_idempotent_on_grants_and_appends_the_piece_once(self):
        spec = conducting_worker_kit(dict(BASE))
        again = conducting_worker_kit(json.loads(json.dumps(spec)))
        assert again["allowedTools"] == spec["allowedTools"]
        # The piece is appended by each call; the projection calls it once.
        assert again["prompt"].count("You may conduct.") == 2

    def test_the_piece_exists_and_names_only_shared_rules(self):
        assert CONDUCTS_PIECE in crewmate_prompts.keys()
        assert crewmate_prompts.placeholders(CONDUCTS_PIECE) <= set(agent._LEDGER_CONDUCTOR_RULES)
