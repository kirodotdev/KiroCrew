"""A pin chosen under one harness never reaches another harness's wire.

``model_scope`` decides WHETHER a pin belongs; these tests pin WHERE that
question is asked. The frozen inventory registers every production function
that calls ``pin_applies`` or ``scoped_pin`` directly or through the injected
``model_pin_applies`` dependency. Focused behavior tests drive each resolution
and wire path whose outcome changes when the active harness cannot claim a pin.

``conftest`` isolates the advertised-model cache per test, so each test states
which harnesses have run.
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import model_registry as mr
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KIRO
from kiro_crew.config.loader import AgentConfig, KiroCrewConfig, resolve_effective_model

CLAUDE_SERVES = ["claude-opus-5[1m]", "claude-sonnet-4-6[1m]"]
CODEX_SERVES = ["openai.gpt-5.6-sol[low]", "openai.gpt-5.4[xhigh]"]

#: The pin from the report, in the spelling a claude picker stores.
CLAUDE_PIN = "claude-opus-5"


@pytest.fixture
def both_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mr,
        "_ADVERTISED_MODELS",
        {"claude_code": list(CLAUDE_SERVES), "codex": list(CODEX_SERVES)},
    )


def _cfg(backend: str, model: str) -> KiroCrewConfig:
    """A config resolved without reading the installed agents dir.

    ``model`` is written straight onto ``AgentConfig`` so the global is already
    collapsed: ``_resolve_agent_model`` (which reads ``~/.kiro/agents``) is only
    consulted for the ``auto`` sentinel.
    """
    return KiroCrewConfig(agent=AgentConfig(acp_backend=backend, model=model))


class TestTheFactorySelection:
    """``acp_effective_model`` is what the provider is constructed with."""

    def test_a_legacy_global_pin_is_not_carried_to_another_harness(self, both_warm: None) -> None:
        """The migration case, resolved at READ time rather than by a rewrite.

        ``agent.model`` predates any notion of scope: it records the opus the
        operator picked while claude was the backend, and says nothing about
        claude. After the switch to codex it must read as unset.
        """
        assert _cfg(ACP_BACKEND_CODEX, CLAUDE_PIN).acp_effective_model(None, None) == ""

    def test_the_same_stored_pin_still_applies_on_its_own_harness(self, both_warm: None) -> None:
        """Nothing is healed on disk, so switching BACK restores the pin.

        This is the property that makes the read-time rule a per-harness memory
        without a second field: one stored value, scoped per read.
        """
        assert _cfg(ACP_BACKEND_CLAUDE, CLAUDE_PIN).acp_effective_model(None, None) != ""

    def test_a_per_session_pick_is_scoped_too(self, both_warm: None) -> None:
        """``model_override`` carries the dashboard slot's pin and outranks every tier.

        Scoping only the config default would leave the dashboard — where the
        picker lives and where the report came from — still sending the pin.
        """
        cfg = _cfg(ACP_BACKEND_CODEX, "")
        assert cfg.acp_effective_model(None, CLAUDE_PIN) == ""

    def test_every_backend_resolves_a_non_empty_namespace(self) -> None:
        """The invariant that makes the namespace seam unambiguous.

        ``acp_effective_model`` takes a namespace, not a backend id, because a
        backend id carries an empty sentinel: ``resolve_selected_backend`` answers
        ``""`` both for kiro and for anything this build cannot select, so an empty
        value is indistinguishable from "not supplied" and a member session's pin
        gets judged against the configured harness rather than the one it runs.

        A namespace carries no such sentinel -- kiro's is ``acp``. A backend whose
        namespace resolved empty would reintroduce the ambiguity, so it must be
        given a namespace rather than left to default.
        """
        from kiro_crew.acp_backends import ACP_BACKENDS_KNOWN
        from kiro_crew.agent_sdk.capabilities import capabilities_for

        candidates = sorted(set(ACP_BACKENDS_KNOWN) | {""})
        empty = [b for b in candidates if not capabilities_for(b).model_id_namespace]
        assert empty == [], (
            "these backends resolve an empty model-registry namespace, which "
            f"re-opens the empty-sentinel ambiguity the seam removes: {empty!r}"
        )

    def test_a_member_route_scopes_the_resolved_kiro_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A member session may run kiro while configuration selects Claude Code."""
        from kiro_crew import model_scope

        observed: list[str] = []

        def _record_namespace(pin: str, namespace: str, **_kwargs: object) -> str:
            observed.append(namespace)
            return pin

        monkeypatch.setattr(model_scope, "scoped_pin", _record_namespace)
        cfg = _cfg(ACP_BACKEND_CLAUDE, CLAUDE_PIN)
        assert cfg.acp_effective_model(None, None, namespace="acp") != ""
        assert observed == ["acp"]

    def test_a_foreign_agent_pin_falls_through_to_the_global(
        self, both_warm: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _cfg(ACP_BACKEND_CODEX, CODEX_SERVES[0])
        monkeypatch.setattr(cfg, "_resolve_named_agent_model", lambda _agent: CLAUDE_PIN)

        assert cfg.acp_effective_model("reviewer", None) != ""

    def test_a_foreign_override_falls_through_to_the_global(self, both_warm: None) -> None:
        cfg = _cfg(ACP_BACKEND_CODEX, CODEX_SERVES[0])

        assert cfg.acp_effective_model(None, CLAUDE_PIN) != ""

    def test_auto_is_untouched(self, both_warm: None) -> None:
        """The shipped default pins nothing and must keep pinning nothing."""
        assert _cfg(ACP_BACKEND_CODEX, "").acp_effective_model(None, None) == ""

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
    def test_auto_resolves_as_unpinned_at_every_tier(self, backend: str) -> None:
        cfg = _cfg(backend, "")

        assert cfg.acp_effective_model(None, None, global_model="") == ""
        assert cfg.acp_effective_model(None, None, global_model="auto") == ""
        assert cfg.acp_effective_model(None, "auto") == ""

    def test_a_cold_harness_scopes_nothing(self) -> None:
        """No fixture: no harness has advertised, so no pin can be called foreign."""
        assert _cfg(ACP_BACKEND_CODEX, CLAUDE_PIN).acp_effective_model(None, None) != ""

    def test_a_cold_alias_pin_that_repoints_is_returned_untranslated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pin = "claude-haiku-4.5"
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})

        assert _cfg(ACP_BACKEND_CLAUDE, pin).acp_effective_model(None, None) == pin

    def test_a_cold_vocabulary_pin_is_translated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pin = "opus-4.8-1m"
        translated = "global.anthropic.claude-opus-4-8[1m]"
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        monkeypatch.setattr(mr, "to_provider_id", lambda _model, _namespace: translated)

        assert _cfg(ACP_BACKEND_CLAUDE, pin).acp_effective_model(None, None) == translated


