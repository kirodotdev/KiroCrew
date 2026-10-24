"""Tests for the agent prompt ``ContextBuilder`` reads for a custom agent.

This module pins ``_load_agent_prompt`` (a null or missing ``prompt``, the managed
contract a fork or template copy carries) and the ``[AGENT SYSTEM PROMPT]`` block for
a custom persona that kiro-cli or KAS delivers itself: sent once where the harness
loaded the same text, kept for every shape that cannot be shown identical, and judged
by the same rule after a compaction. The core turn shape lives in ``test_context.py``.
"""

from __future__ import annotations

import pytest

from conftest import make_dir_link
from kiro_crew.acp.types import ACP_BACKENDS_KNOWN, ACP_BACKENDS_NATIVE_SPEC_PROMPT
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

# Each context-builder test file keeps this xdist group (see ``test_context_prompt_text.py``).
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_context")


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test here builds a ``ContextBuilder``: close its ``SkillsLoader`` (``test/conftest.py``)."""


class TestLoadAgentPrompt:
    """Tests for _load_agent_prompt handling of null/missing prompt values."""

    def test_null_prompt_returns_empty(self, tmp_path, monkeypatch):
        """Agent JSON with "prompt": null should return empty string."""
        import json

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "test.json").write_text(
            json.dumps({"name": "test", "prompt": None}), encoding="utf-8"
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert ContextBuilder._load_agent_prompt("test") == ""

    def test_missing_prompt_returns_empty(self, tmp_path, monkeypatch):
        """Agent JSON without "prompt" key should return empty string."""
        import json

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "test.json").write_text(json.dumps({"name": "test"}), encoding="utf-8")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert ContextBuilder._load_agent_prompt("test") == ""

    @staticmethod
    def _write_spec(tmp_path, monkeypatch, prompt: str) -> None:
        import json

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "test.json").write_text(
            json.dumps({"name": "test", "prompt": prompt}), encoding="utf-8"
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents_dir)

    @staticmethod
    def _managed_contract(tmp_path, monkeypatch):
        from kiro_crew import agent

        package = tmp_path / "installed-package" / "config"
        package.mkdir(parents=True)
        contract = package / "prompt.md"
        contract.write_text("RESOLVED_CONTRACT", encoding="utf-8")
        monkeypatch.setattr(agent, "_BUNDLED_CFG_DIR", package)
        monkeypatch.setattr(agent, "_project_dir", lambda: None)
        assert agent._prompt_path() == contract
        return contract

    @pytest.mark.parametrize("owner_template", ["", "test"], ids=["fork-or-copy", "owner"])
    def test_managed_stub_resolves_to_contract(self, tmp_path, monkeypatch, owner_template):
        """The stub is the managed contract whatever spec carries it: a fork or
        template copy inherits it verbatim and must not receive the stub TEXT as
        its persona."""
        from kiro_crew import agent

        self._managed_contract(tmp_path, monkeypatch)
        self._write_spec(tmp_path, monkeypatch, agent._NATIVE_PROMPT_STUB)
        loaded = ContextBuilder._load_agent_prompt("test", owner_template=owner_template)
        assert loaded == "RESOLVED_CONTRACT"

    @pytest.mark.parametrize("owner_template", ["", "test"], ids=["fork-or-copy", "owner"])
    def test_managed_pointer_resolves_to_contract(self, tmp_path, monkeypatch, owner_template):
        contract = self._managed_contract(tmp_path, monkeypatch)
        self._write_spec(tmp_path, monkeypatch, f"file://{contract}")
        loaded = ContextBuilder._load_agent_prompt("test", owner_template=owner_template)
        assert loaded == "RESOLVED_CONTRACT"

    def test_owner_template_custom_prompt_omitted(self, tmp_path, monkeypatch):
        """An owner template's own (non-managed) prompt reaches the model through
        member essentials, so the session-start load omits it."""
        self._managed_contract(tmp_path, monkeypatch)
        self._write_spec(tmp_path, monkeypatch, "You are a bespoke reviewer.")
        assert ContextBuilder._load_agent_prompt("test", owner_template="test") == ""
        assert ContextBuilder._load_agent_prompt("test") == "You are a bespoke reviewer."

    def test_template_copy_with_stub_delivers_contract_once_at_session_start(
        self, tmp_path, monkeypatch
    ):
        """End to end: a plain (non-member) session on a template copy of the
        managed default, whose spec inherited the stub verbatim, gets the resolved
        contract as its [AGENT SYSTEM PROMPT] exactly once, and never the stub text."""
        from kiro_crew import agent

        self._managed_contract(tmp_path, monkeypatch)
        self._write_spec(tmp_path, monkeypatch, agent._NATIVE_PROMPT_STUB)
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        msg, _ = builder.build_message("hello", is_new_session=True, agent="test")
        assert msg.count("RESOLVED_CONTRACT") == 1
        assert "[AGENT SYSTEM PROMPT]\nRESOLVED_CONTRACT\n[END AGENT SYSTEM PROMPT]" in msg
        assert "follow it as your authoritative contract" not in msg

    def test_custom_agent_gets_own_prompt_under_claude_code(self, tmp_path, monkeypatch):
        """Under claude_code a custom agent without a private owner gets its own
        prompt as its [AGENT SYSTEM PROMPT], not the Kiro Crew persona."""
        self._managed_contract(tmp_path, monkeypatch)
        self._write_spec(tmp_path, monkeypatch, "You are a bespoke reviewer.")
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        msg, _ = builder.build_message(
            "hello", is_new_session=True, agent="test", provider_type="claude_code"
        )
        assert (
            "[AGENT SYSTEM PROMPT]\nYou are a bespoke reviewer.\n[END AGENT SYSTEM PROMPT]" in msg
        )
        assert "RESOLVED_CONTRACT" not in msg

    @staticmethod
    def _builder(tmp_path):
        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )

    @staticmethod
    def _app_persona(tmp_path, text: str) -> str:
        """An app-materialized spec names its persona by an absolute ``file://`` URI."""
        persona = tmp_path / ".kiro" / "crew" / "apps" / "demo" / "data" / "persona.md"
        persona.parent.mkdir(parents=True, exist_ok=True)
        persona.write_text(text, encoding="utf-8")
        return f"file://{persona}"

    @staticmethod
    def _kiro_cli_native_prompt(spec_prompt: str) -> str:
        """The persona kiro-cli itself puts in front of the model for a spec ``prompt``.

        kiro-cli reads the field off disk at spawn: inline text verbatim, or the
        contents of the file a ``file://`` value names. context.py can neither see
        nor change that copy, so it counts toward "delivered once".
        """
        from pathlib import Path

        if spec_prompt.startswith("file://"):
            return Path(spec_prompt[len("file://") :]).read_text(encoding="utf-8")
        return spec_prompt

    @staticmethod
    def _snapshot(agent: str, text: str) -> dict[str, str]:
        """What the runtime records at activation: the persona the harness loaded."""
        return {f"template://{agent}#prompt": text}

    @staticmethod
    def _live_provider(
        *,
        documents: dict[str, str] | None = None,
        across_compaction: bool = False,
    ):
        """A real ``LLMProvider`` subtype, so ``context_provider_of`` admits it.

        *documents* is the activation snapshot its session handle recorded of
        the copy the harness loaded; the runtime writes it only on a harness
        that puts a custom spec's own prompt in front of the model itself, so
        an empty map stands for a harness that never did. *across_compaction*
        is the second fact, read only after a compaction: whether the harness
        is proven to keep that prompt through its own summarization. It
        defaults off, as the base provider does, because a wrong True drops the
        persona for the rest of the session.
        """
        from kiro_crew.providers.base import LLMProvider

        class _Live(LLMProvider):
            @property
            def native_spec_prompt_across_compaction(self) -> bool:
                return across_compaction

            @property
            def native_context_documents(self) -> dict[str, str]:
                return dict(documents or {})

            async def start(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

            async def stream(self, message, *, allow_image=True):
                return
                yield

            async def approve_tool(self, request_id, *, always=False) -> bool:
                return True

            async def reject_tool(self, request_id) -> None:
                return None

            def context_usage_pct(self) -> float:
                return 0.0

        return _Live()

    @pytest.mark.parametrize("shape", ["inline", "app-file"])
    def test_custom_persona_reaches_kiro_cli_once(self, tmp_path, monkeypatch, shape):
        """kiro-cli already delivers a spec's own persona natively, whether the
        spec carries it inline or names an app's persona file, so the
        session-start injection must not deliver the same persona again."""
        self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        spec_prompt = persona if shape == "inline" else self._app_persona(tmp_path, persona)
        self._write_spec(tmp_path, monkeypatch, spec_prompt)
        native = self._kiro_cli_native_prompt(spec_prompt)
        msg, _ = self._builder(tmp_path).build_message(
            "hello",
            is_new_session=True,
            agent="test",
            provider_type="acp",
            context_provider=self._live_provider(documents=self._snapshot("test", native)),
        )
        assert native.count(persona) + msg.count(persona) == 1
        assert "RESOLVED_CONTRACT" not in msg

    @pytest.mark.parametrize(
        "case",
        [
            "no-provider",
            "no-snapshot",
            "edited-after-activation",
            "placeholder",
            "relative-file",
            "home-file",
            "project-spec",
        ],
    )
    def test_custom_persona_block_kept_unless_shown_identical(self, tmp_path, monkeypatch, case):
        """The block is withheld only when it would repeat, word for word, the
        copy the harness loaded at activation; every other shape keeps it, so no
        persona is dropped on a guess. The dedup-defeating shapes are given the
        snapshot anyway, so it is the shape that keeps the block, not its absence."""
        import json

        self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        spec_prompt = persona
        expected = persona
        project = None
        snapshot: dict[str, str] | None = self._snapshot("test", persona)
        if case == "no-snapshot":
            # The handle recorded nothing: a harness that reads no kiro spec never
            # gets a copy recorded (the runtime writes one only on a harness that
            # loads the spec itself), and a read failure or a shape the dedup
            # rejects records nothing either. Without evidence of the harness's
            # copy the block stays.
            snapshot = None
        elif case == "edited-after-activation":
            # kiro-cli loaded the earlier text; the file now says something else,
            # so the block carries the CURRENT text rather than being dropped.
            snapshot = self._snapshot("test", "You are the reviewer this session started with.")
        elif case == "placeholder":
            spec_prompt = "You are {bot_name}, a bespoke reviewer."
            snapshot = self._snapshot("test", spec_prompt)
            expected = None  # resolved: the raw native copy still holds the token
        elif case == "relative-file":
            # A relative ``file://`` resolves against the spec's own directory.
            prompts = tmp_path / ".kiro" / "agents" / "prompts"
            prompts.mkdir(parents=True)
            (prompts / "persona.md").write_text(persona, encoding="utf-8")
            spec_prompt = "file://prompts/persona.md"
        elif case == "home-file":
            # Crew expands ``~``; kiro-cli documents no such expansion for ``prompt``.
            # Pin the home for both expanders: POSIX reads HOME, Windows USERPROFILE.
            monkeypatch.setenv("HOME", str(tmp_path))
            monkeypatch.setenv("USERPROFILE", str(tmp_path))
            (tmp_path / "prompts").mkdir()
            (tmp_path / "prompts" / "persona.md").write_text(persona, encoding="utf-8")
            spec_prompt = "file://~/prompts/persona.md"
        self._write_spec(tmp_path, monkeypatch, spec_prompt)
        if case == "project-spec":
            agents = tmp_path / "proj" / ".kiro" / "agents"
            agents.mkdir(parents=True)
            (agents / "test.json").write_text(
                json.dumps({"name": "test", "prompt": "You are the project reviewer."}),
                encoding="utf-8",
            )
            project = str(tmp_path / "proj")
            expected = "You are the project reviewer."
        provider = None if case == "no-provider" else self._live_provider(documents=snapshot)
        msg, _ = self._builder(tmp_path).build_message(
            "hello",
            is_new_session=True,
            agent="test",
            provider_type="acp",
            project=project,
            context_provider=provider,
        )
        block = msg.split("[AGENT SYSTEM PROMPT]\n", 1)[1].split("\n[END AGENT SYSTEM PROMPT]")[0]
        if expected is None:
            assert "bespoke reviewer" in block and "{bot_name}" not in block
        else:
            assert block == expected

    def test_managed_stub_contract_still_injected_on_native_harness(self, tmp_path, monkeypatch):
        """The stub the harness reads points at the block, so the resolved contract
        is injected on kiro-cli too, once -- even when the handle recorded the stub
        text as what the harness loaded."""
        from kiro_crew import agent

        self._managed_contract(tmp_path, monkeypatch)
        self._write_spec(tmp_path, monkeypatch, agent._NATIVE_PROMPT_STUB)
        msg, _ = self._builder(tmp_path).build_message(
            "hello",
            is_new_session=True,
            agent="test",
            context_provider=self._live_provider(
                documents=self._snapshot("test", agent._NATIVE_PROMPT_STUB),
            ),
        )
        assert msg.count("RESOLVED_CONTRACT") == 1
        assert "[AGENT SYSTEM PROMPT]\nRESOLVED_CONTRACT\n[END AGENT SYSTEM PROMPT]" in msg

    def test_app_persona_file_injected_under_claude_code(self, tmp_path, monkeypatch):
        """Claude Code never reads a kiro spec, so the injection is the only
        channel for an app's persona file and must still carry it, once."""
        self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        self._write_spec(tmp_path, monkeypatch, self._app_persona(tmp_path, persona))
        msg, _ = self._builder(tmp_path).build_message(
            "hello", is_new_session=True, agent="test", provider_type="claude_code"
        )
        assert msg.count(persona) == 1
        assert f"[AGENT SYSTEM PROMPT]\n{persona}\n[END AGENT SYSTEM PROMPT]" in msg

    @staticmethod
    def _real_provider(
        tmp_path, backend: str, documents: dict[str, str], agent_version: str = "2.28.0"
    ):
        """A real ``AcpProvider`` bound to a real session handle, as after ``start()``.

        The snapshot travels the production chain -- ``AcpProvider`` ->
        ``AcpSessionProvider`` -> ``AcpSessionHandle.native_context_documents`` --
        so these tests read the provider's real properties, not a fake's.
        *agent_version* stands in for the handshake's ``agentInfo.version``, the
        release the across-compaction floor reads; the default is the floor.
        """
        import asyncio

        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
        from kiro_crew.acp.session_provider import AcpSessionProvider
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(work_dir=tmp_path, acp_backend=backend)
        runtime = AcpRuntime(work_dir=tmp_path, acp_backend=backend)
        runtime._agent_version = agent_version
        handle = AcpSessionHandle("persona", asyncio.Queue(), runtime, watchdog=WatchdogSettings())
        handle.native_context_documents.update(documents)
        provider._client = AcpSessionProvider(handle, runtime, owns_runtime=True)
        assert provider.native_context_documents == documents
        return provider

    @pytest.mark.parametrize("shape", ["inline", "app-file"])
    @pytest.mark.parametrize("harness", ["kiro-cli", "kas"])
    def test_custom_persona_reaches_real_native_harness_once(
        self, tmp_path, monkeypatch, harness, shape
    ):
        """The real provider of each native harness reports the flag, and the copy
        its handle recorded at activation (``kas_agents.resolve_prompt`` on the KAS
        wire; the spec kiro-cli loads) is the text the block would have repeated,
        so the persona arrives once."""
        import json

        from kiro_crew.acp import kas_agents
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

        self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        spec_prompt = persona if shape == "inline" else self._app_persona(tmp_path, persona)
        self._write_spec(tmp_path, monkeypatch, spec_prompt)
        backend = ACP_BACKEND_KAS if harness == "kas" else ACP_BACKEND_KIRO
        if harness == "kas":
            agents = tmp_path / ".kiro" / "agents"
            spec = json.loads((agents / "test.json").read_text(encoding="utf-8"))
            native = kas_agents.resolve_prompt(spec, agent_id="test", agents_dir=agents)
        else:
            native = self._kiro_cli_native_prompt(spec_prompt)
        msg, _ = self._builder(tmp_path).build_message(
            "hello",
            is_new_session=True,
            agent="test",
            provider_type="acp",
            context_provider=self._real_provider(tmp_path, backend, self._snapshot("test", native)),
        )
        assert native == persona
        assert native.count(persona) + msg.count(persona) == 1
        assert "[AGENT SYSTEM PROMPT]\n" not in msg

    @pytest.mark.parametrize("compacted", [False, True], ids=["session-start", "post-compaction"])
    @pytest.mark.parametrize("harness", ["kiro-cli", "kas"])
    def test_spec_edited_after_activation_keeps_block_on_real_harness(
        self, tmp_path, monkeypatch, harness, compacted
    ):
        """The harness holds the copy it loaded at activation; a spec edited since
        differs from it, so the block is sent with the current text -- at
        session start and when a compaction drops it -- rather than withheld on a
        comparison against the file as it reads now."""
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

        self._managed_contract(tmp_path, monkeypatch)
        activated = "You are the reviewer this session started with."
        edited = "You are a bespoke reviewer, revised mid-session."
        self._write_spec(tmp_path, monkeypatch, edited)
        backend = ACP_BACKEND_KAS if harness == "kas" else ACP_BACKEND_KIRO
        provider = self._real_provider(tmp_path, backend, self._snapshot("test", activated))
        msg, _ = self._builder(tmp_path).build_message(
            "hello" if not compacted else "carry on",
            is_new_session=not compacted,
            needs_reinjection=compacted,
            agent="test",
            provider_type="acp",
            context_provider=provider,
        )
        assert f"[AGENT SYSTEM PROMPT]\n{edited}\n[END AGENT SYSTEM PROMPT]" in msg
        assert activated not in msg

    @pytest.mark.parametrize("compacted", [False, True], ids=["session-start", "post-compaction"])
    @pytest.mark.parametrize("harness", ["kiro-cli", "kiro-cli-below-floor", "kas"])
    def test_matching_snapshot_withheld_only_where_retention_is_shown(
        self, tmp_path, monkeypatch, harness, compacted
    ):
        """Both native harnesses hold the persona at session start, so the block is
        withheld there on a matching copy. After a compaction the harness's OWN
        summarization decides whether it still holds it: kiro-cli does (shown on a
        live transcript), so the block stays withheld from the release that
        transcript was taken on; below that floor, and for KAS, nothing shows it,
        so the block is sent once, as it is for a harness that never delivered
        the prompt."""
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

        self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        self._write_spec(tmp_path, monkeypatch, persona)
        backend = ACP_BACKEND_KAS if harness == "kas" else ACP_BACKEND_KIRO
        version = "2.27.1" if harness == "kiro-cli-below-floor" else "2.28.0"
        provider = self._real_provider(
            tmp_path, backend, self._snapshot("test", persona), agent_version=version
        )
        assert provider.native_spec_prompt_across_compaction is (harness == "kiro-cli")
        msg, _ = self._builder(tmp_path).build_message(
            "hello" if not compacted else "carry on",
            is_new_session=not compacted,
            needs_reinjection=compacted,
            agent="test",
            provider_type="acp",
            context_provider=provider,
        )
        block = f"[AGENT SYSTEM PROMPT]\n{persona}\n[END AGENT SYSTEM PROMPT]"
        if compacted and harness != "kiro-cli":
            assert msg.count(block) == 1
            assert msg.count(persona) == 1
        else:
            assert "[AGENT SYSTEM PROMPT]\n" not in msg
            assert persona not in msg

    @pytest.mark.parametrize(
        "backend", sorted(ACP_BACKENDS_KNOWN - ACP_BACKENDS_NATIVE_SPEC_PROMPT)
    )
    def test_custom_persona_block_sent_by_every_other_real_harness(
        self, tmp_path, monkeypatch, backend
    ):
        """A harness that reads no kiro spec has the block as its only copy of
        the persona, so it is always sent."""
        from kiro_crew.providers.acp import AcpProvider

        self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        self._write_spec(tmp_path, monkeypatch, self._app_persona(tmp_path, persona))
        msg, _ = self._builder(tmp_path).build_message(
            "hello",
            is_new_session=True,
            agent="test",
            provider_type="acp",
            context_provider=AcpProvider(work_dir=tmp_path, acp_backend=backend),
        )
        assert msg.count(persona) == 1
        assert f"[AGENT SYSTEM PROMPT]\n{persona}\n[END AGENT SYSTEM PROMPT]" in msg

    @pytest.mark.parametrize(
        "case",
        [
            "native-persona",
            "native-unproven-retention",
            "other-harness-persona",
            "native-stub",
            "edited-after-activation",
        ],
    )
    def test_post_compaction_reinjection_applies_the_same_rule(self, tmp_path, monkeypatch, case):
        """A compaction drops the block but not the harness's own system prompt,
        so the reinjection withholds a custom persona exactly where session start
        does -- against the copy loaded at activation -- and still restores the
        managed contract on a native harness.

        Only a harness proven to keep that prompt through its own compaction is
        trusted after one: with no such proof the block is re-sent, as it is on a
        harness that never delivered the prompt itself."""
        from kiro_crew import agent

        self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        stub = case == "native-stub"
        self._write_spec(tmp_path, monkeypatch, agent._NATIVE_PROMPT_STUB if stub else persona)
        activated = (
            "You are the reviewer this session started with."
            if case == "edited-after-activation"
            else persona
        )
        msg, _ = self._builder(tmp_path).build_message(
            "carry on",
            is_new_session=False,
            needs_reinjection=True,
            agent="test",
            provider_type="acp",
            context_provider=self._live_provider(
                # A harness that never delivered the prompt has no copy recorded:
                # the runtime writes one only on a harness that loads the spec.
                documents=(
                    {} if case == "other-harness-persona" else self._snapshot("test", activated)
                ),
                across_compaction=case != "native-unproven-retention",
            ),
        )
        if case == "native-persona":
            assert persona not in msg
            assert "[AGENT SYSTEM PROMPT]\n" not in msg
        elif case in (
            "native-unproven-retention",
            "other-harness-persona",
            "edited-after-activation",
        ):
            assert msg.count(persona) == 1
            assert f"[AGENT SYSTEM PROMPT]\n{persona}\n[END AGENT SYSTEM PROMPT]" in msg
            assert activated == persona or activated not in msg
        else:
            assert msg.count("RESOLVED_CONTRACT") == 1
            assert "[AGENT SYSTEM PROMPT]\nRESOLVED_CONTRACT\n[END AGENT SYSTEM PROMPT]" in msg

    @pytest.mark.parametrize("shape", ["inline", "app-file"])
    def test_activation_snapshot_reads_the_copy_kiro_cli_loads(self, tmp_path, monkeypatch, shape):
        """``native_spec_prompt_copy``, the read the runtime takes around the two
        moments kiro-cli loads a spec (process spawn and ``session/set_mode``,
        never a plain ``session/new``), returns the same text kiro-cli puts in
        front of the model: inline verbatim, or the contents of an absolute
        ``file://``."""
        from kiro_crew.member_essential_context import native_spec_prompt_copy

        self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        spec_prompt = persona if shape == "inline" else self._app_persona(tmp_path, persona)
        self._write_spec(tmp_path, monkeypatch, spec_prompt)
        assert native_spec_prompt_copy("test") == self._kiro_cli_native_prompt(spec_prompt)
        assert ContextBuilder._load_agent_prompt("test") == native_spec_prompt_copy("test")

    def test_activation_snapshot_reads_the_skill_view_alias_kiro_cli_loads(
        self, tmp_path, monkeypatch
    ):
        """kiro-cli is given the skill-view alias at ``--agent`` and ``set_mode``: a
        copy of the spec taken when the view was prepared. With *transport*
        naming that alias, the snapshot is the alias's text -- what kiro-cli holds
        -- even after the source spec was edited, so the builder sends the edited
        persona once rather than withholding it against a copy kiro-cli never
        loaded. The agent's own name reads the source as before; a non-alias
        stand-in and an absent alias file yield nothing, which keeps the block."""
        import json

        from kiro_crew.member_essential_context import native_spec_prompt_copy

        self._managed_contract(tmp_path, monkeypatch)
        copied = "You are the persona the view copied."
        edited = "You are the persona edited after the view was prepared."
        self._write_spec(tmp_path, monkeypatch, copied)
        alias = "kirocrew-skill-view-" + "a" * 24
        (tmp_path / ".kiro" / "agents" / f"{alias}.json").write_text(
            json.dumps({"name": alias, "prompt": copied}), encoding="utf-8"
        )
        self._write_spec(tmp_path, monkeypatch, edited)
        assert native_spec_prompt_copy("test") == edited
        assert native_spec_prompt_copy("test", transport="test") == edited
        assert native_spec_prompt_copy("test", transport=alias) == copied
        assert native_spec_prompt_copy("test", transport="other-agent") is None
        assert native_spec_prompt_copy("test", transport="kirocrew-skill-view-" + "b" * 24) is None
        msg, _ = self._builder(tmp_path).build_message(
            "hello",
            is_new_session=True,
            agent="test",
            provider_type="acp",
            context_provider=self._live_provider(documents=self._snapshot("test", copied)),
        )
        assert msg.count(edited) == 1
        assert f"[AGENT SYSTEM PROMPT]\n{edited}\n[END AGENT SYSTEM PROMPT]" in msg
        assert copied not in msg

    def test_plain_name_snapshot_records_nothing_when_the_checkout_shadows_the_spec(
        self, tmp_path, monkeypatch
    ):
        """With no skill view kiro-cli is given the plain name and resolves it from
        its process cwd before the user level, so a ``.kiro/agents`` spec in that
        checkout declaring the name is the copy it loads -- and not one Crew's
        block can be shown to equal (``native_spec_prompt_shape`` admits the
        user level alone). The read with *work_dir* follows kiro-cli's order:
        when the checkout declares the name (by declared name, not filename) it
        records nothing, so the block is sent; the user level when the checkout
        does not; the alias regardless of the checkout. A checkout spec the
        reader refuses, or a name the checkout declares twice, records nothing
        too. Without the dir the user level is read as before. The builder then
        sends the user-level persona once rather than withholding it against a
        copy kiro-cli never loaded."""
        import json

        from kiro_crew.member_essential_context import native_spec_prompt_copy

        self._managed_contract(tmp_path, monkeypatch)
        user_level = "You are the persona of the user-level spec."
        checkout = "You are the persona the checkout spec declares."
        self._write_spec(tmp_path, monkeypatch, user_level)
        alias = "kirocrew-skill-view-" + "a" * 24
        (tmp_path / ".kiro" / "agents" / f"{alias}.json").write_text(
            json.dumps({"name": alias, "prompt": user_level}), encoding="utf-8"
        )
        project = tmp_path / "checkout"
        project_agents = project / ".kiro" / "agents"
        project_agents.mkdir(parents=True)
        assert native_spec_prompt_copy("test", work_dir=project) == user_level
        (project_agents / "renamed.json").write_text(
            json.dumps({"name": "test", "prompt": checkout}), encoding="utf-8"
        )
        assert native_spec_prompt_copy("test", work_dir=project) is None
        assert native_spec_prompt_copy("test", transport="test", work_dir=str(project)) is None
        assert native_spec_prompt_copy("test", transport=alias, work_dir=project) == user_level
        assert native_spec_prompt_copy("test") == user_level
        (project_agents / "twin.json").write_text(
            json.dumps({"name": "test", "prompt": "a second declaration"}), encoding="utf-8"
        )
        assert native_spec_prompt_copy("test", work_dir=project) is None
        (project_agents / "twin.json").unlink()
        (project_agents / "renamed.json").unlink()
        (project_agents / "test.json").write_text("{not json", encoding="utf-8")
        assert native_spec_prompt_copy("test", work_dir=project) is None
        msg, _ = self._builder(tmp_path).build_message(
            "hello",
            is_new_session=True,
            agent="test",
            provider_type="acp",
            context_provider=self._live_provider(documents={}),
        )
        assert msg.count(user_level) == 1
        assert f"[AGENT SYSTEM PROMPT]\n{user_level}\n[END AGENT SYSTEM PROMPT]" in msg

    @pytest.mark.parametrize(
        "case",
        [
            "managed-stub",
            "managed-pointer",
            "relative-file",
            "home-file",
            "missing-file",
            "sandbox-masked-file",
            "sandbox-masked-link",
            "empty",
            "null",
            "absent-spec",
        ],
    )
    def test_activation_snapshot_records_nothing_for_ineligible_shapes(
        self, tmp_path, monkeypatch, case
    ):
        """Every shape ``_load_agent_prompt`` refuses to call the harness's copy, and
        every read failure, yields no snapshot -- so the block is sent rather than
        withheld on a copy that cannot be shown. That includes a file the gateway
        reads while a sandbox on this host masks its tree, named directly or through
        a link: the sandboxed harness may be denied it and drop it, so the block is
        sent even against a recorded copy equal to the file."""
        import json

        from kiro_crew import agent
        from kiro_crew.member_essential_context import native_spec_prompt_copy

        contract = self._managed_contract(tmp_path, monkeypatch)
        persona = "You are a bespoke reviewer."
        if case in ("relative-file", "home-file"):
            # ``~`` expands to the home; a relative ``file://`` resolves against
            # the spec's own directory.
            base = tmp_path / ".kiro" / "agents" if case == "relative-file" else tmp_path
            (base / "prompts").mkdir(parents=True)
            (base / "prompts" / "persona.md").write_text(persona, encoding="utf-8")
            monkeypatch.setenv("HOME", str(tmp_path))
            monkeypatch.setenv("USERPROFILE", str(tmp_path))
        # ``scratch`` is a crew-home leaf every sandbox tier masks.
        masked = tmp_path / ".kiro" / "crew" / "scratch" / "persona.md"
        if case.startswith("sandbox-masked"):
            masked.parent.mkdir(parents=True)
            masked.write_text(persona, encoding="utf-8")
        if case == "sandbox-masked-link":
            # A directory link (a junction on Windows, which needs no symlink
            # privilege) into the masked tree, so the case runs on every host and
            # ``prompts/persona.md`` reaches the masked file through the link.
            make_dir_link(tmp_path / "prompts", masked.parent)
        spec_prompt = {
            "managed-stub": agent._NATIVE_PROMPT_STUB,
            "managed-pointer": f"file://{contract}",
            "relative-file": "file://prompts/persona.md",
            "home-file": "file://~/prompts/persona.md",
            "missing-file": f"file://{tmp_path / 'prompts' / 'absent.md'}",
            "sandbox-masked-file": f"file://{masked}",
            "sandbox-masked-link": f"file://{tmp_path / 'prompts' / 'persona.md'}",
            "empty": "",
            "null": None,
            "absent-spec": persona,
        }[case]
        if case == "null":
            agents_dir = tmp_path / ".kiro" / "agents"
            agents_dir.mkdir(parents=True, exist_ok=True)
            (agents_dir / "test.json").write_text(
                json.dumps({"name": "test", "prompt": None}), encoding="utf-8"
            )
            monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
            monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir)
            monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents_dir)
        else:
            self._write_spec(tmp_path, monkeypatch, spec_prompt)
        assert native_spec_prompt_copy("nobody" if case == "absent-spec" else "test") is None
        if case in ("relative-file", "home-file", "sandbox-masked-file", "sandbox-masked-link"):
            # Crew still reads these for its own block; only the dedup is refused.
            native_copy: list[bool] = []
            assert ContextBuilder._load_agent_prompt("test", native_copy_out=native_copy) == persona
            assert native_copy == [False]
        if case.startswith("sandbox-masked"):
            msg, _ = self._builder(tmp_path).build_message(
                "hello",
                is_new_session=True,
                agent="test",
                provider_type="acp",
                context_provider=self._live_provider(documents=self._snapshot("test", persona)),
            )
            assert msg.count(persona) == 1
            assert f"[AGENT SYSTEM PROMPT]\n{persona}\n[END AGENT SYSTEM PROMPT]" in msg

    @pytest.mark.parametrize("agent_name", [None, "kirocrew"], ids=["no-agent", "kirocrew"])
    @pytest.mark.parametrize("compacted", [False, True], ids=["session-start", "post-compaction"])
    def test_default_agent_contract_injected_on_native_harness(
        self, tmp_path, monkeypatch, agent_name, compacted
    ):
        """kiro-cli reads only a pointer for the built-in agent, so its contract
        arrives through the block alone, on a native harness as on any other."""
        self._managed_contract(tmp_path, monkeypatch)
        msg, _ = self._builder(tmp_path).build_message(
            "hello",
            is_new_session=not compacted,
            needs_reinjection=compacted,
            agent=agent_name,
            provider_type="acp",
            context_provider=self._live_provider(),
        )
        assert msg.count("RESOLVED_CONTRACT") == 1
        assert "[AGENT SYSTEM PROMPT]\nRESOLVED_CONTRACT\n[END AGENT SYSTEM PROMPT]" in msg
