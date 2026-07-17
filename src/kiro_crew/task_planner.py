"""Task planning — decomposition, task parsing, grouping, and plan management."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import TOOL_DENY
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.sel import sel
from kiro_crew.task_models import (
    SESSION_PREFIX,
    Project,
    Task,
    TaskStatus,
)

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)


# ── Name Resolution ──


def auto_name(spec_content: str, spec_path: str = "") -> str:
    """Derive a human-readable name from spec content."""
    lines = [line.strip() for line in spec_content.splitlines()]
    for line in lines:
        if line.startswith("# Task:"):
            return line[7:].strip()[:60]
    for line in lines:
        if line.startswith("# ") and not line.startswith("# #"):
            return line[2:].strip()[:60]
    if spec_path:
        return Path(spec_path).stem.replace("_", " ").title()[:60]
    # Fallback: first non-empty line, trimmed to word boundary
    for line in lines:
        if line and not line.startswith(("#", "---", "```")):
            if len(line) <= 60:
                return line
            cut = line[:60].rsplit(" ", 1)[0] or line[:60]
            return cut + "…"
    return ""


# ── Parallel Task Grouping ──


def group_parallel_tasks(
    tasks: list[Task],
    completed_indices: set[int] | None = None,
) -> list[list[Task]]:
    """Group tasks into sequential batches; independent tasks run in parallel."""
    if not tasks:
        return []

    groups: list[list[Task]] = []
    done: set[int] = set(completed_indices) if completed_indices else set()

    remaining = list(tasks)
    while remaining:
        ready: list[Task] = []
        blocked: list[Task] = []
        for task in remaining:
            if all(d in done for d in task.depends_on):
                ready.append(task)
            else:
                blocked.append(task)

        if not ready:
            for task in blocked:
                groups.append([task])
            break

        groups.append(ready)
        for task in ready:
            done.add(task.index)
        remaining = blocked

    return groups


# ── Dependency Normalization ──


def normalize_cross_group_deps(tasks: list[Task]) -> list[Task]:
    """Ensure cross-group deps are complete: if a task depends on any task
    in a prior group, it must depend on ALL tasks in that group.

    Only used for LLM-generated plans. User edits via update_plan_tasks
    bypass this to respect explicit dependency choices."""
    if not tasks:
        return tasks

    # Build groups via the same algorithm used at runtime
    groups: list[set[int]] = []
    done: set[int] = set()
    remaining = list(tasks)
    while remaining:
        ready = [t for t in remaining if all(d in done for d in t.depends_on)]
        if not ready:
            for t in remaining:
                groups.append({t.index})
            break
        groups.append({t.index for t in ready})
        done.update(t.index for t in ready)
        remaining = [t for t in remaining if t.index not in done]

    # Map each task index to its group index
    idx_to_group: dict[int, int] = {}
    for gi, g in enumerate(groups):
        for idx in g:
            idx_to_group[idx] = gi

    # Normalize: expand partial cross-group deps to full group deps
    for task in tasks:
        if not task.depends_on:
            continue
        dep_groups: set[int] = set()
        for d in task.depends_on:
            if d in idx_to_group:
                dep_groups.add(idx_to_group[d])
        full_deps: set[int] = set()
        for gi in dep_groups:
            full_deps.update(groups[gi])
        task.depends_on = sorted(full_deps)

    return tasks


# ── Task Parsing ──


def parse_tasks(text: str) -> list[Task]:
    """Parse LLM output into Task objects.

    Accepts either:
    - A JSON object with "steps" key (preferred)
    - A plain JSON array of tasks (backward compat)
    """
    text = text.strip()
    if not text:
        return []

    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse tasks JSON: %.200s", text)
        return []

    if isinstance(parsed, dict):
        data = parsed.get("tasks", parsed.get("steps", []))
    elif isinstance(parsed, list):
        data = parsed
    else:
        return []

    if not isinstance(data, list):
        return []

    tasks: list[Task] = []
    for i, item in enumerate(data, 1):
        if isinstance(item, dict) and "title" in item:
            deps = item.get("depends_on", [])
            if not isinstance(deps, list):
                deps = []
            valid_deps = [int(d) for d in deps if isinstance(d, (int, float)) and 0 < int(d) < i]
            tasks.append(
                Task(
                    index=i,
                    title=str(item["title"]),
                    description=str(item.get("description", item["title"])),
                    requires_approval=bool(item.get("requires_approval")),
                    force_approval=bool(item.get("force_approval")),
                    depends_on=valid_deps,
                )
            )
    return normalize_cross_group_deps(tasks)


# ── LLM Decomposition ──


async def decompose(
    spec: str,
    sessions: SessionManager,
    ctx: ContextBuilder | None = None,
    work_dir: str = "",
    task_id: str = "",
    agent: str = "",
) -> list[Task]:
    """Use LLM to break a spec into ordered implementation tasks."""
    prompt = (
        "You are a task decomposition agent. Break this specification "
        "into concrete, ordered implementation steps. Each step should "
        "be a single, actionable code change or command.\n\n"
        f"Working directory: {work_dir}\n"
        "ALL files MUST be created in this directory. Use absolute paths.\n\n"
        "Rules:\n"
        "- Each step should be independently testable\n"
        "- Include file paths when applicable\n"
        "- Keep steps small and focused\n"
        "- Order by dependency (implement before test)\n"
        "- depends_on lists the step indices this step requires to be done first\n"
        "- A step can ONLY depend on earlier steps (lower indices)\n"
        "- Default to sequential: step N depends on step N-1 "
        "unless steps are truly independent\n"
        "- Only omit depends_on (or use []) for steps that can genuinely "
        "run in parallel\n"
        "- The LAST step must be the final verification/test step\n"
        "- Mark destructive steps with requires_approval: true\n"
        "- Mark steps that MUST block (even in auto-approve mode) with force_approval: true\n\n"
        "Return a JSON object:\n"
        '  "steps": array of step objects with keys:\n'
        '    "title": string, "description": string,\n'
        '    "depends_on": [step_indices] (optional, default []),\n'
        '    "requires_approval": bool (optional, default false),\n'
        '    "force_approval": bool (optional, default false — always blocks)\n\n'
        "Example:\n"
        '{"steps": [\n'
        '  {"title": "Create foo.py", "description": "Create...",'
        ' "depends_on": []},\n'
        '  {"title": "Add tests", "description": "Test...",'
        ' "depends_on": [1]}]}\n\n'
        "Respond with ONLY valid JSON, no markdown fences.\n\n"
        f"## Specification\n\n{spec}"
    )

    session_key = (
        f"{SESSION_PREFIX}:{task_id}:decompose" if task_id else f"{SESSION_PREFIX}:decompose"
    )
    try:
        client, is_new, _resumed = await sessions.get_or_create(session_key, agent=agent or None)
        if ctx:
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_prompt, _ = await run_in_embed_pool(
                ctx.build_message, prompt, is_new, session_key, agent=agent or None
            )
        else:
            full_prompt = prompt

        text = ""
        async for event in client.stream(full_prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # CSE SEC-006: gate decomposition-phase tool calls through the
                # same deny-list/hook check used during execution, instead of
                # unconditionally approving. A prompt-injection embedded in the
                # spec content could otherwise trigger dangerous tools (fs/exec)
                # during the planning phase, bypassing the execution-phase gate.
                if ctx is not None and getattr(ctx, "hooks", None) is not None:
                    hook_result = ctx.hooks.on_tool_call(
                        event.title,
                        session_key=session_key,
                        agent=agent,
                        tool_kind=event.tool_kind,
                        raw_params=event.raw_tool_params,
                    )
                    if hook_result.action == TOOL_DENY:
                        await client.reject_tool(event.request_id)
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=agent or "kirocrew",
                            source="taskrunner",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            request_id=event.request_id,
                            error="hook_deny",
                            metadata={"phase": "decomposition"},
                        )
                        continue
                else:
                    # Deny-by-default: with no hook store there is nothing to
                    # gate the request, so reject rather than fall through to
                    # approve. Decomposition normally only emits JSON (no tool
                    # calls), so this blocks only the anomalous/injection case.
                    await client.reject_tool(event.request_id)
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=agent or "kirocrew",
                        source="taskrunner",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="denied",
                        request_id=event.request_id,
                        error="no_hook_store",
                        metadata={"phase": "decomposition"},
                    )
                    continue
                await client.approve_tool(event.request_id)
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=agent or "kirocrew",
                    source="taskrunner",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="auto_approved",
                    request_id=event.request_id,
                    metadata={"phase": "decomposition"},
                )
            elif event.kind == EVENT_COMPLETE:
                break

        return parse_tasks(text)
    except Exception:
        logger.exception("Decomposition failed")
        return []
    finally:
        sessions.release(session_key)
        await sessions.reset(session_key)


# ── Plan Serialization ──


def plan_to_chat_context(run: Project) -> str:
    """Serialize a planned run for chat round-trip."""
    parts: list[str] = [f"# Task Plan: {run.task_id}\n"]

    if run.original_input:
        parts.append("## Original Input\n")
        parts.append(run.original_input[:3000] + "\n")

    if run.spec_content and run.spec_content != run.original_input:
        parts.append("## Spec\n")
        parts.append(run.spec_content[:3000] + "\n")

    # Grouped tasks
    completed = {t.index for t in run.tasks if t.status in (TaskStatus.PASSED, TaskStatus.SKIPPED)}
    groups = group_parallel_tasks(run.tasks, completed)
    parts.append("## Execution Plan\n")
    for gi, group in enumerate(groups, 1):
        label = f"### Group {gi}" + (" (parallel)" if len(group) > 1 else "")
        parts.append(label)
        for t in group:
            dep_str = (
                f" (depends on: {', '.join(str(d) for d in t.depends_on)})" if t.depends_on else ""
            )
            parts.append(f"{t.index}. **{t.title}**{dep_str}")
            if t.description and t.description != t.title:
                parts.append(f"   {t.description[:200]}")
        parts.append("")

    # JSON block for round-trip
    tasks_json = [
        {
            "title": t.title,
            "description": t.description,
            "depends_on": t.depends_on,
            "requires_approval": t.requires_approval,
            **({"force_approval": True} if t.force_approval else {}),
        }
        for t in run.tasks
    ]
    parts.append("## Tasks JSON\n")
    parts.append("```json")
    parts.append(json.dumps(tasks_json, indent=2))
    parts.append("```\n")
    parts.append(
        "Discuss the plan naturally. When you suggest changes, always include "
        "the complete updated tasks array at the end of your response as a "
        "```json code block. This enables the 'Apply to Tasks' button for the user."
    )
    parts.append(f"\n<!-- plan_task_id:{run.task_id} -->")

    return "\n".join(parts)


# ── Plan Update ──


def update_plan_tasks(run: Project, tasks: list[dict]) -> Project:
    """Replace tasks on a planned run. Returns the updated run."""
    updatable = {"planned", "cancelled", "failed"}
    if run.status not in updatable:
        raise ValueError(f"Run {run.task_id} is not in an updatable state (status={run.status})")
    new_tasks: list[Task] = []
    existing_map = {t.index: t for t in run.tasks} if run.tasks else {}
    for i, item in enumerate(tasks, 1):
        if not isinstance(item, dict) or "title" not in item:
            continue
        deps = item.get("depends_on", [])
        if not isinstance(deps, list):
            deps = []
        valid_deps = [int(d) for d in deps if isinstance(d, (int, float)) and 0 < int(d) < i]
        # force_approval is user-controlled. Preserve existing value when key absent
        # to avoid silent gate removal by callers that don't include the field.
        if "force_approval" in item:
            new_fa = bool(item["force_approval"])
        else:
            new_fa = existing_map[i].force_approval if i in existing_map else False
        new_tasks.append(
            Task(
                index=i,
                title=item["title"],
                description=item.get("description", item["title"]),
                requires_approval=bool(item.get("requires_approval")),
                force_approval=new_fa,
                depends_on=valid_deps,
            )
        )
    if not new_tasks:
        raise ValueError("No valid tasks")
    run.tasks = new_tasks  # skip normalize — respect user's explicit dep edits
    run.status = "planned"
    run.error = ""
    run.replan_count = 0
    return run


# ── YAML Workflow Decomposition ──

_YAML_ALLOWED_AGENT_KEYS = frozenset({
    "agent", "timeout", "depends_on", "description", "prompt", "shell",
})


def _check_acyclic(tasks: list[Task]) -> None:
    """Raise ValueError if the task dependency graph contains a cycle (iterative DFS)."""
    white, gray, black = 0, 1, 2
    color = {t.index: white for t in tasks}
    adj = {t.index: t.depends_on for t in tasks}
    for start in (t.index for t in tasks):
        if color[start] != white:
            continue
        stack = [(start, iter(adj.get(start, [])))]
        color[start] = gray
        while stack:
            node, children = stack[-1]
            try:
                dep = next(children)
                if color[dep] == gray:
                    raise ValueError(
                        f"Cycle detected: task {node} and task {dep} form a circular dependency"
                    )
                if color[dep] == white:
                    color[dep] = gray
                    stack.append((dep, iter(adj.get(dep, []))))
            except StopIteration:
                color[node] = black
                stack.pop()


_MAX_YAML_SIZE = 256 * 1024  # 256 KB
_MAX_AGENTS = 50


def decompose_yaml(yaml_content: str) -> list[Task]:
    """Parse a YAML workflow definition directly into Task objects.

    Bypasses the LLM decomposer entirely — depends_on is enforced as-is.
    """
    if len(yaml_content) > _MAX_YAML_SIZE:
        raise ValueError(f"YAML too large ({len(yaml_content)} bytes, max {_MAX_YAML_SIZE})")
    try:
        import yaml as _yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for YAML workflow decomposition: pip install PyYAML"
        ) from exc
    wf = _yaml.safe_load(yaml_content)
    if not wf or not isinstance(wf, dict) or "agents" not in wf:
        raise ValueError("YAML must have an 'agents' key with agent definitions")

    agents = wf["agents"]
    if not isinstance(agents, dict):
        raise ValueError("'agents' must be a mapping of agent names to specs")
    if not agents:
        raise ValueError("'agents' mapping is empty — define at least one agent")
    if len(agents) > _MAX_AGENTS:
        raise ValueError(f"Too many agents ({len(agents)}, max {_MAX_AGENTS})")
    agent_names = list(agents.keys())
    name_to_idx = {name: i + 1 for i, name in enumerate(agent_names)}

    tasks: list[Task] = []
    for i, (name, spec) in enumerate(agents.items(), 1):
        if not isinstance(name, str):
            raise ValueError(f"Agent name must be a string, got {type(name).__name__}: {name!r}")
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            raise ValueError(f"Agent '{name}' spec must be a mapping, got {type(spec).__name__}")
        bad_keys = set(spec.keys()) - _YAML_ALLOWED_AGENT_KEYS
        if bad_keys:
            raise ValueError(f"Agent '{name}' has unknown keys: {bad_keys}")

        deps = spec.get("depends_on", [])
        if deps is None:
            deps = []
        if not isinstance(deps, list):
            raise ValueError(f"Agent '{name}' depends_on must be a list")
        dep_indices = []
        for d in deps:
            if not isinstance(d, str):
                raise ValueError(f"Agent '{name}' depends_on entries must be strings, got {type(d).__name__}: {d!r}")
            if d not in name_to_idx:
                raise ValueError(f"Agent '{name}' depends on unknown agent '{d}'")
            dep_indices.append(name_to_idx[d])

        prompt = spec.get("prompt", spec.get("shell", ""))
        description = (
            f"Agent: {spec.get('agent', name)}\n"
            f"Timeout: {spec.get('timeout', '45m')}\n\n{prompt}"
        ).strip()

        tasks.append(Task(
            index=i,
            title=spec.get("description", name.replace("-", " ").title()),
            description=description,
            depends_on=dep_indices,
        ))

    _check_acyclic(tasks)
    return normalize_cross_group_deps(tasks)