class TestTheDisplayResolver:
    """The model chip must name the model the next turn will actually run."""

    def test_the_resolver_has_no_unused_backend_override(self) -> None:
        assert "backend" not in inspect.signature(resolve_effective_model).parameters

    def test_the_chip_drops_a_pin_the_active_harness_cannot_claim(self, both_warm: None) -> None:
        cfg = _cfg(ACP_BACKEND_CODEX, CLAUDE_PIN)
        assert resolve_effective_model(cfg, None) == ""

    def test_the_chip_shows_the_pin_on_its_own_harness(self, both_warm: None) -> None:
        cfg = _cfg(ACP_BACKEND_CLAUDE, CLAUDE_PIN)
        assert resolve_effective_model(cfg, None) == CLAUDE_PIN

    def test_the_chip_scopes_a_foreign_pin_at_debug(
        self, both_warm: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.model_scope"):
            assert resolve_effective_model(_cfg(ACP_BACKEND_CODEX, CLAUDE_PIN), None) == ""

        records = [r for r in caplog.records if r.name == "kiro_crew.model_scope"]
        assert records and all(r.levelno == logging.DEBUG for r in records)

    def test_the_chip_and_the_factory_agree(self, both_warm: None) -> None:
        """The two resolvers exist to not drift; a scope applied to one only would.

        Compared as "both pinned / both unpinned" rather than by id: the factory
        additionally translates into the backend's namespace, which the chip does
        not.
        """
        for backend in (ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KIRO):
            cfg = _cfg(backend, CLAUDE_PIN)
            assert bool(resolve_effective_model(cfg, None)) == bool(
                cfg.acp_effective_model(None, None)
            ), f"chip and factory disagree on {backend}"


class TestTheClientHandshake:
    """``_apply_startup_model`` is the site that emitted the reported warning."""

    @staticmethod
    def _client(backend: str, model: str, advertised: list[str]):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient()
        client._session_id = "sess-1"
        client._model = model
        client._acp_backend = backend
        client._available_models = [{"modelId": m, "name": m} for m in advertised]
        return client

    @pytest.mark.asyncio
    async def test_an_explicit_foreign_pick_is_sent(self, both_warm: None) -> None:
        client = self._client(ACP_BACKEND_CODEX, CODEX_SERVES[0], CODEX_SERVES)
        sent: list[tuple[str, str]] = []

        async def _record_config_option(name: str, value: str) -> None:
            sent.append((name, value))

        client.set_config_option = _record_config_option

        await client.set_model(CLAUDE_PIN)

        assert sent == [("model", CLAUDE_PIN)]

    @pytest.mark.asyncio
    async def test_a_foreign_pin_is_never_pushed(
        self, both_warm: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing on the wire, and no WARNING anywhere.

        The adapter's refusal warning is the symptom the user reported. A fix
        that still sent the id and merely handled the rejection more quietly
        would leave that line in place, so the absence of a WARNING is asserted
        as directly as the absence of a request.
        """
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._client(ACP_BACKEND_CODEX, CLAUDE_PIN, CODEX_SERVES)
        client._resolved_model_id = "openai.gpt-5.6-sol[low]"
        sent: list = []

        async def _record(method, params=None):
            sent.append((method, params))
            return {}

        client._send_request = _record
        client.set_config_option = _record

        with caplog.at_level(logging.DEBUG):
            await client._apply_startup_model()

        assert sent == [], f"a foreign pin reached the wire: {sent!r}"
        # Not left holding the id we declined: the "!= DEFAULT_MODEL" test is
        # what the warm-pool re-apply path reads, so a foreign id parked here
        # would be re-offered on every claim.
        assert client._model == DEFAULT_MODEL
        assert client._resolved_model_id == "openai.gpt-5.6-sol[low]"
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], f"the scope drop must not warn: {[r.getMessage() for r in warnings]}"

    @pytest.mark.asyncio
    async def test_an_alias_pin_is_scoped_before_wire_translation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.acp.client import DEFAULT_MODEL

        alias = "claude-haiku-4.5"
        substitute = "claude-sonnet-4-6[1m]"
        client = self._client(ACP_BACKEND_CLAUDE, alias, [substitute])
        client._resolved_model_id = substitute
        sent: list = []

        async def _record(method, params=None):
            sent.append((method, params))
            return {}

        client._send_request = _record
        client.set_config_option = _record

        with (
            patch(
                "kiro_crew.acp.client.model_registry.resolve_wire_model_id", return_value=substitute
            ),
            caplog.at_level(logging.DEBUG),
        ):
            await client._apply_startup_model()

        assert sent == [], f"an alias pin reached the wire: {sent!r}"
        assert client._model == DEFAULT_MODEL
        warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert warnings == [], [record.getMessage() for record in warnings]

    @pytest.mark.asyncio
    async def test_a_foreign_pin_entering_kiro_uses_the_live_list(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.acp.client import DEFAULT_MODEL

        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"codex": list(CODEX_SERVES)})
        kiro_serves = ["claude-opus-4.8"]
        client = self._client(ACP_BACKEND_KIRO, CODEX_SERVES[0], kiro_serves)
        client._resolved_model_id = kiro_serves[0]
        sent: list = []

        async def _record(method, params=None):
            sent.append((method, params))
            return {}

        client._send_request = _record
        client.set_config_option = _record

        with caplog.at_level(logging.DEBUG):
            await client._apply_startup_model()

        assert mr.advertised_models("acp") == []
        assert sent == [], f"a foreign pin reached the kiro wire: {sent!r}"
        assert client._model == DEFAULT_MODEL
        warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert warnings == [], [record.getMessage() for record in warnings]

    @pytest.mark.asyncio
    async def test_the_harness_own_pin_is_still_pushed(self, both_warm: None) -> None:
        """The scope gate must not become a general withhold."""
        client = self._client(ACP_BACKEND_KIRO, "claude-opus-4.8", ["claude-opus-4.8"])
        client._resolved_model_id = "claude-sonnet-4.6"
        sent: list = []

        async def _record(method, params=None):
            sent.append((method, params))
            return {}

        client._send_request = _record

        await client._apply_startup_model()

        assert len(sent) == 1, f"expected one model send, got {sent!r}"
        assert sent[0][1]["modelId"] == "claude-opus-4.8"
        assert client._model == "claude-opus-4.8"


class TestTheProviderRuntimeScope:
    @staticmethod
    def _provider(model: str):
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(model=model, acp_backend=ACP_BACKEND_CODEX)
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._resume_session_id = ""
        return provider

    @pytest.mark.asyncio
    async def test_a_foreign_pin_logs_info_without_an_entitlement_warning(
        self, both_warm: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = self._provider(CLAUDE_PIN)
        handle = MagicMock()
        handle.session_id = "codex-sess-1"
        handle.store_session_config = MagicMock()
        handle.set_model = AsyncMock()
        handle.available_models = [{"modelId": model, "name": model} for model in CODEX_SERVES]
        runtime = MagicMock(pid=4321)
        runtime.spawn = AsyncMock()
        runtime.create_session = AsyncMock(return_value=handle)

        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda h, r, **kw: MagicMock(_handle=h, _runtime=r, resumed=False),
            ),
            patch("pathlib.Path.exists", return_value=False),
            caplog.at_level(logging.INFO),
        ):
            await provider._start_kiro_runtime_impl({}, {})

        handle.set_model.assert_not_awaited()
        warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert warnings == [], [record.getMessage() for record in warnings]
        assert any(
            "belongs to another harness" in record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO
        )


class TestTheWarmPoolClaim:
    """The one wire site that re-applies the caller's pin without the factory."""

    @pytest.mark.asyncio
    async def test_a_foreign_pin_logs_info_without_an_entitlement_warning(
        self, both_warm: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 2
        cfg.session.pool_agent = "kirocrew"
        cfg.session.pool_ttl_secs = 1800
        cfg.session.timeout_secs = 3600
        cfg.agent.default_agent = ""
        cfg.agent.model = "auto"
        cfg.agent.acp_backend = ACP_BACKEND_CODEX

        pooled = MagicMock(spec=AcpProvider)
        pooled.client = MagicMock()
        pooled.client.backend = ACP_BACKEND_CODEX
        pooled.client._model = CODEX_SERVES[1]
        pooled.client.rekey = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.available_models = MagicMock(
            return_value=[{"modelId": model, "name": model} for model in CODEX_SERVES]
        )
        pooled.is_process_alive = MagicMock(return_value=True)
        pooled.cwd = ""

        factory = MagicMock(return_value=pooled)
        manager = SessionManager(cfg, factory)
        manager._drain_and_claim = AsyncMock(return_value=pooled)
        manager._resolve_agent_model = MagicMock(return_value=CODEX_SERVES[1])

        with caplog.at_level(logging.INFO):
            provider, _, _ = await manager.get_or_create("slot-foreign", model=CLAUDE_PIN)

        assert provider is pooled
        pooled.client.set_model.assert_not_awaited()
        warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert warnings == [], [record.getMessage() for record in warnings]
        assert any(
            "belongs to another harness" in record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO
        )

    @pytest.mark.asyncio
    async def test_an_alias_pin_is_not_translated_before_scoping(
        self, both_warm: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 2
        cfg.session.pool_agent = "kirocrew"
        cfg.session.pool_ttl_secs = 1800
        cfg.session.timeout_secs = 3600
        cfg.agent.default_agent = ""
        cfg.agent.model = "auto"
        cfg.agent.acp_backend = ACP_BACKEND_CLAUDE

        pooled = MagicMock(spec=AcpProvider)
        pooled.client = MagicMock()
        pooled.client.backend = ACP_BACKEND_CLAUDE
        pooled.client._model = "global.anthropic.claude-sonnet-4-6[1m]"
        pooled.client.rekey = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.available_models = MagicMock(
            return_value=[{"modelId": "global.anthropic.claude-sonnet-4-6[1m]"}]
        )
        pooled.is_process_alive = MagicMock(return_value=True)
        pooled.cwd = ""

        factory = MagicMock(return_value=pooled)
        manager = SessionManager(cfg, factory)
        manager._drain_and_claim = AsyncMock(return_value=pooled)
        manager._resolve_agent_model = MagicMock(
            return_value="global.anthropic.claude-sonnet-4-6[1m]"
        )

        with caplog.at_level(logging.INFO):
            await manager.get_or_create("slot-alias", model="claude-haiku-4.5")

        pooled.client.set_model.assert_not_awaited()
        assert any(
            "belongs to another harness" in record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO
        )


#: The calls that translate a model id into a backend's own namespace. A value
#: assigned from one of these is POST-translation, and for an alias that
#: translation is already a substitution -- ``to_provider_id('claude-haiku-4.5',
#: 'claude_code')`` answers Sonnet's id, because the claude backend serves no
#: Haiku. Scoping such a value asks the gate about the substitute instead of the
#: pin, so the pin passes and the substitution lands silently.
_TRANSLATING_CALLS = frozenset({"to_provider_id", "to_acp_id", "resolve_wire_model_id"})

#: The scope entry points. ``model_pin_applies`` is the injected spelling
#: ``session_allocation`` reaches ``pin_applies`` through.
_SCOPE_CALLS = frozenset({"pin_applies", "scoped_pin", "model_pin_applies"})


class _TranslatedPinVisitor(ast.NodeVisitor):
    """Find scope calls whose first argument was translated in the same function.

    Per function body: record every local name assigned from a translating call,
    then flag any scope call whose first positional argument is one of those names.
    Scoped per function rather than per module because a name is only evidence
    about the flow it was assigned in.
    """

    def __init__(self, module: str) -> None:
        self.module = module
        self.scope: list[str] = []
        self.offenders: list[tuple[str, str, str]] = []

    def _qualified(self) -> str:
        return ".".join(self.scope) or "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        for call in self._scope_calls(node):
            if not call.args:
                continue
            translated = self._translated_names(node, before=call)
            first = call.args[0]
            if isinstance(first, ast.Name):
                name = first.id
            elif isinstance(first, ast.Attribute):
                name = ast.unparse(first)
            else:
                continue
            if name in translated:
                self.offenders.append((self.module, self._qualified(), name))
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    @staticmethod
    def _translated_names(node: ast.AST, *, before: ast.Call) -> set[str]:
        names: set[str] = set()
        before_position = (before.lineno, before.col_offset)
        for inner in ast.walk(node):
            position = (getattr(inner, "lineno", -1), getattr(inner, "col_offset", -1))
            if position >= before_position:
                continue
            if isinstance(inner, ast.Assign):
                targets = inner.targets
                value = inner.value
            elif isinstance(inner, ast.AnnAssign):
                targets = [inner.target]
                value = inner.value
            else:
                continue
            if not isinstance(value, ast.Call):
                continue
            func = value.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called not in _TRANSLATING_CALLS:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(ast.unparse(target))
        return names

    @staticmethod
    def _scope_calls(node: ast.AST) -> list[ast.Call]:
        found: list[ast.Call] = []
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                if inner.func.attr in _SCOPE_CALLS:
                    found.append(inner)
        return found


def _translated_pin_sites(source_root: Path) -> list[tuple[str, str, str]]:
    """Every scope call in *source_root* handed a value translated nearby."""
    repository_root = source_root.parents[1]
    offenders: list[tuple[str, str, str]] = []
    for path in sorted(source_root.rglob("*.py")):
        if path.name == "model_scope.py":
            continue
        visitor = _TranslatedPinVisitor(path.relative_to(repository_root).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        offenders.extend(visitor.offenders)
    return offenders


class TestScopeSeesTheUntranslatedPin:
    """The invariant behind every scope site: judge the pin, never its translation.

    A frozen list of call sites cannot express this. The warm-pool claim was
    registered in such a list and still scoped an already-translated alias, so the
    silent ``claude-haiku-4.5`` to Sonnet substitution survived there. This asserts
    the property instead of the inventory, and it needs no edit when a legitimate
    site is added. The prose inventory of the sites lives in
    ``docs/system-specs/common/model-selection.md``.
    """

    def test_no_scope_site_is_handed_a_translated_value(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "kiro_crew"
        offenders = _translated_pin_sites(source_root)
        assert offenders == [], (
            "a model scope call is judging a translated id instead of the stored pin; "
            "pass the pre-translation value (see acp_effective_model, which scopes "
            f"before translating): {offenders!r}"
        )

    def test_attribute_flow_is_detected(self, tmp_path: Path) -> None:
        source_root = tmp_path / "src" / "kiro_crew"
        source_root.mkdir(parents=True)
        (source_root / "example.py").write_text(
            "class Client:\n"
            "    def apply(self):\n"
            "        self.model = registry.resolve_wire_model_id(self.model, namespace)\n"
            "        return scope.pin_applies(self.model, namespace)\n",
            encoding="utf-8",
        )

        assert _translated_pin_sites(source_root) == [
            ("src/kiro_crew/example.py", "Client.apply", "self.model")
        ]


class TestThePickerDefault:
    """``GET /api/models`` marks a default; it must be one this list can contain."""

    def test_a_foreign_pin_is_not_offered_as_this_harness_default(self, both_warm: None) -> None:
        """Marking it would put an id the returned list omits into the selected slot."""
        from kiro_crew.dashboard.handlers.agents import _scoped_default

        cfg = _cfg(ACP_BACKEND_CODEX, CLAUDE_PIN)
        assert _scoped_default(cfg, ACP_BACKEND_CODEX) == ""

    def test_the_harness_own_pin_is_offered(self, both_warm: None) -> None:
        from kiro_crew.dashboard.handlers.agents import _scoped_default

        cfg = _cfg(ACP_BACKEND_CLAUDE, CLAUDE_PIN)
        assert _scoped_default(cfg, ACP_BACKEND_CLAUDE) == CLAUDE_PIN

    def test_the_picker_scopes_a_foreign_pin_at_debug(
        self, both_warm: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.dashboard.handlers.agents import _scoped_default

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.model_scope"):
            assert _scoped_default(_cfg(ACP_BACKEND_CODEX, CLAUDE_PIN), ACP_BACKEND_CODEX) == ""

        records = [r for r in caplog.records if r.name == "kiro_crew.model_scope"]
        assert records and all(r.levelno == logging.DEBUG for r in records)

    def test_a_config_without_the_field_answers_empty(self, both_warm: None) -> None:
        """Callers stub ``cfg.agent`` with only the fields their path reads.

        The endpoint reads ``acp_backend`` through ``getattr`` for that reason,
        and this read has to be as forgiving: a stub carrying only the backend
        must still get a list back rather than an AttributeError.
        """
        import types

        from kiro_crew.dashboard.handlers.agents import _scoped_default

        cfg = types.SimpleNamespace(agent=types.SimpleNamespace(acp_backend=ACP_BACKEND_CODEX))
        assert _scoped_default(cfg, ACP_BACKEND_CODEX) == ""
