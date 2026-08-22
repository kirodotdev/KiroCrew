"""Crew-variable ``{{name}}`` expansion at the three backend boundaries.

Boundaries covered here:

* the agent system prompt (``context.ContextBuilder.build_message``), for a
  built-in AND a custom agent;
* a cron job's message, at DISPATCH time only
  (``cron.build_cron_session_context``);
* a monitor-loop nudge body
  (``dashboard.handlers.autonudge.render_nudge_message``).

And the two negatives that carry the security argument: a variable named after a
reserved prompt token cannot change that token's substituted value, and a
steering file — project-scoped content that can arrive from a cloned repo — is
left byte-identical.

Hermetic: every case builds config objects in memory, patches
``KiroCrewConfig.load``, and keeps all filesystem state under ``tmp_path``. No
absolute path literal is used, so nothing anchors to a drive Windows CI does not
have.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import patch

import pytest

from kiro_crew import context as ctx_mod
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    coerce_variables,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.cron import CronJob, build_cron_session_context
from kiro_crew.dashboard.handlers.autonudge import render_nudge_message
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.variables import RESERVED_TOKENS, validate_pair

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    global_vars: dict[str, str] | None = None,
    crew_vars: dict[str, str] | None = None,
    crew: str = "mycrew",
) -> KiroCrewConfig:
    """A config carrying one crew and one workspace, plus variable layers."""
    cfg = KiroCrewConfig()
    cfg.variables = dict(global_vars or {})
    cfg.workspaces = {"default": WorkspaceConfig(dir="workspace")}
    cfg.default_workspace = "default"
    cfg.agents = {
        crew: KiroCrewAgentConfig(
            kiro_agent="kirocrew",
            workspace="default",
            variables=dict(crew_vars or {}),
        )
    }
    cfg.default_agent = crew
    return cfg


def _patch_config(monkeypatch: pytest.MonkeyPatch, cfg: KiroCrewConfig) -> None:
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls: cfg))


def _builder(tmp_path) -> ContextBuilder:
    """A ContextBuilder whose every store lives under *tmp_path*."""
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
        bot_name="Kiro",
    )


def _write_builtin_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path, body: str) -> None:
    """Point the built-in prompt loader at a prompt file under *tmp_path*."""
    p = tmp_path / "prompt.md"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setattr(ctx_mod, "_prompt_path", lambda **_kw: p)


def _write_custom_agent(monkeypatch: pytest.MonkeyPatch, tmp_path, name: str, body: str) -> None:
    """Register a custom agent whose inline prompt is *body*."""
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.json").write_text(
        json.dumps({"name": name, "prompt": body}), encoding="utf-8"
    )
    monkeypatch.setattr(ctx_mod, "kiro_agents_dir", lambda: agents)


# ---------------------------------------------------------------------------
# Boundary 1 — agent system prompt
# ---------------------------------------------------------------------------


class TestSystemPromptExpansion:
    def test_builtin_agent_prompt_expands(self, tmp_path, monkeypatch):
        """The built-in prompt file's {{name}} tokens resolve from config."""
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        _write_builtin_prompt(monkeypatch, tmp_path, "Health check: {{baseUrl}}/health")

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True)

        assert "Health check: https://ops.example/health" in msg
        assert "{{baseUrl}}" not in msg

    def test_custom_agent_prompt_expands(self, tmp_path, monkeypatch):
        """A CUSTOM agent's prompt is covered too.

        The custom branch loads via ``_load_agent_prompt`` and would bypass any
        expansion placed inside ``_resolve_prompt_templates``; the call site is
        where both branches converge, which is what this pins.
        """
        _patch_config(
            monkeypatch,
            _config(crew="mycrew", crew_vars={"baseUrl": "https://crew.example"}),
        )
        _write_custom_agent(monkeypatch, tmp_path, "mycrew", "Deploy target: {{baseUrl}}")

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True, agent="mycrew")

        assert "Deploy target: https://crew.example" in msg

    def test_crew_layer_wins_in_prompt(self, tmp_path, monkeypatch):
        """The session's own crew layer overrides global for the same key."""
        _patch_config(
            monkeypatch,
            _config(
                crew="mycrew",
                global_vars={"env": "prod"},
                crew_vars={"env": "staging"},
            ),
        )
        _write_custom_agent(monkeypatch, tmp_path, "mycrew", "Env is {{env}}.")

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True, agent="mycrew")

        assert "Env is staging." in msg

    def test_unknown_token_left_byte_identical(self, tmp_path, monkeypatch):
        """An undefined name survives rather than blanking the sentence."""
        _patch_config(monkeypatch, _config(global_vars={"other": "x"}))
        _write_builtin_prompt(monkeypatch, tmp_path, "curl {{baseUrl}}/health")

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True)

        assert "curl {{baseUrl}}/health" in msg


