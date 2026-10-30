"""Tests for model attribution in kiro_crew.dashboard.handlers.usage.

Covers the wrapper walk that finds a session's model and the warning a billed
row with no model raises. The rest of the module's tests live in test_usage.py.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.dashboard.handlers.usage import (
    _resolve_model,
    _wrapper_chain,
    persist_token_record,
    read_effective_agent,
    read_effective_model,
    read_turn_model,
)

# ── model attribution through wrapper layers ────────────────────────────
#
# ``persist_token_record`` fills a blank caller-side model from ``model_source``
# by walking the provider/client/handle nesting with ``_wrapper_chain``. The
# model sits at the bottom of the ``_handle`` chain under wrapper layers whose
# sibling holders hold nothing, so the walk is depth-first with a visited set and
# only a runaway guard; a blank model on a billed row would otherwise land the
# turn's credits in the ``unknown`` bucket. A billed row that still has no model
# is logged at the write site.


class _Node:
    """A bare wrapper. Attributes are exactly what the constructor sets."""

    def __init__(self, name: str, **holders: object) -> None:
        self._name = name
        for holder, inner in holders.items():
            setattr(self, holder, inner)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<{self._name}>"


def _documented_shape(extra_layers: int, *, model: str = "auto") -> tuple[object, object]:
    """The nesting ``_wrapper_chain``'s docstring documents, plus wrappers.

    ``AcpProvider`` -> ``client``/``_client`` -> ``AcpSessionProvider`` ->
    ``_handle`` -> ``AcpSessionHandle`` (holds ``_model``) -> ``_runtime``, then
    ``extra_layers`` wrappers stacked on top, each carrying the same four holders
    the walk follows, three of them siblings that hold nothing. Returns
    ``(outermost, handle)``.
    """
    runtime = _Node("runtime")
    handle = _Node("handle", _runtime=runtime)
    handle._model = model
    session_provider = _Node("session_provider", _handle=handle, _runtime=runtime)
    node: object = _Node("provider", client=session_provider, _client=session_provider)
    for i in range(extra_layers):
        node = _Node(
            f"wrapper{i}",
            client=_Node(f"a{i}"),
            _client=_Node(f"b{i}"),
            _handle=node,
            _runtime=_Node(f"c{i}"),
        )
    return node, handle


class TestWalkReachesTheModelRegardlessOfDepth:
    """The wrapper-depth failure, pinned against the real walk."""

    @pytest.mark.parametrize("extra_layers", [0, 1, 2, 3, 4])
    def test_documented_shape_resolves_auto_through_every_layer_count(self, extra_layers):
        source, handle = _documented_shape(extra_layers)
        assert any(
            node is handle for node in _wrapper_chain(source)
        ), f"the handle must be in the chain at {extra_layers} extra layer(s)"
        assert _resolve_model("", source) == "auto"

    @pytest.mark.parametrize("extra_layers", [3, 4, 12])
    def test_concrete_model_survives_deep_wrapping(self, extra_layers):
        source, _handle = _documented_shape(extra_layers, model="claude-opus-5")
        assert read_effective_model(source) == "claude-opus-5"
        assert read_turn_model(source) == "claude-opus-5"
        assert _resolve_model("", source) == "claude-opus-5"

    def test_resolved_id_deep_in_the_chain_still_beats_a_shallow_model(self):
        # Two-pass precedence is unchanged by the traversal order: a resolved id
        # ANYWHERE outranks a plain _model anywhere.
        source, handle = _documented_shape(4, model="auto")
        handle._resolved_model_id = "global.anthropic.claude-opus-5[1m]"
        outer = _Node("outer", _handle=source)
        outer._model = "claude-opus-5"
        assert read_effective_model(outer) == "global.anthropic.claude-opus-5[1m]"

    def test_agent_is_read_off_the_runtime_below_deep_wrapping(self):
        source, handle = _documented_shape(5)
        handle._runtime._agent = "kirocrew-lite"
        assert read_effective_agent(source) == "kirocrew-lite"

    def test_session_model_still_outranks_the_runtime_model_when_wrapped(self):
        # _runtime is walked after the whole _handle subtree, so the process-level
        # --model argument never outranks the session handle's own model, no
        # matter how many wrappers sit above both.
        source, handle = _documented_shape(4, model="claude-opus-5")
        handle._runtime._model = "claude-haiku-4.5"
        assert read_effective_model(source) == "claude-opus-5"

    def test_runtime_reached_from_a_shallow_wrapper_does_not_preempt_the_handle(self):
        # A wrapper holding both a runtime and a deeper session provider must
        # follow _handle to the bottom before it reads the runtime.
        runtime = _Node("runtime")
        runtime._model = "process-level-arg"
        handle = _Node("handle", _runtime=runtime)
        handle._model = "claude-opus-5"
        session_provider = _Node("session_provider", _handle=handle)
        wrapper = _Node("wrapper", _client=session_provider, _runtime=runtime)
        assert read_effective_model(wrapper) == "claude-opus-5"


def _default_model_session(extra_layers: int, *, resolved: str = "auto") -> object:
    """The chain a kiro session on the default model really produces.

    Built on the real classes, with no constructor run: the start path skips
    ``set_model`` for the default model, so the handle's ``_model`` stays
    ``""``, and ``session/new`` stores the backend's ``currentModelId`` as
    ``_resolved_model_id``. ``extra_layers`` wrappers sit on top, as in
    :func:`_documented_shape`.
    """
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    runtime = object.__new__(AcpRuntime)
    handle = object.__new__(AcpSessionHandle)
    handle._runtime = runtime
    handle._model = ""
    handle._resolved_model_id = resolved
    session_provider = object.__new__(AcpSessionProvider)
    session_provider._handle = handle
    session_provider._runtime = runtime
    provider = object.__new__(AcpProvider)
    provider._client = session_provider
    node: object = provider
    for i in range(extra_layers):
        node = _Node(
            f"wrapper{i}",
            client=_Node(f"a{i}"),
            _client=_Node(f"b{i}"),
            _handle=node,
            _runtime=_Node(f"c{i}"),
        )
    return node


class TestBackendReportedAutoIsAttributed:
    """A default-model session's rows and footer read ``auto``, not blank."""

    @pytest.mark.parametrize("extra_layers", [0, 1, 4])
    def test_backend_reported_auto_resolves_auto(self, extra_layers):
        source = _default_model_session(extra_layers)
        assert read_effective_model(source) == "", "auto is never a concrete id"
        assert _resolve_model("", source) == "auto"

    @pytest.mark.parametrize("resolved", ["AUTO", " auto "])
    def test_backend_reported_auto_is_case_and_space_insensitive(self, resolved):
        assert _resolve_model("", _default_model_session(1, resolved=resolved)) == "auto"

    def test_footer_reads_auto_for_the_default_model_session(self):
        assert read_turn_model(_default_model_session(0)) == "auto"

    @pytest.mark.parametrize("extra_layers", [0, 4])
    def test_blank_request_and_blank_resolved_id_stay_blank(self, extra_layers):
        source = _default_model_session(extra_layers, resolved="")
        assert read_turn_model(source) == ""
        assert _resolve_model("", source) == ""

    def test_concrete_resolved_id_still_wins_over_auto(self):
        source = _default_model_session(2, resolved="claude-opus-5")
        outer = _Node("outer", _handle=source)
        outer._resolved_model_id = "auto"
        assert read_turn_model(outer) == "claude-opus-5"
        assert _resolve_model("", outer) == "claude-opus-5"


