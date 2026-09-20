"""Assign spine stages to persistent members without changing acceptance gates."""

from __future__ import annotations

import asyncio
import time
import uuid

from kiro_crew.agent_discovery import _read_agent_spec, spec_str
from kiro_crew.agent_spec_format import is_agent_spec_name, is_markdown_spec, spec_stem
from kiro_crew.config.loader import _session_work_dir
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import HOOK_REPLY
from kiro_crew.members import record_activity
from kiro_crew.platform.context import redact_via_context
from kiro_crew.workflows.registry import _await_owned

from ..backend import crew, store
from .agent_runner import AgentResult, SessionAgentRunner


async def _io(function, *args, **kwargs):
    """Finish owned disk work before cancellation can retire its session."""
    return await _await_owned(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))


def _require_unshadowed_templates(cwd: str) -> None:
    """Refuse checkout-supplied grants before allocating a member provider."""
    agents = project_agents_dir(cwd)
    protected = {role.template for role in crew.ROLES.values()}
    try:
        # Only confirmed absence is safe. Stat both components so a dangling
        # .kiro link cannot look like an absent agents directory.
        for directory in (agents.parent, agents):
            try:
                directory.lstat()
            except FileNotFoundError:
                return
            directory.stat()
        # Discovery's glob scan suppresses errors and omits native skill-view
        # filenames. The native CLI can still load those files by declared name.
        for path in agents.iterdir():
            if not is_agent_spec_name(path.name):
                continue
            if is_markdown_spec(path):
                try:
                    path.with_suffix(".json").lstat()
                except FileNotFoundError:
                    pass
                else:
                    continue  # The shared resolver gives the JSON twin precedence.
            data = _read_agent_spec(path, operation="auto_improvement_assignment", source="unknown")
            if data is None:
                raise ValueError(f"Cannot verify project agent spec {path}")
            name = spec_str(data, "name", spec_stem(path.name)) or spec_stem(path.name)
            if name in protected:
                raise ValueError(
                    f"Project agent spec {path} shadows Auto-Improvement template {name!r}; "
                    "rename or remove the project spec before running an assignment"
                )
    except OSError as exc:
        raise ValueError(f"Cannot verify project agent directory {agents}: {exc}") from exc


class CrewRunner:
    def __init__(self, runtime, identities, *, stop_check=None, on_activity=None):
        self._runners = {
            role: MemberSessionRunner(
                runtime,
                role,
                identity,
                stop_check=stop_check,
                on_activity=on_activity,
            )
            for role, identity in identities.items()
        }

    def for_role(self, role: str) -> MemberSessionRunner:
        return self._runners[role]

    def run(self, prompt: str, **kwargs) -> AgentResult:
        return self.for_role("discovery").run(prompt, **kwargs)

    def total_cost_usd(self) -> float:
        return sum(runner.total_cost_usd() for runner in self._runners.values())

    def ensure_agent_registered(self) -> bool:
        from kiro_crew.agent_discovery import list_agents

        owned = {
            agent.name
            for agent in list_agents()
            if agent.filename.startswith(f"{store.APP_NAME}--")
        }
        return all(spec.template in owned for spec in crew.ROLES.values())