class TestReservedTokenCannotBeShadowed:
    def test_reserved_name_refused_by_grammar(self):
        """A variable may not be NAMED after a built-in token."""
        for name in sorted(RESERVED_TOKENS):
            key, reason = validate_pair(name, "9999")
            assert key is None, name
            assert "reserved" in reason
        assert coerce_variables({"MAX_SUBAGENTS": "9999", "ok": "v"}, "global") == {"ok": "v"}

    def test_reserved_token_value_unchanged_even_if_present(self, tmp_path, monkeypatch):
        """Ordering, not just the grammar, protects the built-in tokens.

        A hand-edited config that smuggles a reserved name past validation still
        cannot change what ``{{MAX_SUBAGENTS}}`` resolves to: expansion runs
        AFTER every built-in pass, so by then the token is already gone.
        """
        cfg = _config(global_vars={"MAX_SUBAGENTS": "9999", "VERBOSITY_BLOCK": "PWNED"})
        _patch_config(monkeypatch, cfg)
        monkeypatch.setattr(
            "kiro_crew.subagent.resolve_max_subagents", lambda _cfg: 7, raising=True
        )
        _write_builtin_prompt(
            monkeypatch, tmp_path, "cap={{MAX_SUBAGENTS}}\n{{VERBOSITY_BLOCK}}\nend"
        )

        msg, _ = _builder(tmp_path).build_message("hi", is_new_session=True)

        assert "cap=7" in msg
        assert "9999" not in msg
        assert "PWNED" not in msg


class TestSteeringNotExpanded:
    def test_steering_token_left_byte_identical(self, tmp_path, monkeypatch):
        """Steering content is project-scoped and must never be expanded.

        ``_load_steering_resources`` reads ``file://`` resources that can come
        from a cloned repo, so its ``{{token}}`` text stays verbatim while the
        agent prompt in the SAME message expands.
        """
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        _write_builtin_prompt(monkeypatch, tmp_path, "Prompt target {{baseUrl}}")
        monkeypatch.setattr(
            ctx_mod,
            "_load_steering_resources",
            lambda: "[STEERING]\ncurl {{baseUrl}}/admin\n",
        )

        msg, _ = _builder(tmp_path).build_message(
            "hi", is_new_session=True, provider_type="claude_code"
        )

        # The prompt expanded...
        assert "Prompt target https://ops.example" in msg
        # ...and the steering body did not.
        assert "curl {{baseUrl}}/admin" in msg


# ---------------------------------------------------------------------------
# Boundary 2 — cron dispatch
# ---------------------------------------------------------------------------


def _job(**kw) -> CronJob:
    base = {"id": "j1", "name": "nightly", "message": "Poll {{baseUrl}}/status"}
    base.update(kw)
    return CronJob(**base)  # type: ignore[arg-type]


