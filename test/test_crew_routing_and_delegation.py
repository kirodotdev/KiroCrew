"""Crew routing and delegation: the seam that keeps a crew's memory its own.

Three surfaces, one contract. ``trigger_match`` decides which crew a task
belongs to, ``route_crew`` reports that ranking, and ``spawn_run(crew=...)`` is
the only way to act on it that carries the crew's memory silo with it.

The failure this file exists to catch is not a crash. It is a task quietly
handled by the wrong crew: ``spawn_run(agent=<crew name>)`` looks like
delegation, is accepted, runs — and reads the operator's global store, because
``agent`` names a kiro-cli template and a crew name simply is not one. So the
assertions are about WHICH STORE a delegation lands in, not about whether the
call succeeded.
"""

from __future__ import annotations

import json

import pytest
from member_memory_helpers import patch_private_memory_supported, write_member_manifest

from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, rank_triggered, trigger_score, words_of

# ── the shared primitive ──


class TestTriggerScoring:
    def test_a_fully_present_phrase_scores_one(self):
        score, negated = trigger_score("fix the bug", words_of("please fix the bug now"))
        assert score == 1.0 and negated is False

    def test_score_is_the_fraction_of_the_phrases_own_words(self):
        # Three words, two present. A phrase is scored by how completely IT is
        # matched, so a long specific phrase cannot win on one shared word.
        score, _ = trigger_score("review the diff", words_of("review the code"))
        assert score == pytest.approx(2 / 3)

    def test_an_entry_takes_its_best_phrase(self):
        score, _ = trigger_score("nothing alike, fix the bug", words_of("fix the bug"))
        assert score == 1.0

    def test_a_negative_vetoes_however_well_the_positives_scored(self):
        score, negated = trigger_score(
            "fix the bug, !production", words_of("fix the bug in production")
        )
        assert score == 1.0, "the positive must still be scored"
        assert negated is True, "and the negative must still veto it"

    def test_phrase_order_cannot_change_the_outcome(self):
        a = trigger_score("!production, fix the bug", words_of("fix the bug in production"))
        b = trigger_score("fix the bug, !production", words_of("fix the bug in production"))
        assert a == b

    def test_a_negative_needs_all_of_its_words(self):
        _, negated = trigger_score("fix, !production outage", words_of("fix the production bug"))
        assert negated is False, "'outage' is absent, so the veto must not fire"


class TestRanking:
    ROSTER = [
        ("coding crew", "fix the bug, review the pull request"),
        ("email crew", "draft a reply, inbox"),
        ("quiet crew", ""),
    ]

    def test_the_matching_crew_wins(self):
        assert [n for n, _ in rank_triggered("fix the bug", self.ROSTER)] == ["coding crew"]

    def test_an_unrelated_task_matches_nothing(self):
        # The important half: no match must mean NO match, not the least-bad
        # crew. A router that always answers routes email into the codebase.
        assert rank_triggered("what is the weather in Tokyo", self.ROSTER) == []

    def test_a_crew_without_triggers_is_never_a_candidate(self):
        """DOUBLY guaranteed, so this cannot fail on the explicit guard alone.

        An empty, whitespace-only or comma-only trigger list scores 0.0, which is
        already below the floor — verified, not assumed. So deleting
        ``rank_triggered``'s explicit skip changes no outcome and this test stays
        green: it covers the PROPERTY, not that one line. Kept because the
        property is the operator's opt-out from being routed to, and a future
        change to the floor could make the skip load-bearing.
        """
        assert trigger_score("", words_of("quiet crew"))[0] < MIN_TRIGGER_OVERLAP
        assert all(n != "quiet crew" for n, _ in rank_triggered("quiet crew", self.ROSTER))

    def test_ties_keep_the_operators_roster_order(self):
        tied = [("first", "handle the ticket"), ("second", "handle the ticket")]
        assert [n for n, _ in rank_triggered("handle the ticket", tied)] == ["first", "second"]

    def test_below_the_floor_does_not_match(self):
        low = [("crew", "one two three four five")]
        assert rank_triggered("one", low) == []
        assert MIN_TRIGGER_OVERLAP > 0.2, "a single word must not carry a five-word phrase"

    def test_the_skills_loader_scores_through_this_module(self):
        """One definition of "matches", not two that agree today.

        The skills loader and crew routing read the same operator-authored
        grammar. A second implementation would diverge exactly on the phrasings
        that matter and the symptom would be a misrouted task.
        """
        import inspect

        from kiro_crew import skills

        src = inspect.getsource(skills.SkillsLoader.get_triggered_skills)
        assert "trigger_score(" in src, "the loader must delegate, not re-implement"
        assert skills._MIN_TRIGGER_OVERLAP == MIN_TRIGGER_OVERLAP


