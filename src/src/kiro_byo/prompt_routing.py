"""Mode-neutral prompt routing preparation for the ACP Turn Engine.

These helpers prepare the selected provider and execution plan; the owning
TurnRunnerMixin still controls session locking, durable admission, Trace
emission, and the provider/tool execution loop. Do not start tools here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agent_runtime import ExecutionPlan
from .mode_router import ReviewTarget, build_execution_plan, infer_tools_required
from .policy_packs import runtime_policy_context_for_request
from .reasoning_control import resolve_thinking_mode
from .session_events import append_lifecycle, set_route_pin


@dataclass(frozen=True)
class _PreparedPromptRoute:
    """Selection facts needed by durable admission and the provider tool loop."""

    provider: Any
    model: Any
    selection_truth: dict[str, Any]
    mode_fallbacks: list[tuple[Any, Any]] = field(default_factory=list)
    post_stage: ReviewTarget | None = None
    primary_effort: str = ""
    primary_thinking: str = ""
    reasoning_profile: Any = None
    routing_signals: Any = None
    runtime_mode_context: str = ""
    relay: Any = None
    execution_plan: ExecutionPlan | None = None
    decision: Any = None


class PromptRoutingMixin:
    def _restore_prompt_type_pin(self, session: Any, decision: Any, provider: Any, model: Any) -> tuple[Any, Any, Any]:
        """Respect a sticky pin only inside this turn's eligible fallback pool."""
        pin_key = f"{decision.pack}:{decision.policy}"
        pin_restored = False
        if decision.execution_intent.sticky:
            pins = self._runtime_seams().session_projection.snapshot(session).route_pins
            pinned = pins.get(pin_key) if isinstance(pins, dict) else None
            if pinned:
                # Keep a long task on the model it started with, as long
                # as that model is still eligible for this turn.
                # A historic sticky pin is valid only inside this turn's
                # executable tier/capability pool. candidates also includes
                # out-of-tier models for diagnostics and optional reviewers.
                for row in decision.execution_fallbacks:
                    if (row.provider.id, row.model.model_id) == pinned:
                        provider, model = row.provider, row.model
                        pin_restored = True
                        break
            if isinstance(pins, dict):
                set_route_pin(
                    session, pin_key, provider.id, model.model_id, reason="sticky_policy"
                )
        if pin_restored or provider is not decision.provider or model is not decision.model:
            from dataclasses import replace as _dc_replace

            decision = _dc_replace(
                decision, provider=provider, model=model,
                selection_source="sticky_pin" if pin_restored else "policy_ranked",
            )
        return decision, provider, model

    def _select_type_prompt_route(
        self, req_id: Any, session_id: str, session: Any, config: Any,
        text: str, has_image: bool, selection_truth: dict[str, Any], selected_type: str,
    ) -> _PreparedPromptRoute | None:
        """Plan a real Type route using the existing model-router capability seam."""
        # Tool visibility and current-turn tool need are independent. A
        # Personal Agent may expose many tools while the user only says
        # "hello"; that must not exclude otherwise valid models.
        route_tools = self._active_tools_for_turn(session)
        tools_available = bool(route_tools)
        tools_required = infer_tools_required(
            text, tools=route_tools, history=self._runtime_seams().session_projection.snapshot(session).messages
        )
        decision = self._runtime_seams().model_router.route(
            config,
            pack=config.mode_pack,
            policy=selected_type,
            prompt=text,
            history=self._runtime_seams().session_projection.snapshot(session).messages,
            has_image=has_image,
            tools_required=tools_required,
            tools_available=tools_available,
            # Type effort is chosen only after a concrete model is selected.
            # Never let a stale/manual session level filter the Type pool.
            reasoning_effort="",
        )
        if decision is None:
            self._runtime_event(
                "model.route.failed", session,
                data={"pack": config.mode_pack, "policy": selected_type, "reason": "no eligible model"},
            )
            self.emit_runtime_error(session_id, "Kiro BYO mode could not find an eligible configured model for this turn")
            self.result(req_id, {"stopReason": "end_turn"})
            return None
        provider, model = decision.provider, decision.model
        execution_intent = decision.execution_intent
        runtime_mode_context = runtime_policy_context_for_request(
            decision.pack, decision.policy, text, active=True
        )
        routing_signals = decision.signals
        decision, provider, model = self._restore_prompt_type_pin(
            session, decision, provider, model,
        )
        mode_fallbacks = [
            (row.provider, row.model) for row in decision.execution_fallbacks
            if (row.provider.id, row.model.model_id) != (provider.id, model.model_id)
        ]
        # AUTO thinking and AUTO effort are independent policy axes.
        # The concrete model's capabilities and explicit user locks are
        # resolved before anything reaches a provider adapter.
        reasoning_profile = execution_intent.reasoning
        primary_thinking, primary_effort = self._runtime_seams().reasoning_resolver.resolve(
            model, session, reasoning_profile, decision.signals,
            role="lead", stage="planning",
        )
        execution_plan = build_execution_plan(decision)
        relay = None
        if execution_plan.worker is not None:
            teammate = self._relay_teammate(
                decision, provider, model, config=config, signals=decision.signals
            )
            if teammate is not None:
                # The tuple remains a compatibility carrier for the
                # currently routed worker. The plan, not a Type-specific
                # branch, decides whether this role exists.
                relay = (teammate[0], teammate[1], "")
        review_target = execution_plan.post_stage
        post_stage = review_target
        append_lifecycle(
            session,
            "model/selected",
            {
                "pack": decision.pack,
                "policy": decision.policy,
                "provider_id": provider.id,
                "model": model.model_id,
                "execution_mode": execution_plan.mode,
                "selection_kind": selection_truth["kind"],
                "selection_source": selection_truth["source"],
                "routing_applied": True,
            },
        )
        return _PreparedPromptRoute(
            provider=provider, model=model, selection_truth=selection_truth,
            mode_fallbacks=mode_fallbacks, post_stage=post_stage,
            primary_effort=primary_effort, primary_thinking=primary_thinking,
            reasoning_profile=reasoning_profile, routing_signals=routing_signals,
            runtime_mode_context=runtime_mode_context, relay=relay,
            execution_plan=execution_plan, decision=decision,
        )

    def _select_concrete_prompt_route(
        self, req_id: Any, session_id: str, session: Any, config: Any,
        text: str, selection_truth: dict[str, Any],
    ) -> _PreparedPromptRoute | None:
        """Concrete selections bypass Type policy while honoring model preferences."""
        resolved = self._resolved_model(config, session)
        if resolved is None:
            self.emit_runtime_error(
                session_id,
                f"model {session.model!r} is no longer present in config/providers.json",
            )
            self.result(req_id, {"stopReason": "end_turn"})
            return None
        provider, model = resolved
        runtime_mode_context = runtime_policy_context_for_request(
            config.mode_pack, config.mode_type, text, active=False
        )
        # A concrete model selection still obeys that model's persistent
        # BYO Auto/On/Off preference. With no Type suggestion, Auto
        # preserves the verified model/provider default.
        primary_thinking = resolve_thinking_mode(
            model, getattr(model, "thinking_preference", "auto"), ""
        )
        append_lifecycle(
            session,
            "model/selected",
            {
                "pack": "",
                "policy": "concrete",
                "provider_id": provider.id,
                "model": model.model_id,
                "execution_mode": "single_pass",
                "selection_kind": selection_truth["kind"],
                "selection_source": selection_truth["source"],
                "routing_applied": False,
            },
        )
        self._runtime_event(
            "model.route.bypassed", session,
            data={
                "requested_model": session.model,
                "selection": selection_truth,
                "provider_id": provider.id,
                "resolved_model": model.model_id,
                "reason": "concrete model selected",
            },
            content={"prompt": text},
        )
        return _PreparedPromptRoute(
            provider=provider, model=model, selection_truth=selection_truth,
            runtime_mode_context=runtime_mode_context,
            primary_thinking=primary_thinking,
        )

    def _prepare_prompt_routing(
        self, req_id: Any, session_id: str, session: Any, config: Any,
        text: str, has_image: bool,
    ) -> _PreparedPromptRoute | None:
        """Choose a Type plan or concrete model, without starting a durable run."""
        # Tool visibility is projected by Crew at session creation. On a
        # cold interactive browser turn, briefly join the MCP warmup so the
        # first model round can see the actual Crew browser schema.
        self._prepare_crew_browser_for_turn(session, text)
        self._sync_host_capabilities(session)
        selection_truth = self._ensure_selection_truth(session, config)
        session.response_language_preference = getattr(config, "response_language", "auto")
        session.ui_language_preference = getattr(config, "ui_language", "auto")
        self._apply_reasoning_language_policy(
            session, getattr(config, "reasoning_language", "auto"), text
        )
        selected_type = self._type_for_selection(config, session.model)
        if selected_type is not None:
            return self._select_type_prompt_route(
                req_id, session_id, session, config, text, has_image,
                selection_truth, selected_type,
            )
        return self._select_concrete_prompt_route(
            req_id, session_id, session, config, text, selection_truth,
        )