class TestWalkTerminates:
    """Cycle safety and the runaway guard."""

    def test_self_referential_wrapper_terminates(self):
        node = _Node("loop")
        node._client = node
        node.client = node
        node._model = "claude-opus-5"
        assert [n for n in _wrapper_chain(node)] == [node]
        assert read_effective_model(node) == "claude-opus-5"

    def test_mutual_cycle_terminates_with_each_node_once(self):
        a = _Node("a")
        b = _Node("b", _handle=a)
        a._client = b
        a._runtime = b
        chain = _wrapper_chain(a)
        assert len(chain) == 2
        assert chain[0] is a and chain[1] is b

    def test_shared_runtime_is_visited_once(self):
        # Both the handle and the session provider hold the SAME runtime
        # (session_handle_class.py / session_provider.py); it must not be double-counted.
        source, handle = _documented_shape(2)
        chain = _wrapper_chain(source)
        assert sum(1 for n in chain if n is handle._runtime) == 1
        assert len(chain) == len({id(n) for n in chain})

    def test_attribute_synthesizing_source_hits_the_guard_and_reports_blank(self):
        # A MagicMock answers every getattr with a fresh child, so without the
        # guard the walk would never end. It stops, and claims no model.
        source = MagicMock()
        chain = _wrapper_chain(source)
        assert len(chain) == usage_mod._WRAPPER_CHAIN_MAX_NODES
        assert read_effective_model(source) == ""
        assert read_turn_model(source) == ""

    def test_guard_is_far_above_the_documented_shape(self):
        # The guard is a runaway stop, not a depth limit: the documented shape
        # with four extra layers uses a fraction of it.
        source, _handle = _documented_shape(4)
        assert len(_wrapper_chain(source)) < usage_mod._WRAPPER_CHAIN_MAX_NODES // 2


