"""Narrow replaceable seams around the Kiro BYO Turn Engine.

This is intentionally *not* an everything-is-a-plugin framework.  The Turn
Engine depends on a handful of stable capabilities; the default implementations
adapt today's router/provider/tool/session code to those contracts.  Tests or
future runtimes can replace one seam without teaching the core about provider
brands or product Mode/Type names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from .mode_router import route_collaboration_model, route_model
from .session_events import SessionProjection as EventProjection, derive_session_projection


class ModelRouter(Protocol):
    def route(
        self,
        config: Any,
        *,
        policy: str | None,
        pack: str | None,
        prompt: str,
        history: Sequence[dict],
        has_image: bool,
        tools_required: bool | None,
        tools_available: bool | None,
        reasoning_effort: str,
    ) -> Any: ...

    def collaboration(
        self,
        config: Any,
        intent: Any,
        signals: Any,
        *,
        leader_provider: Any,
        leader_model: Any,
        excluded_models: set[tuple[str, str]] | None = None,
    ) -> Any: ...


class ProviderInvoker(Protocol):
    def invoke(self, session: Any, **kwargs: Any) -> Any: ...


class ToolExecutor(Protocol):
    def execute(self, session: Any, **kwargs: Any) -> Any: ...


class SessionProjection(Protocol):
    def snapshot(self, session: Any) -> EventProjection: ...


class PostStageRunner(Protocol):
    def run(self, session: Any, **kwargs: Any) -> Any: ...


class ReasoningResolver(Protocol):
    def resolve(
        self,
        model: Any,
        session: Any,
        reasoning_profile: Any,
        signals: Any = None,
        *,
        role: str = "lead",
        stage: str = "turn",
    ) -> tuple[str, str]: ...


class DefaultModelRouter:
    def route(self, config: Any, **kwargs: Any) -> Any:
        return route_model(config, **kwargs)

    def collaboration(
        self,
        config: Any,
        intent: Any,
        signals: Any,
        *,
        leader_provider: Any,
        leader_model: Any,
        excluded_models: set[tuple[str, str]] | None = None,
    ) -> Any:
        return route_collaboration_model(
            config,
            intent,
            signals,
            leader_provider=leader_provider,
            leader_model=leader_model,
            excluded_models=excluded_models,
        )


class EventSessionProjection:
    def snapshot(self, session: Any) -> EventProjection:
        events = getattr(session, "events", ())
        return derive_session_projection(events)


@dataclass(frozen=True)
class BoundProviderInvoker:
    invoke_fn: Callable[..., Any]

    def invoke(self, session: Any, **kwargs: Any) -> Any:
        return self.invoke_fn(session, **kwargs)


@dataclass(frozen=True)
class BoundToolExecutor:
    execute_fn: Callable[..., Any]

    def execute(self, session: Any, **kwargs: Any) -> Any:
        return self.execute_fn(session, **kwargs)


@dataclass(frozen=True)
class BoundPostStageRunner:
    run_fn: Callable[..., Any]

    def run(self, session: Any, **kwargs: Any) -> Any:
        return self.run_fn(session, **kwargs)


@dataclass(frozen=True)
class BoundReasoningResolver:
    resolve_fn: Callable[..., tuple[str, str]]

    def resolve(
        self,
        model: Any,
        session: Any,
        reasoning_profile: Any,
        signals: Any = None,
        *,
        role: str = "lead",
        stage: str = "turn",
    ) -> tuple[str, str]:
        return self.resolve_fn(
            model,
            session,
            reasoning_profile,
            signals,
            role=role,
            stage=stage,
        )


@dataclass(frozen=True)
class RuntimeSeams:
    model_router: ModelRouter
    provider_invoker: ProviderInvoker
    tool_executor: ToolExecutor
    session_projection: SessionProjection
    post_stage_runner: PostStageRunner
    reasoning_resolver: ReasoningResolver
