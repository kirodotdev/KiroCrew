"""``decisions.provider``: the provider contract, the registry, and the fallbacks.

Pins four things the refactor must not move:

* every registered provider satisfies :class:`DecisionProvider` and carries
  coherent egress / authority / capability metadata;
* the gate builds its provider through the registry, and the registry picks by
  lane and by the configured endpoint, not by a hard-wired class;
* every refusal -- no consent, the fleet ceiling, a failing or hanging provider --
  still returns ``None`` (the caller's shipped behaviour) without constructing a
  provider or opening a socket on the refusal paths;
* the stored spellings (``"jev"`` lane, ``auto:jev`` slot model, ``"jev"``
  feedback side) still read with the meaning they had.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.config.sections import (
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
    NudgeWakeConfig,
)
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions import local_models
from kiro_crew.decisions import provider as provider_mod
from kiro_crew.decisions.types import Answer, Choice, Noul

REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

QUESTIONS = [Choice(id="verdict", prompt="Which skill?", options=["NONE", "DUP"])]
JUDGE_QUESTIONS = [Noul(id="quiet", prompt="Hold this wake?")]


def _config(*, timeout_ms: int = 1000, judge_provider: str | None = None):
    """A stand-in carrying only ``.decisions``, which is all the gate reads."""
    kwargs: dict = {"bucket": 100, "provider": DecisionProviderConfig(timeout_ms=timeout_ms)}
    if judge_provider is not None:
        kwargs["nudge_wake"] = NudgeWakeConfig(provider=judge_provider)
    return SimpleNamespace(decisions=DecisionsConfig(**kwargs))


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    """A real keystone file under *tmp_path*; returns a writer. Absent by default."""
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)

    def _write(state: dict | None) -> None:
        if state is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(state), encoding="utf-8")

    return _write


@pytest.fixture
def no_rows(monkeypatch):
    """Record rows instead of writing them; returns the list."""
    rows: list[dict] = []

    def _append(row, **_kwargs):
        rows.append(row)
        return True

    monkeypatch.setattr(gate_mod._log, "append", _append)
    return rows


@pytest.fixture
def selections(monkeypatch):
    """Record every registry selection the gate makes; returns the list of spec ids."""
    chosen: list[str] = []
    real = provider_mod.select

    def _spy(lane, **kwargs):
        spec = real(lane, **kwargs)
        chosen.append(spec.id)
        return spec

    monkeypatch.setattr(gate_mod._provider, "select", _spy)
    return chosen


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test if anything opens an outbound connection."""

    def _refuse(*_args, **_kwargs):
        pytest.fail("a decision path opened a network connection")

    real_connect = socket.socket.connect

    def _connect(self, *args, **kwargs):
        # On Windows every new event loop (``asyncio.run`` in each test) builds its
        # self-pipe with ``socket.socketpair()``, whose stdlib fallback connects two
        # loopback sockets. That is the loop's own plumbing, not a decision path.
        caller = sys._getframe(1)
        if caller.f_globals.get("__name__") == "socket" and caller.f_code.co_name in (
            "_fallback_socketpair",
            "socketpair",
        ):
            return real_connect(self, *args, **kwargs)
        return _refuse()

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


class _Answering:
    """A provider double answering every question in its own domain."""

    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, state, questions):
        self.calls += 1
        out = {}
        for q in questions:
            value = q.options[-1] if isinstance(q, Choice) else 0.9
            out[q.id] = Answer(id=q.id, value=value, p=0.9)
        return out


# ---------------------------------------------------------------------------
# Protocol conformance and metadata
# ---------------------------------------------------------------------------