class TestSequenceMembersExpandWithTheirOwnCrew:
    """``agent_sequence`` takes precedence over ``agent_id`` at dispatch, so
    expanding once from ``agent_id`` served every member the wrong crew's values —
    and the DEFAULT crew's for a job that sets only a sequence."""

    def test_the_override_selects_that_members_crew(self, monkeypatch):
        cfg = _config(crew="alpha", crew_vars={"who": "alpha-crew"})
        cfg.agents["beta"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="default", variables={"who": "beta-crew"}
        )
        _patch_config(monkeypatch, cfg)
        job = _job(message="run as {{who}}", agent_id="alpha")

        _key, alpha = build_cron_session_context(job, "alpha")
        _key, beta = build_cron_session_context(job, "beta")

        assert "run as alpha-crew" in alpha
        assert "run as beta-crew" in beta

    def test_the_override_keeps_the_previous_run_carry_over(self, monkeypatch):
        """The regression this pins: expanding per member by calling the bare
        expander skipped the ``last_result`` prepend that only this function does,
        so from the second run on every sequence member lost the prior-run context
        and the do-not-repeat instruction.
        """
        cfg = _config(crew="alpha", crew_vars={"who": "alpha-crew"})
        _patch_config(monkeypatch, cfg)
        job = _job(message="run as {{who}}", agent_id="alpha")
        job.persistent_session = True
        job.last_result = "PRIOR OUTPUT"

        _key, msg = build_cron_session_context(job, "alpha")

        assert "PRIOR OUTPUT" in msg, "the previous-run carry-over was dropped"
        assert "do NOT repeat the same content" in msg
        assert "run as alpha-crew" in msg
        # The carry-over is prepended AFTER expansion, so a token inside a previous
        # run's model output is never scanned.
        assert msg.index("PRIOR OUTPUT") < msg.index("run as alpha-crew")

    def test_a_previous_result_holding_a_token_is_not_expanded(self, monkeypatch):
        cfg = _config(crew="alpha", crew_vars={"who": "alpha-crew"})
        _patch_config(monkeypatch, cfg)
        job = _job(message="go", agent_id="alpha")
        job.persistent_session = True
        job.last_result = "the model wrote {{who}} last time"

        _key, msg = build_cron_session_context(job, "alpha")

        assert "{{who}}" in msg, "a token in a previous run's output was expanded"

    def test_expands_at_dispatch_and_stored_message_keeps_token(self, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job()

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Poll https://ops.example/status"
        # The STORE is untouched: the literal token is what gets persisted.
        assert job.message == "Poll {{baseUrl}}/status"

    def test_editing_a_variable_changes_the_next_dispatch(self, monkeypatch):
        cfg = _config(global_vars={"baseUrl": "https://old.example"})
        _patch_config(monkeypatch, cfg)
        job = _job()

        _k1, first = build_cron_session_context(job)
        assert first == "Poll https://old.example/status"

        # The user edits the variable — no job rewrite anywhere.
        cfg.variables["baseUrl"] = "https://new.example"

        _k2, second = build_cron_session_context(job)
        assert second == "Poll https://new.example/status"
        assert job.message == "Poll {{baseUrl}}/status"

    def test_uses_the_jobs_own_crew(self, monkeypatch):
        _patch_config(
            monkeypatch,
            _config(
                crew="mycrew",
                global_vars={"env": "prod"},
                crew_vars={"env": "staging"},
            ),
        )
        job = _job(message="Env {{env}}", agent_id="mycrew")

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Env staging"

    def test_stateless_job_expands_too(self, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job(persistent_session=False)

        key, prompt = build_cron_session_context(job)

        assert key.startswith("cron:j1:")
        assert prompt == "Poll https://ops.example/status"

    def test_previous_run_result_is_not_expanded(self, monkeypatch):
        """``last_result`` is a prior run's MODEL OUTPUT, not user-authored."""
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job(last_result="earlier I emitted {{baseUrl}} verbatim")

        _key, prompt = build_cron_session_context(job)

        assert "earlier I emitted {{baseUrl}} verbatim" in prompt
        assert "Poll https://ops.example/status" in prompt

    def test_no_variables_configured_is_a_passthrough(self, monkeypatch):
        _patch_config(monkeypatch, _config())
        job = _job()

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Poll {{baseUrl}}/status"


# ---------------------------------------------------------------------------
# Boundary 3 — monitor loop nudge
# ---------------------------------------------------------------------------


class TestNudgeExpansion:
    def test_variable_expands_and_stop_file_still_resolves(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"prNum": "4161"}))
        sentinel = tmp_path / ".stop-loop"

        out = render_nudge_message("Check PR {{prNum}}; halt via {{STOP_FILE}}", str(sentinel))

        assert f"Check PR 4161; halt via {sentinel}" == out

    def test_stop_file_resolves_when_no_variables_configured(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch, _config())
        sentinel = tmp_path / ".stop-loop"

        assert render_nudge_message("halt: {{STOP_FILE}}", str(sentinel)) == f"halt: {sentinel}"
        assert render_nudge_message("halt: {{STOP_FILE}}", None) == "halt: "

    def test_variable_cannot_forge_the_sentinel_path(self, tmp_path, monkeypatch):
        """A value containing ``{{STOP_FILE}}`` resolves to the REAL sentinel.

        Expansion is single-pass, so the inserted text is not rescanned as a
        variable; the gateway's own ``{{STOP_FILE}}`` replace then runs last and
        can only produce the sentinel it was given.
        """
        cfg = _config(global_vars={"note": "or touch {{STOP_FILE}} to bail"})
        _patch_config(monkeypatch, cfg)
        sentinel = tmp_path / ".stop-loop"
        attacker = tmp_path / "attacker-chosen"

        out = render_nudge_message("Keep going ({{note}})", str(sentinel))

        assert str(sentinel) in out
        assert str(attacker) not in out

    def test_crew_scope_used_when_agent_named(self, tmp_path, monkeypatch):
        _patch_config(
            monkeypatch,
            _config(crew="mycrew", global_vars={"env": "prod"}, crew_vars={"env": "staging"}),
        )
        sentinel = tmp_path / ".stop-loop"

        out = render_nudge_message("Env {{env}} — {{STOP_FILE}}", str(sentinel), "mycrew")

        assert out == f"Env staging — {sentinel}"