# ── routing and delegation over a real config ──


def _materialize_private_stores(config):
    """Create readable, empty owned stores for routing execution fixtures."""
    from kiro_crew.memory_stores import memory_store_dir_for
    from kiro_crew.vector_memory import VectorMemoryStore

    for name, record in config.memory_stores.items():
        if record.memory_version != 2:
            continue
        root = memory_store_dir_for(name)
        write_member_manifest(root, record.owner_member)
        vector = VectorMemoryStore(db_path=root / "memory.db")
        vector.init()
        vector.close()


@pytest.fixture
def crew_config(tmp_path, monkeypatch):
    """A roster with two crews on two silos, plus the default crew."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "memory_stores": {
                    "coding": {"owner_member": "coding crew", "memory_version": 2},
                    "email": {"owner_member": "email crew", "memory_version": 2},
                },
                "default_agent": "kirocrew",
                "agents": {
                    "kirocrew": {},
                    "coding crew": {
                        "memory_store": "coding",
                        "triggers": "fix the bug, review the pull request",
                        "description": "Owns the codebase",
                    },
                    "email crew": {
                        "memory_store": "email",
                        "triggers": "draft a reply, inbox",
                        "description": "Owns correspondence",
                    },
                },
            }
        )
    )
    from kiro_crew.config import loader as loader_mod
    from kiro_crew.config.loader import KiroCrewConfig

    loader_mod._invalidate_config_cache()
    config = KiroCrewConfig.load()
    _materialize_private_stores(config)
    yield config
    loader_mod._invalidate_config_cache()


class TestRouteCrew:
    def test_broken_member_does_not_hide_ranked_healthy_matches(self, crew_config, monkeypatch):
        from dataclasses import replace

        from kiro_crew import mcp_core

        legacy = replace(crew_config.agents["coding crew"], memory_store="missing-store")
        crew_config.agents = {"legacy": legacy, **crew_config.agents}
        for name in ("legacy", "coding crew", "email crew"):
            crew_config.agents[name].triggers = "fix the bug"
        monkeypatch.setattr(mcp_core.KiroCrewConfig, "load", lambda: crew_config)

        out = json.loads(mcp_core._do_route_crew("fix the bug"))

        assert [item["crew"] for item in out["matches"]] == ["coding crew", "email crew"]
        assert [item["memory_store"] for item in out["matches"]] == ["coding", "email"]
        assert [item["crew"] for item in out["unavailable"]] == ["legacy"]
        assert "missing or invalid memory binding" in out["unavailable"][0]["reason"]
        assert "memory_store" not in out["unavailable"][0]

    def test_all_unavailable_is_distinct_from_no_trigger_match(self, crew_config, monkeypatch):
        from kiro_crew import mcp_core

        crew_config.agents["coding crew"].memory_store = "default"
        monkeypatch.setattr(mcp_core.KiroCrewConfig, "load", lambda: crew_config)
        out = json.loads(mcp_core._do_route_crew("fix the bug"))
        assert out["matches"] == []
        assert [item["crew"] for item in out["unavailable"]] == ["coding crew"]
        assert "do not substitute Global" in out["guidance"]
        unmatched = json.loads(mcp_core._do_route_crew("weather in Tokyo"))
        assert unmatched["matches"] == unmatched["unavailable"] == []

    @pytest.mark.parametrize("named", [False, True])
    def test_memory_refusal_redacts_before_bounding_and_does_not_record_a_binding(
        self, crew_config, monkeypatch, named
    ):
        from unittest.mock import Mock

        from kiro_crew import mcp_core
        from kiro_crew.memory_stores import UnknownMemoryStore

        secret = "AKIAIOSFODNN7EXAMPLE"
        reason = f"private memory unavailable: /home/alice/private/memory.db {secret} " + "x" * 1200
        monkeypatch.setattr(
            mcp_core, "resolve_agent_bindings", Mock(side_effect=UnknownMemoryStore(reason))
        )
        activity = Mock()
        monkeypatch.setattr(mcp_core, "record_activity", activity)

        out = json.loads(
            mcp_core._do_select_crew("coding crew")
            if named
            else mcp_core._do_route_crew("fix the bug")
        )
        safe = out["error"] if named else out["unavailable"][0]["reason"]
        assert safe.startswith("private memory unavailable:")
        assert "/home/alice" not in safe and secret not in safe
        assert len(safe) <= 1000
        assert "bound" not in out
        if not named:
            assert out["matches"] == []
        activity.assert_not_called()

    @pytest.mark.parametrize("named", [False, True])
    def test_unrelated_binding_errors_are_not_silenced(self, crew_config, monkeypatch, named):
        from unittest.mock import Mock

        from kiro_crew import mcp_core

        monkeypatch.setattr(
            mcp_core, "resolve_agent_bindings", Mock(side_effect=RuntimeError("binding defect"))
        )
        with pytest.raises(RuntimeError, match="binding defect"):
            if named:
                mcp_core._do_select_crew("coding crew")
            else:
                mcp_core._do_route_crew("fix the bug")

    def test_a_coding_task_routes_to_the_coding_crew_and_names_its_store(self, crew_config):
        from kiro_crew import mcp_core

        out = json.loads(mcp_core._do_route_crew("please fix the bug in the parser"))
        assert [m["crew"] for m in out["matches"]] == ["coding crew"]
        assert out["matches"][0]["memory_store"] == "coding"
        assert out["matches"][0]["description"] == "Owns the codebase"

    def test_an_email_task_routes_to_the_email_crew(self, crew_config):
        from kiro_crew import mcp_core

        out = json.loads(mcp_core._do_route_crew("draft a reply to this inbox thread"))
        assert [m["crew"] for m in out["matches"]] == ["email crew"]
        assert out["matches"][0]["memory_store"] == "email"

    def test_no_match_reports_the_default_and_no_crew(self, crew_config):
        from kiro_crew import mcp_core

        out = json.loads(mcp_core._do_route_crew("what is the weather in Tokyo"))
        assert out["matches"] == []
        assert out["default_agent"] == "kirocrew"

    def test_the_default_crew_is_never_a_routing_target(self, crew_config):
        from kiro_crew import mcp_core

        out = json.loads(mcp_core._do_route_crew("fix the bug"))
        assert all(m["crew"] != "kirocrew" for m in out["matches"])

    def test_an_empty_task_is_refused(self, crew_config):
        from kiro_crew import mcp_core

        assert "error" in json.loads(mcp_core._do_route_crew("   "))


class TestDelegationCarriesTheCrewsStore:
    def test_each_crew_resolves_to_its_own_store_and_they_differ(self, crew_config):
        from kiro_crew.config.loader import resolve_agent_bindings

        coding = resolve_agent_bindings(crew_config, "coding crew").memory_store_name
        email = resolve_agent_bindings(crew_config, "email crew").memory_store_name
        assert (coding, email) == ("coding", "email")

    def test_a_crew_name_is_not_a_template_name(self, crew_config):
        """The leak, stated as a test.

        Passing a crew name where a TEMPLATE is expected does not fail — it
        resolves to the default store, so the delegation runs against the
        operator's own memory. This is why delegation has its own parameter.
        """
        from kiro_crew.context import _target_key

        _, via_crew = _target_key(None, "coding")
        assert via_crew == "coding"
        key_via_agent, via_agent = _target_key(None, None)
        assert via_agent == "" and key_via_agent == "default"

    def test_the_schema_accepts_a_crew_name_containing_a_space(self):
        """Crew creation only strips the name, so a regex here would refuse a
        crew the operator can see in their own roster."""
        from kiro_crew.validation import SPAWN_RUN_SCHEMA, validate_tool_args

        cleaned = validate_tool_args(
            {"task": "fix the bug", "crew": "coding crew"}, SPAWN_RUN_SCHEMA
        )
        assert cleaned.get("crew") == "coding crew"

    def test_subagent_info_carries_a_store_and_defaults_to_the_global_one(self):
        from kiro_crew.subagent import SubagentInfo

        assert SubagentInfo(id="a", task="t").memory_store == ""
        assert SubagentInfo(id="a", task="t", memory_store="coding").memory_store == "coding"

    @pytest.mark.asyncio
    async def test_legacy_named_store_keeps_optional_vector_preparation(self, crew_config):
        """Legacy V1 named stores retain their own keyword/Markdown fallback."""
        from unittest.mock import MagicMock

        from kiro_crew.config.sections import MemoryStoreConfig
        from kiro_crew.context import prepare_store_vectors
        from kiro_crew.memory_stores import memory_store_dir_for

        crew_config.memory_stores["legacy"] = MemoryStoreConfig()
        crew_config.save()
        memory_store_dir_for("legacy").mkdir(parents=True)

        class Raises:
            def ensure_store(self, _name):
                raise RuntimeError("boom")

        await prepare_store_vectors(object(), "legacy")  # no ensure_store at all
        await prepare_store_vectors(MagicMock(), "legacy")  # not awaitable
        await prepare_store_vectors(Raises(), "legacy")  # raises
        await prepare_store_vectors(MagicMock(), "")  # no store named

    @pytest.mark.asyncio
    async def test_a_named_store_is_actually_stood_up(self, crew_config, monkeypatch):
        """The other direction: the guard must not have made it a no-op."""
        from kiro_crew import context as ctx_mod
        from kiro_crew.context import ContextBuilder, prepare_store_vectors

        # This test opens the real owned store but never starts a provider.
        # Host sandbox capability is covered by the member execution tests.
        patch_private_memory_supported(monkeypatch)
        builder = ContextBuilder()
        try:
            await prepare_store_vectors(builder, "coding")
            assert "coding" in ctx_mod._vector_stores
            assert ctx_mod._vector_stores["coding"]._db_path.name == "memory.db"
            assert "memory_stores/coding" in ctx_mod._vector_stores["coding"]._db_path.as_posix()
        finally:
            for store in ctx_mod._vector_stores.values():
                try:
                    store.close()
                except Exception:
                    pass
            ctx_mod._vector_stores.clear()
            ctx_mod._memory_stores.clear()
            ctx_mod._lesson_stores.clear()


class TestTheMembersRosterCarriesIdentity:
    def test_description_and_triggers_are_exposed(self, crew_config):
        """A roster that shows a crew's store but not what it is for cannot
        answer "which of these should handle a ticket"."""
        import inspect

        from kiro_crew.dashboard.handlers import members as members_handler

        src = inspect.getsource(members_handler)
        assert '"description": agent_cfg.description' in src
        assert '"triggers": agent_cfg.triggers' in src


class TestTheSpawnEndpointActuallyDelegates:
    """The whole delegation path, asserted at the point it can silently fail.

    Every link here was individually correct while the feature was a complete
    no-op: the schema declared `crew`, the bindings resolved, `spawn` accepted a
    `memory_store` — and `api_spawn`'s validation dict, which is CLOSED, simply
    never listed the field, so `cleaned.get("crew")` was always None and the whole
    resolution block below it was unreachable. The MCP handler dropped it the same
    way. Neither failed; both quietly ran the work against the operator's own
    memory.

    So these assert on what the MANAGER RECEIVED, which is the only place the
    chain's weakest link shows up.
    """

    @staticmethod
    def _spawn_call(body: dict, cfg_payload: dict, tmp_path, monkeypatch):
        """Drive `api_spawn` with *body* and return the kwargs `spawn` got."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(json.dumps(cfg_payload))
        from kiro_crew.config import loader as loader_mod

        loader_mod._invalidate_config_cache()
        _materialize_private_stores(loader_mod.KiroCrewConfig.load())
        cov = pytest.importorskip("test_handlers_messaging_coverage")
        mgr = cov._mgr()
        mgr.spawn.return_value = cov._info()
        resp = cov._run(cov.mod.api_spawn, cov._Req(cov._state(subagents=mgr), body))
        loader_mod._invalidate_config_cache()
        return resp, mgr

    _CFG = {
        "memory_stores": {
            "coding": {"owner_member": "coding", "memory_version": 2},
            "email": {"owner_member": "email", "memory_version": 2},
        },
        "default_agent": "kirocrew",
        "agents": {
            "kirocrew": {"kiro_agent": "kirocrew"},
            "coding": {"memory_store": "coding", "kiro_agent": "kirocrew", "triggers": "fix"},
            "email": {"memory_store": "email", "kiro_agent": "kirocrew", "triggers": "draft"},
        },
    }

    def test_naming_a_crew_spawns_into_that_crews_store(self, tmp_path, monkeypatch):
        resp, mgr = self._spawn_call(
            {"task": "fix the bug", "crew": "coding"}, self._CFG, tmp_path, monkeypatch
        )
        assert resp.status == 200, resp
        mgr.spawn.assert_called_once()
        assert mgr.spawn.call_args.kwargs["memory_store"] == "coding"

    def test_a_different_crew_gets_a_different_store(self, tmp_path, monkeypatch):
        _resp, mgr = self._spawn_call(
            {"task": "draft a reply", "crew": "email"}, self._CFG, tmp_path, monkeypatch
        )
        assert mgr.spawn.call_args.kwargs["memory_store"] == "email"

    def test_omitting_crew_leaves_the_store_unnamed(self, tmp_path, monkeypatch):
        """Unchanged behaviour for every caller that does not delegate."""
        _resp, mgr = self._spawn_call({"task": "just do it"}, self._CFG, tmp_path, monkeypatch)
        assert mgr.spawn.call_args.kwargs["memory_store"] == ""

    def test_an_unknown_crew_is_refused_and_never_spawns(self, tmp_path, monkeypatch):
        """Refused, NOT degraded. Everywhere else an unresolvable store falls back
        to the global one and that is the safe direction; here the caller's whole
        reason for naming a crew is to keep the task inside it."""
        resp, mgr = self._spawn_call(
            {"task": "x", "crew": "ghost"}, self._CFG, tmp_path, monkeypatch
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "unknown_crew"
        mgr.spawn.assert_not_called()

    def test_the_mcp_tool_forwards_the_field_it_accepts(self):
        """A schema field the handler drops is a documented no-op. `select_crew`,
        `route_crew` and the conductor skill all instruct `spawn_run(crew=...)`,
        so the handler reading it is the contract."""
        import inspect

        from kiro_crew.mcp_tools import spawn as spawn_tool

        src = inspect.getsource(spawn_tool)
        assert 'args.get("crew")' in src, "the handler must READ the field"
        assert 'body["crew"] = crew' in src, "and FORWARD it to the endpoint"

    def test_no_guidance_still_tells_the_model_to_use_agent_for_a_crew(self):
        """`spawn_run(agent=<crew>)` is accepted and runs on the DEFAULT store, so
        guidance saying so is a leak the docs teach. The conductor skill is the
        copy that matters most: it is `always: true`, loaded into every session."""
        from pathlib import Path

        import kiro_crew

        root = Path(kiro_crew.__file__).parent
        for rel in ("conductor_skill.py", "mcp_tools/control.py"):
            text = (root / rel).read_text()
            assert 'spawn_run(agent="<name>"' not in text, rel
            assert "spawn_run(agent=<crew>" not in text, rel


class TestASymlinkCannotAliasTwoCrewsOntoOneSilo:
    """The containment re-check is IDENTITY, not "somewhere under the root".

    Parent equality after ``.resolve()`` refuses a link that escapes the stores
    root and ACCEPTS one that redirects inside it. With ``memory_stores/acme``
    pointing at ``memory_stores/finance`` the resolved parent is still the root,
    so both crews were handed one silo -- vector rows, markdown, FTS index and
    lessons -- with every path check reporting success and nothing logged.

    The agent's own file tools cannot plant the link (the keystone fence covers
    the subtree), so the reachable authors are a restored backup, an archive
    extraction, the onboarding importer, or a hand-edit. That is a narrow door,
    not a closed one, and "isolated unless someone restored a backup" is not the
    guarantee a per-crew silo makes.
    """

    @pytest.fixture
    def aliased(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(
            json.dumps({"memory_stores": {"acme": {}, "finance": {}}})
        )
        from kiro_crew.config import loader as loader_mod
        from kiro_crew.memory_stores import ensure_memory_store_dir
        from kiro_crew.platform_compat import symlink_or_junction

        loader_mod._invalidate_config_cache()
        finance = ensure_memory_store_dir("finance")
        symlink_or_junction(finance, finance.parent / "acme")
        yield finance
        loader_mod._invalidate_config_cache()

    @pytest.mark.parametrize(
        "resolver",
        (
            "resolve_store_path",
            "memory_store_dir_for",
            "memory_index_path_for",
            "ensure_memory_store_dir",
        ),
    )
    def test_every_resolver_refuses_the_alias(self, aliased, resolver):
        from kiro_crew import memory_stores as ms

        with pytest.raises(ms.UnknownMemoryStore):
            getattr(ms, resolver)("acme")

    def test_the_lesson_store_refuses_it_too(self, aliased):
        """Its owner carve-out used the same parent equality, so it accepted the
        alias and would have appended one crew's corrections to another's file."""
        from kiro_crew.learn import _is_owned_store_root

        assert _is_owned_store_root(aliased.parent / "acme") is False
        assert _is_owned_store_root(aliased) is True, "the real store must still be owned"

    def test_the_legitimate_store_is_unaffected(self, aliased):
        """The check must not refuse a root reached through a symlinked ANCESTOR
        (``/tmp`` is one on macOS) -- the root is resolved before the join."""
        from kiro_crew.memory_stores import resolve_store_path

        assert resolve_store_path("finance") == aliased / "memory.db"

    def test_a_store_that_does_not_exist_yet_still_resolves(self, tmp_path, monkeypatch):
        """``strict=False`` resolution returns the composed path, so a store being
        created for the first time is identical to itself and passes."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(json.dumps({"memory_stores": {"fresh": {}}}))
        from kiro_crew.config import loader as loader_mod
        from kiro_crew.memory_stores import resolve_store_path

        loader_mod._invalidate_config_cache()
        try:
            assert resolve_store_path("fresh").name == "memory.db"
        finally:
            loader_mod._invalidate_config_cache()