class TestEveryProviderConforms:
    @pytest.mark.parametrize("spec", provider_mod.providers(), ids=lambda s: s.id)
    def test_build_returns_a_decision_provider(self, spec):
        built = spec.build(DecisionProviderConfig(), "auto")
        assert isinstance(built, provider_mod.DecisionProvider)
        assert inspect.iscoroutinefunction(built.ask)

    @pytest.mark.parametrize("spec", provider_mod.providers(), ids=lambda s: s.id)
    def test_metadata_is_coherent(self, spec):
        assert spec.lane in provider_mod.LANES
        assert spec.egress in (
            provider_mod.EGRESS_THIRD_PARTY,
            provider_mod.EGRESS_LOOPBACK,
            provider_mod.EGRESS_SESSION_PROVIDER,
        )
        assert spec.authority in (
            provider_mod.AUTHORITY_KEYSTONE,
            provider_mod.AUTHORITY_SESSION_PROVIDER,
        )
        assert spec.kind in (
            provider_mod.KIND_HOSTED,
            provider_mod.KIND_LOCAL,
            provider_mod.KIND_SESSION_LLM,
        )
        image_kinds = {provider_mod.KIND_BOX, provider_mod.CAPABILITY_IMAGE}
        assert spec.supports and spec.supports <= provider_mod.QUESTION_KINDS | image_kinds
        assert provider_mod.QUESTION_KINDS <= spec.supports
        # Box is asked only about an image, so it is declared with images or not at all.
        assert (provider_mod.KIND_BOX in spec.supports) is (
            provider_mod.CAPABILITY_IMAGE in spec.supports
        )
        # Only the hosted service is declared to read images.
        assert (provider_mod.CAPABILITY_IMAGE in spec.supports) is (
            spec.id == provider_mod.PROVIDER_HOSTED
        )
        # Only a loopback provider is governed by the local row, and only the
        # session-LLM lane is authorized without the keystone.
        assert spec.local is (spec.egress == provider_mod.EGRESS_LOOPBACK)
        assert (spec.governance_row == provider_mod.ROW_DECISIONS_LOCAL) is spec.local
        assert spec.governance_row in (provider_mod.ROW_DECISIONS, provider_mod.ROW_DECISIONS_LOCAL)
        assert (spec.authority == provider_mod.AUTHORITY_SESSION_PROVIDER) is (
            spec.lane == provider_mod.LANE_SESSION_LLM
        )

    def test_ids_are_unique_and_every_lane_has_a_provider(self):
        ids = [spec.id for spec in provider_mod.providers()]
        assert len(ids) == len(set(ids))
        assert {spec.lane for spec in provider_mod.providers()} == set(provider_mod.LANES)

    def test_core_contract_names_no_vendor(self):
        """The contract module's public names are provider-neutral."""
        public = [name for name in vars(provider_mod) if not name.startswith("_")]
        assert not [name for name in public if "jev" in name.lower()]

    def test_importing_the_contract_pulls_in_no_transport(self):
        tree = ast.parse((REPO_SRC / "decisions" / "provider.py").read_text(encoding="utf-8"))
        top = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        names = {
            (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
            for node in top
            for alias in node.names
        }
        assert not [n for n in names if "aiohttp" in n or "impl_" in n or "capability" in n]


# ---------------------------------------------------------------------------
# Registry selection
# ---------------------------------------------------------------------------


class TestRegistrySelection:
    def test_session_llm_lane(self):
        assert provider_mod.select(gate_mod.LANE_LLM).id == provider_mod.PROVIDER_SESSION_LLM

    def test_hosted_default_endpoint(self):
        spec = provider_mod.select(
            gate_mod.LANE_ENDPOINT, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, model=""
        )
        assert spec.id == provider_mod.PROVIDER_HOSTED

    @pytest.mark.parametrize("preset", local_models.LOCAL_MODELS, ids=lambda m: m.id)
    def test_each_catalog_preset_selects_the_local_provider(self, preset):
        endpoint = local_models.endpoint_for(preset.default_port)
        spec = provider_mod.select(gate_mod.LANE_ENDPOINT, endpoint=endpoint, model=preset.model)
        assert spec.id == provider_mod.PROVIDER_LOCAL
        assert spec.local is True

    @pytest.mark.parametrize(
        "endpoint", ["http://127.0.0.1:9999/custom", "none", "https://proxy.example.invalid/x"]
    )
    def test_anything_else_is_governed_as_hosted(self, endpoint):
        """A hand-written loopback may tunnel to a hosted provider; it is not local."""
        spec = provider_mod.select(gate_mod.LANE_ENDPOINT, endpoint=endpoint, model="m")
        assert spec.id == provider_mod.PROVIDER_HOSTED

    def test_unknown_lane_reads_as_the_endpoint_lane(self):
        assert provider_mod.select("typo").lane == gate_mod.LANE_ENDPOINT

    def test_the_gate_builds_through_the_registry(self, monkeypatch):
        """``gate._oracle`` constructs what the selected spec's implementation is."""
        import kiro_crew.decisions.impl_llm as impl_llm
        import kiro_crew.decisions.impl_systemone as impl_systemone

        endpoint_double, llm_models = object(), []
        monkeypatch.setattr(impl_systemone, "SystemOneProvider", lambda provider: endpoint_double)
        monkeypatch.setattr(
            impl_llm, "LlmProvider", lambda model="": llm_models.append(model) or _Answering()
        )
        cfg = DecisionProviderConfig()
        assert gate_mod._oracle(gate_mod.LANE_ENDPOINT, cfg, "jev-latest") is endpoint_double
        gate_mod._oracle(gate_mod.LANE_LLM, cfg, impl_llm.JUDGE_MODEL_DEFAULT)
        gate_mod._oracle(gate_mod.LANE_LLM, cfg, "my-judge-1")
        # The inherit sentinel asks for no model; a named one is passed through.
        assert llm_models == ["", "my-judge-1"]

    def test_budget_window_comes_from_the_registry(self):
        low, high = provider_mod.SESSION_LLM_TIMEOUT_WINDOW
        assert gate_mod.timeout_secs(_config(timeout_ms=1), lane=gate_mod.LANE_LLM) == low
        assert gate_mod.timeout_secs(_config(timeout_ms=10**9), lane=gate_mod.LANE_LLM) == high
        assert gate_mod.timeout_secs(_config(timeout_ms=250)) == pytest.approx(0.25)


#: What the hard-wired selection this registry replaced built, written out as
#: data: ``(point is the judge, judge provider, endpoint consented) -> built``.
#: ``None`` means no provider was constructed. Every non-judge point had one
#: provider, the System One client, and only on consent.
_LEGACY = {
    (False, None, True): "endpoint",
    (False, None, False): None,
    (True, "auto", True): "endpoint",
    (True, "auto", False): "llm",
    (True, "jev", True): "endpoint",
    (True, "jev", False): None,
    (True, "llm", True): "llm",
    (True, "llm", False): "llm",
}


class TestResolutionMatchesLegacy:
    """Every point, under each consent and judge-lane setting, builds what it did before."""

    @pytest.mark.parametrize(
        ("point", "judge_provider", "consented"),
        [
            (point, lane, consented)
            for point in gate_mod.DECISION_POINT_NAMES
            # The judge lane setting only reaches the judge point.
            for lane in (("auto", "jev", "llm") if point == gate_mod.JUDGE_POINT else ("auto",))
            for consented in (True, False)
        ],
    )
    def test_provider_resolution_matches_legacy(
        self, keystone, no_rows, no_network, monkeypatch, point, consented, judge_provider
    ):
        import kiro_crew.decisions.impl_llm as impl_llm
        import kiro_crew.decisions.impl_systemone as impl_systemone

        is_judge = point == gate_mod.JUDGE_POINT
        built: list[str] = []

        def _record(kind):
            def _factory(*_a, **_k):
                built.append(kind)
                return _Answering()

            return _factory

        monkeypatch.setattr(impl_systemone, "SystemOneProvider", _record("endpoint"))
        monkeypatch.setattr(impl_llm, "LlmProvider", _record("llm"))
        monkeypatch.setattr(gate_mod, "_capability_denied", lambda *_a, **_k: False)
        scopes = {"tool_args": True, "compaction": True, "memory_text": True}
        scopes["nudge_evidence"] = True
        keystone(
            {"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT, **scopes}
            if consented
            else None
        )
        cfg = _config(judge_provider=judge_provider)
        asyncio.run(gate_mod.decide(point, {"m": "x"}, QUESTIONS, config=cfg))
        expected = _LEGACY[(is_judge, judge_provider if is_judge else None, consented)]
        assert built == ([] if expected is None else [expected])


# ---------------------------------------------------------------------------
# Fallbacks: nothing constructed, nothing sent, the caller's path runs
# ---------------------------------------------------------------------------


class TestDisabledConstructsNothing:
    def test_no_keystone_no_provider_no_network(self, keystone, selections, no_rows, no_network):
        keystone(None)
        cfg = _config()
        for point in gate_mod.DECISION_POINT_NAMES:
            if point == gate_mod.JUDGE_POINT:
                continue
            assert asyncio.run(gate_mod.decide(point, {"m": "x"}, QUESTIONS, config=cfg)) is None
            assert gate_mod.is_enabled(point, config=cfg) is False
        assert selections == []
        assert no_rows == []

    def test_provider_none_constructs_nothing_even_with_consent(
        self, keystone, selections, no_rows, no_network
    ):
        keystone({"enabled": True, "endpoint": local_models.ENDPOINT_NONE})
        cfg = SimpleNamespace(
            decisions=DecisionsConfig(
                bucket=100, provider=DecisionProviderConfig(endpoint=local_models.ENDPOINT_NONE)
            )
        )
        assert asyncio.run(gate_mod.decide("skills.select", {}, QUESTIONS, config=cfg)) is None
        assert selections == []


class TestPolicyDenial:
    """The internal edition pins ``capabilities.decisions`` off: no lane may run."""

    @pytest.fixture
    def denied(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.decisions.capability.is_decisions_denied",
            lambda *_a, **_k: True,
        )

    def test_consented_endpoint_point_falls_back(
        self, keystone, denied, selections, no_rows, no_network
    ):
        keystone({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT})
        answers = asyncio.run(gate_mod.decide("skills.select", {}, QUESTIONS, config=_config()))
        assert answers is None
        assert selections == []
        assert no_rows == []

    @pytest.mark.parametrize("preset", local_models.LOCAL_MODELS, ids=lambda m: m.id)
    def test_a_consented_local_preset_falls_back(
        self, keystone, denied, selections, no_rows, no_network, monkeypatch, preset
    ):
        """The local provider is denied too, attested or not."""
        endpoint = local_models.endpoint_for(preset.default_port)
        keystone({"enabled": True, "endpoint": endpoint})
        monkeypatch.setattr(
            "kiro_crew.decisions.capability.is_local_preset", lambda *_a, **_k: True
        )
        cfg = SimpleNamespace(
            decisions=DecisionsConfig(
                bucket=100,
                provider=DecisionProviderConfig(endpoint=endpoint, model=preset.model),
            )
        )
        assert asyncio.run(gate_mod.decide("skills.select", {}, QUESTIONS, config=cfg)) is None
        assert selections == []
        assert no_rows == []

    @pytest.mark.parametrize("lane", ["auto", "jev", "llm"])
    def test_the_judge_falls_back_on_every_lane(
        self, keystone, denied, selections, no_rows, no_network, lane
    ):
        keystone(
            {
                "enabled": True,
                "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT,
                "nudge_evidence": True,
            }
        )
        cfg = _config(judge_provider=lane)
        answers = asyncio.run(
            gate_mod.decide(gate_mod.JUDGE_POINT, {}, JUDGE_QUESTIONS, config=cfg)
        )
        assert answers is None
        assert selections == []


class TestUnconsentedEndpointFallsToTheSessionModel:
    def test_auto_judge_uses_the_non_endpoint_provider(
        self, keystone, selections, no_rows, no_network, monkeypatch
    ):
        """No endpoint consent: ``auto`` lands on the session-LLM provider, never the endpoint."""
        import kiro_crew.decisions.impl_llm as impl_llm
        import kiro_crew.decisions.impl_systemone as impl_systemone

        keystone(None)
        monkeypatch.setattr(gate_mod, "_capability_denied", lambda *_a, **_k: False)
        answering = _Answering()
        monkeypatch.setattr(impl_llm, "LlmProvider", lambda model="": answering)
        monkeypatch.setattr(
            impl_systemone,
            "SystemOneProvider",
            lambda provider: pytest.fail("the endpoint provider was built without consent"),
        )
        answers = asyncio.run(
            gate_mod.decide(
                gate_mod.JUDGE_POINT, {}, JUDGE_QUESTIONS, config=_config(judge_provider="auto")
            )
        )
        assert answers is not None and answering.calls == 1
        assert selections == [provider_mod.PROVIDER_SESSION_LLM]


class TestProviderFailureFallsBack:
    @pytest.fixture
    def consented(self, keystone):
        keystone({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT})

    def _install(self, monkeypatch, oracle):
        import kiro_crew.decisions.impl_systemone as impl_systemone

        monkeypatch.setattr(impl_systemone, "SystemOneProvider", lambda provider: oracle)

    def test_refusing_provider(self, consented, no_rows, monkeypatch):
        class _Refusing:
            async def ask(self, state, questions):
                raise RuntimeError("refused")

        monkeypatch.setattr(gate_mod, "_capability_denied", lambda *_a, **_k: False)
        self._install(monkeypatch, _Refusing())
        assert (
            asyncio.run(gate_mod.decide("skills.select", {}, QUESTIONS, config=_config())) is None
        )
        assert [row["error"] for row in no_rows] == [gate_mod.ERROR_PROVIDER]

    def test_hanging_provider_is_cut_at_the_budget(self, consented, no_rows, monkeypatch):
        class _Hanging:
            async def ask(self, state, questions):
                # Never set: only the gate's own budget ends this await.
                await asyncio.Event().wait()

        monkeypatch.setattr(gate_mod, "_capability_denied", lambda *_a, **_k: False)
        self._install(monkeypatch, _Hanging())
        cfg = _config(timeout_ms=20)
        assert asyncio.run(gate_mod.decide("skills.select", {}, QUESTIONS, config=cfg)) is None
        assert [row["error"] for row in no_rows] == [gate_mod.ERROR_TIMEOUT]

    def test_unbuildable_provider(self, consented, no_rows, monkeypatch):
        monkeypatch.setattr(gate_mod, "_capability_denied", lambda *_a, **_k: False)

        def _broken(provider):
            raise ImportError("no transport")

        import kiro_crew.decisions.impl_systemone as impl_systemone

        monkeypatch.setattr(impl_systemone, "SystemOneProvider", _broken)
        assert (
            asyncio.run(gate_mod.decide("skills.select", {}, QUESTIONS, config=_config())) is None
        )
        assert [row["error"] for row in no_rows] == [gate_mod.ERROR_PROVIDER]


# ---------------------------------------------------------------------------
# Stored spellings keep their meaning
# ---------------------------------------------------------------------------


class TestPersistedSpellings:
    def test_the_endpoint_lane_value_is_still_jev(self):
        assert gate_mod.LANE_ENDPOINT == gate_mod.LANE_JEV == "jev"
        assert gate_mod.LANE_LLM == "llm"
        cfg = _config(judge_provider="jev")
        assert gate_mod.judge_lane(cfg, endpoint_consented=False) == gate_mod.LANE_ENDPOINT

    def test_auto_jev_slot_model_still_resolves(self):
        from kiro_crew.dashboard.chat_handlers import JEV_ROUTE_MODEL
        from kiro_crew.dashboard.session_control_parts.model_switch import _JEV_ROUTE_SPELLINGS

        assert JEV_ROUTE_MODEL == "auto:jev"
        assert "auto:jev" in _JEV_ROUTE_SPELLINGS

    def test_former_python_names_are_the_same_objects(self):
        import kiro_crew.decisions.impl_jev as impl_jev
        import kiro_crew.decisions.impl_llm as impl_llm
        import kiro_crew.decisions.impl_systemone as impl_systemone

        assert impl_jev is impl_systemone
        assert impl_systemone.JevOracle is impl_systemone.SystemOneProvider
        assert impl_systemone.JevProtocolError is impl_systemone.ProviderProtocolError
        assert impl_systemone.JevHttpError is impl_systemone.ProviderHttpError
        assert impl_llm.LlmOracle is impl_llm.LlmProvider
        assert local_models.PRESET_JEV == local_models.PRESET_HOSTED == "jev"

    def test_hosted_profile_keeps_the_shipped_values(self):
        import kiro_crew.decisions.impl_systemone as impl_systemone

        profile = impl_systemone.HOSTED_TYPESAFE
        assert profile.endpoint == DECISION_PROVIDER_ENDPOINT_DEFAULT
        assert impl_systemone.VAULT_SECRET_NAME == profile.vault_secret == "TYPESAFE_API_KEY"
        assert impl_systemone._REQUEST_ID_HEADER == profile.request_id_header

    def test_feedback_side_jev_still_reads(self):
        from kiro_crew.decisions.log import FEEDBACK_SIDES

        assert "jev" in FEEDBACK_SIDES


# ---------------------------------------------------------------------------
# The browser stack does not depend on decisions
# ---------------------------------------------------------------------------


def _browser_sources() -> list[Path]:
    roots = list(REPO_SRC.glob("browser*"))
    roots += list((REPO_SRC / "mcp_tools").glob("browser*.py"))
    roots += list((REPO_SRC / "dashboard" / "handlers").glob("browser*.py"))
    files: list[Path] = []
    for root in roots:
        files.extend(sorted(root.rglob("*.py")) if root.is_dir() else [root])
    return files


#: The one browser module that may reach the seam, and only from inside a function
#: (the optional ``browser.step`` hooks), so the package never joins the browser's
#: import graph.
_BROWSER_DECISION_HOOKS = "browser_harness/decide.py"


def _imports_decisions(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hooks = path.relative_to(REPO_SRC).as_posix() == _BROWSER_DECISION_HOOKS
    nodes = tree.body if hooks else list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            if any(a.name.startswith("kiro_crew.decisions") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("kiro_crew.decisions"):
                return True
            if module == "kiro_crew" and any(a.name == "decisions" for a in node.names):
                return True
    return False


def test_the_browser_stack_imports_nothing_from_decisions():
    files = _browser_sources()
    assert files, "no browser sources found; the scan would pass vacuously"
    assert _BROWSER_DECISION_HOOKS in {p.relative_to(REPO_SRC).as_posix() for p in files}
    assert [str(p.relative_to(REPO_SRC)) for p in files if _imports_decisions(p)] == []


def test_the_browser_hooks_reach_the_seam_only_through_the_gate():
    """No provider implementation is named from browser code: only ``gate``/``types``."""
    tree = ast.parse((REPO_SRC / _BROWSER_DECISION_HOOKS).read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("kiro_crew.decisions")
    }
    assert modules == {"kiro_crew.decisions.types"} | (modules & {"kiro_crew.decisions"})
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.decisions"
        for alias in node.names
    }
    assert names == {"gate"}
    assert "jev" not in (REPO_SRC / _BROWSER_DECISION_HOOKS).read_text(encoding="utf-8").lower()