class TestBlankModelBilledRowIsLoud:
    """A non-zero charge with no model cannot be persisted silently."""

    @staticmethod
    def _shard_dir(monkeypatch, tmp_path):
        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
        return shard_dir

    @staticmethod
    def _rows(shard_dir):
        return [
            json.loads(line)
            for shard in sorted(shard_dir.glob("*.jsonl"))
            for line in shard.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _attribution_warnings(caplog):
        return [
            r
            for r in caplog.records
            if r.name == usage_mod.__name__
            and r.levelno == logging.WARNING
            and "model attribution failed" in r.getMessage()
        ]

    def test_billed_row_with_blank_model_is_persisted_and_logged(
        self, tmp_path, monkeypatch, caplog
    ):
        shard_dir = self._shard_dir(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record(
                "chat-synthetic",
                "",
                SimpleNamespace(credits=12.0),
                provider="acp",
                surface="dashboard",
                model_source=None,
            )
        rows = self._rows(shard_dir)
        assert len(rows) == 1 and rows[0]["credits"] == 12.0, rows
        assert rows[0]["model"] == "", "the charge is real; the row must still be written"
        warnings = self._attribution_warnings(caplog)
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
        message = warnings[0].getMessage()
        assert "slot=chat-synthetic" in message
        assert "surface=dashboard" in message
        assert "provider=acp" in message
        assert "credits=12.0" in message

    def test_cost_only_billing_is_also_loud(self, tmp_path, monkeypatch, caplog):
        self._shard_dir(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record(
                "slot", "", SimpleNamespace(cost_usd=0.03), provider="claude_code", surface="cron"
            )
        assert len(self._attribution_warnings(caplog)) == 1

    @pytest.mark.parametrize(
        "usage",
        [
            SimpleNamespace(input_tokens=1200),
            SimpleNamespace(output_tokens=40),
            SimpleNamespace(cache_creation_tokens=900),
            SimpleNamespace(cache_read_tokens=3000),
        ],
        ids=["input", "output", "cache_create", "cache_read"],
    )
    def test_token_only_billing_is_also_loud(self, tmp_path, monkeypatch, caplog, usage):
        # The claude seam bills in tokens with cost still 0 on most turns and
        # credits 0 off the kiro path: every dimension usage_has_billing
        # accepts must count here too, or exactly those rows stay silent.
        self._shard_dir(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record("slot", "", usage, provider="claude_code", surface="dashboard")
        warnings = self._attribution_warnings(caplog)
        assert len(warnings) == 1
        assert "tokens=" in warnings[0].getMessage()

    def test_record_predicate_agrees_with_usage_has_billing_on_every_dimension(self):
        # `_record_is_billed` is a copy of `usage_has_billing` spelled in record
        # field names. This pins the copy to the original across EVERY TurnUsage
        # field: a dimension the original bills in must bill here too, and one
        # it ignores (num_turns, duration_ms) must stay free here.
        from dataclasses import fields

        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.llm_helpers import usage_has_billing

        now = datetime.now().astimezone()
        for field in fields(TurnUsage):
            usage = TurnUsage(**{field.name: 1})
            record = usage_mod._build_token_record(
                "slot", "", SimpleNamespace(usage=usage), "acp", now
            )
            assert usage_mod._record_is_billed(record) == usage_has_billing(usage), field.name

    def test_attributed_row_is_quiet(self, tmp_path, monkeypatch, caplog):
        self._shard_dir(monkeypatch, tmp_path)
        source, _handle = _documented_shape(4, model="claude-opus-5")
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record(
                "slot", "", SimpleNamespace(credits=12.0), provider="acp", model_source=source
            )
        assert self._attribution_warnings(caplog) == []

    @pytest.mark.parametrize(
        "credits",
        [
            # A timeout that billed nothing is not an attribution failure.
            pytest.param(0.0, id="test_free_row_with_blank_model_is_quiet"),
            # A NaN credit is a corrupt measurement (the sanitizer's warning
            # names it), not a charge to attribute.
            pytest.param(math.nan, id="test_non_finite_charge_is_not_treated_as_billing"),
        ],
    )
    def test_unbilled_row_with_blank_model_is_quiet(self, tmp_path, monkeypatch, caplog, credits):
        self._shard_dir(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record("slot", "", SimpleNamespace(credits=credits), provider="acp")
        assert self._attribution_warnings(caplog) == []

    def test_write_site_covers_every_persist_path(self, tmp_path, monkeypatch, caplog):
        # The check lives on the shared write, so the sync and async variants
        # cannot drift apart.
        self._shard_dir(monkeypatch, tmp_path)
        record = usage_mod._build_token_record(
            "slot", "", SimpleNamespace(credits=3.0), "acp", datetime.now().astimezone()
        )
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            usage_mod._write_token_record(record, datetime.now().astimezone())
        assert len(self._attribution_warnings(caplog)) == 1

    def test_deep_wrapping_end_to_end_names_the_model(self, tmp_path, monkeypatch):
        # Model "" from the caller, four extra wrapper layers on model_source,
        # a real charge: the row names the model.
        shard_dir = self._shard_dir(monkeypatch, tmp_path)
        source, _handle = _documented_shape(4, model="gpt-5.6-luna")
        persist_token_record(
            "chat-1", "", SimpleNamespace(credits=7.5), provider="acp", model_source=source
        )
        rows = self._rows(shard_dir)
        assert rows[0]["model"] == "gpt-5.6-luna"
        assert rows[0]["credits"] == 7.5


class TestBillingStatsHolderWalkReachesTheRunner:
    """The sibling walk in ``llm_helpers`` follows the same discipline."""

    @staticmethod
    def _deep_provider(extra_layers: int) -> tuple[object, object]:
        # The documented billing shape: acp provider -> _client -> session
        # provider -> _handle -> runner (holds last_prompt_stats), plus wrappers
        # each carrying every holder the walk follows, siblings holding nothing.
        runner = _Node("runner")
        runner.last_prompt_stats = SimpleNamespace(credits=4.0)
        session_provider = _Node("session_provider", _handle=runner)
        node: object = _Node("provider", _client=session_provider)
        for i in range(extra_layers):
            node = _Node(
                f"wrapper{i}",
                _client=node,
                _handle=_Node(f"h{i}"),
                _sess=_Node(f"s{i}"),
                provider=_Node(f"p{i}"),
            )
        return node, runner

    @pytest.mark.parametrize("extra_layers", [0, 2, 4, 12])
    def test_runner_is_reached_through_every_layer_count(self, extra_layers):
        from kiro_crew.llm_helpers import _billing_stat_holders, _billing_stats

        source, runner = self._deep_provider(extra_layers)
        assert any(node is runner for node in _billing_stat_holders(source))
        assert _billing_stats(source) is runner.last_prompt_stats

    def test_nearest_wrapper_is_still_first(self):
        from kiro_crew.llm_helpers import _billing_stat_holders

        source, _runner = self._deep_provider(3)
        assert _billing_stat_holders(source)[0] is source

    def test_cycle_terminates_with_each_node_once(self):
        from kiro_crew.llm_helpers import _billing_stat_holders

        a = _Node("a")
        b = _Node("b", provider=a)
        a._sess = b
        a._client = b
        chain = _billing_stat_holders(a)
        assert len(chain) == 2 and chain[0] is a and chain[1] is b

    def test_attribute_synthesizing_source_hits_the_guard(self):
        from kiro_crew import llm_helpers

        chain = llm_helpers._billing_stat_holders(MagicMock())
        assert len(chain) == llm_helpers._WRAPPER_WALK_MAX_NODES