class TestTheSystemPromptUsesTheCrewNotTheRuntimeName:
    """``agent`` and ``crew`` are different identities and only one can resolve
    variables.

    The dashboard turn path passes the resolved KIRO AGENT name in ``agent``
    ("kirocrew") because build_message's is_custom check needs the runtime name.
    That name is not a key in ``config.agents``, and resolve_variables falls back
    to the default crew for an unknown name BY DESIGN — so routing it through
    ``agent`` made every non-default crew silently receive the default crew's
    values, with no error anywhere. These tests pin the two identities apart.
    """

    def _cfg(self):
        cfg = KiroCrewConfig()
        cfg.variables = {"scope": "global"}
        cfg.workspaces = {"default": WorkspaceConfig(dir="workspace")}
        cfg.default_workspace = "default"
        cfg.agents = {
            "default": KiroCrewAgentConfig(
                kiro_agent="kirocrew", workspace="default", variables={"scope": "default-crew"}
            ),
            # The crew under test: its NAME differs from the kiro agent it runs on,
            # which is the whole point — "kirocrew" cannot identify it.
            "oncall": KiroCrewAgentConfig(
                kiro_agent="kirocrew", workspace="default", variables={"scope": "oncall-crew"}
            ),
        }
        cfg.default_agent = "default"
        return cfg

    def test_the_crew_alias_selects_that_crews_values(self):
        cfg = self._cfg()
        with patch.object(KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            out = ContextBuilder._expand_crew_variables("scope={{scope}}", "oncall")
        assert out == "scope=oncall-crew"

    def test_the_runtime_name_would_have_selected_the_default_crew(self):
        """Why the separate parameter exists, asserted directly rather than
        described: the runtime name silently yields the wrong crew's value."""
        cfg = self._cfg()
        with patch.object(KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            out = ContextBuilder._expand_crew_variables("scope={{scope}}", "kirocrew")
        assert out == "scope=default-crew", (
            "a kiro-agent runtime name must not resolve to a real crew; it falls "
            "back to the default, which is exactly why build_message takes crew="
        )

    def test_the_dashboard_turn_passes_the_alias_not_the_runtime_name(self):
        """A source guard on the one call site that carries both identities. The
        bug was invisible in behaviour because the fallback is silent, so the
        wiring itself is what needs pinning."""
        from kiro_crew.dashboard import chat_runner

        source = inspect.getsource(chat_runner)
        assert "crew=crew_alias or None" in source
        # And the overloaded argument must still carry the runtime name, since
        # build_message's is_custom check depends on it.
        assert "agent=kiro_agent or slot.agent or None" in source

    def test_build_message_accepts_crew_as_keyword_only(self):
        params = inspect.signature(ContextBuilder.build_message).parameters
        assert params["crew"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["crew"].default is None

    def test_a_caller_passing_only_agent_still_resolves_its_own_crew(self):
        """The OTHER half of the fix, and the direction that is easy to break.

        Slack, Discord, Telegram, cron and the channel dispatcher all pass a real
        crew alias in ``agent`` and no ``crew``. Resolving from ``crew`` alone would
        invert the original bug — those paths would start serving the default crew.
        """
        source = inspect.getsource(ContextBuilder.build_message)
        assert "crew if crew is not None else agent" in source

    def test_the_fallback_is_exercised_for_both_shapes(self, tmp_path, monkeypatch):
        """Two DISTINCT shapes, which an earlier version only claimed: it labelled A
        and B differently and then ran the byte-identical call twice, so the
        dashboard's explicit-crew path was never exercised here at all.
        """
        cfg = self._cfg()
        with patch.object(KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            # Shape A: a crew alias arriving as `agent` (every channel transport).
            assert ContextBuilder._expand_crew_variables("{{scope}}", "oncall") == "oncall-crew"
            # Shape C: nothing identifiable -> the default crew, explicitly.
            assert ContextBuilder._expand_crew_variables("{{scope}}", None) == "default-crew"

        # Shape B: the dashboard's form — a kiro-agent RUNTIME name in `agent` and
        # the crew alias in `crew`. Driven through build_message so the argument
        # that actually resolves variables is the one under test.
        _patch_config(monkeypatch, cfg)
        _write_builtin_prompt(monkeypatch, tmp_path, "scope={{scope}}")
        builder = _builder(tmp_path)
        built, _ = builder.build_message(
            "hello", True, "sess-shape-b", agent="kirocrew", crew="oncall"
        )
        assert (
            "scope=oncall-crew" in built
        ), "build_message resolved the runtime name instead of the crew alias"