class MemberSessionRunner(SessionAgentRunner):
    def __init__(self, runtime, role, identity, **kwargs):
        super().__init__(agent_name=crew.ROLES[role].template, **kwargs)
        self._runtime = runtime
        self.role = role
        self.member_id = identity

    def _allows_tool(self, event: object, tool: str, allowed: list[str] | None) -> bool:
        if getattr(event, "mcp_server_name", ""):
            return (
                allowed != []
                and getattr(event, "mcp_identity_trusted", False)
                and getattr(event, "mcp_server_name", "") == "kirocrew-core"
                and getattr(event, "tool_name", "") in {"memory_recall", "learn_add"}
            )
        return super()._allows_tool(event, tool, allowed)

    def run(
        self,
        prompt: str,
        *,
        cwd=None,
        allowed_tools=None,
        append_system=None,
        max_turns=40,
        timeout_s=None,
        add_dirs=None,
    ) -> AgentResult:
        timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
        t0 = time.monotonic()
        if not self._runtime.loop.is_running():
            return AgentResult(ok=False, error="member gateway is unavailable")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._runtime.loop:
            raise RuntimeError("The synchronous improvement driver must run off the gateway loop")
        future = asyncio.run_coroutine_threadsafe(
            self._bounded_assignment(
                prompt,
                cwd=cwd,
                allowed_tools=allowed_tools,
                append_system=append_system,
                max_turns=max_turns,
                timeout_s=timeout_s,
                t0=t0,
            ),
            self._runtime.loop,
        )
        try:
            return future.result(timeout=timeout_s + 60)
        except Exception as exc:
            future.cancel()
            return AgentResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - t0,
            )

    async def _bounded_assignment(self, prompt, **kwargs):
        task = asyncio.create_task(self._assignment(prompt, **kwargs))
        try:
            while not task.done():
                remaining = kwargs["timeout_s"] - (time.monotonic() - kwargs["t0"])
                if remaining <= 0:
                    raise TimeoutError(f"timeout after {kwargs['timeout_s']}s")
                if self._stop_check is not None and self._stop_check():
                    raise RuntimeError("stopped by request")
                await asyncio.wait({task}, timeout=min(0.25, remaining))
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _assignment(self, prompt, **kwargs):
        config, name, _member = await _io(crew.resolve_role, self.role, self.member_id)
        execution = resolve_member_execution(config, name, app=store.APP_NAME)
        key = f"auto-improvement-{uuid.uuid4().hex}"
        # Pin the same default the provider factory uses, so omitted cwd cannot
        # bypass admission or resolve to a different workspace after the check.
        cwd = kwargs["cwd"] or await _io(_session_work_dir, key)
        kwargs["cwd"] = str(cwd)
        await _io(_require_unshadowed_templates, kwargs["cwd"])
        log = ConversationLog()
        await _io(bind_session_execution, key, execution)
        await _io(
            log.update_metadata,
            key,
            {
                "agent": name,
                "app": store.APP_NAME,
                "title": f"{name}: {self.role}",
                "project_dir": kwargs["cwd"] or "",
            },
        )
        sessions = self._runtime.sessions
        provider = None
        result = AgentResult(ok=False, error="assignment interrupted")
        try:
            await self._runtime.context_builder.ensure_store(execution.store.store_id)
            provider, is_new, resumed = await sessions.get_or_create(
                key,
                agent=execution.template_id,
                crew_agent=name,
                cwd=kwargs["cwd"],
            )
            full_prompt, hook = await _io(
                self._runtime.context_builder.build_message,
                prompt,
                is_new,
                session_key=key,
                agent=execution.template_id,
                resumed=resumed,
                interactive=False,
                project=kwargs["cwd"],
                memory_store=execution.store.store_id,
                member=name,
                execution_context=execution,
                request_prefix_context=kwargs["append_system"],
            )
            logged_prompt = await _io(redact_via_context, prompt)
            await _io(log.append, key, "user", logged_prompt, agent=name)
            await _io(
                record_activity,
                name,
                key,
                execution.memory_mode,
                project=kwargs["cwd"] or "",
                via="auto-improvement",
                dedupe_session=True,
            )
            self._emit_activity(
                {
                    "kind": "text",
                    "detail": f"{name} started {self.role}",
                    "member": name,
                    "member_id": self.member_id,
                    "session": key,
                },
            )
            if hook is not None and hook.action == HOOK_REPLY:
                result = AgentResult(ok=False, text=hook.text, error="assignment handled by a hook")
            else:
                result = await super()._run_async(
                    full_prompt,
                    factory=None,
                    provider=provider,
                    session_key=key,
                    cwd=kwargs["cwd"],
                    append_system=None,
                    timeout_s=kwargs["timeout_s"],
                    t0=kwargs["t0"],
                    max_turns=kwargs["max_turns"],
                    allowed_tools=kwargs["allowed_tools"],
                )
            logged_text = await _io(redact_via_context, result.text)
            await _io(log.append, key, "assistant", logged_text, agent=name)
            return result
        except Exception as exc:
            result = AgentResult(ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:

            async def finish():
                try:
                    logged_error = await _io(redact_via_context, result.error)
                    await _io(
                        log.update_metadata,
                        key,
                        {
                            "auto_improvement": {
                                "role": self.role,
                                "ok": result.ok,
                                "error": logged_error,
                            }
                        },
                    )
                    self._emit_activity(
                        {
                            "kind": "text",
                            "detail": f"{name} finished {self.role}: "
                            + ("completed" if result.ok else logged_error),
                            "member": name,
                            "member_id": self.member_id,
                            "session": key,
                        },
                    )
                finally:
                    if provider is not None:
                        sessions.release(key)
                        await sessions.remove(key)

            await _await_owned(asyncio.create_task(finish()))
