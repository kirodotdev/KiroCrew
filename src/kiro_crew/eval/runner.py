"""Multi-session evaluation runner.

Runs scenarios against a KiroCrew instance, capturing responses and
scoring assertions. Each scenario gets a fresh memory directory with
optional profile seeding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.eval.scenario import Assertion, AssertionType, Scenario, SeedProfile, Session, Turn
from kiro_crew.memory import MemoryStore
from kiro_crew.platform.context import redact_via_context
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    LLMProvider,
)
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader

logger = logging.getLogger(__name__)


# ── Result types ──


@dataclass
class TurnResult:
    """Result of a single turn."""

    user_message: str
    agent_response: str
    tool_calls: list[str] = field(default_factory=list)
    assertion_results: list[tuple[Assertion, bool]] = field(default_factory=list)
    elapsed_secs: float = 0.0
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and all(ok for _, ok in self.assertion_results)


@dataclass
class SessionResult:
    """Result of a single session."""

    name: str
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.turns)


@dataclass
class ScenarioResult:
    """Result of a complete scenario."""

    name: str
    description: str = ""
    dimensions: list[str] = field(default_factory=list)
    sessions: list[SessionResult] = field(default_factory=list)
    elapsed_secs: float = 0.0
    consolidation_failures: int = 0

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.sessions)

    @property
    def total_assertions(self) -> int:
        return sum(len(t.assertion_results) for s in self.sessions for t in s.turns)

    @property
    def passed_assertions(self) -> int:
        return sum(
            1 for s in self.sessions for t in s.turns for _, ok in t.assertion_results if ok
        )

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "assertions": f"{self.passed_assertions}/{self.total_assertions}",
            "sessions": len(self.sessions),
            "elapsed_secs": round(self.elapsed_secs, 2),
            "dimensions": self.dimensions,
        }


# ── Per-dimension scoring ──


def score_by_dimension(results: list[ScenarioResult]) -> dict[str, dict[str, Any]]:
    """Aggregate pass rates per evaluation dimension across scenarios.

    Scores at the scenario pass/fail level per dimension to avoid
    double-counting assertions when a scenario declares multiple dimensions.

    Returns a dict like:
        {"memory_recall": {"total": 5, "passed": 4, "rate": 0.8}, ...}
    """
    dim_stats: dict[str, dict[str, int]] = {}
    for r in results:
        for dim in r.dimensions:
            stats = dim_stats.setdefault(dim, {"total": 0, "passed": 0})
            stats["total"] += 1
            stats["passed"] += int(r.passed)

    # Every entry is created by the loop above and immediately counted, so `total`
    # is at least 1 for each key and the division needs no zero guard.
    return {
        dim: {
            "total": s["total"],
            "passed": s["passed"],
            "rate": round(s["passed"] / s["total"], 3),
        }
        for dim, s in dim_stats.items()
    }


# ── Profile seeding ──


def _seed_profile(ws: Path, seed: SeedProfile) -> None:
    """Write seed profile data into the workspace memory directory."""
    memory = MemoryStore(workspace=ws)
    memory.init()
    if seed.preferences:
        memory.write_preferences(seed.preferences)
    if seed.projects:
        memory.write_projects(seed.projects)
    if seed.lessons:
        lessons_file = ws / "lessons.jsonl"
        with lessons_file.open("a", encoding="utf-8") as f:
            for rule in seed.lessons:
                entry = {"ts": "seed", "rule": rule, "category": "knowledge"}
                f.write(json.dumps(entry) + "\n")


# ── Runner ──


ProviderFactory = Callable[[str], LLMProvider]

# Tools considered safe to auto-approve during eval (read-only).
# Fully-qualified names use exact match; ambiguous short names require
# additional path validation before approval.
_SAFE_TOOL_EXACT: frozenset[str] = frozenset(
    s.lower()
    for s in (
        "WorkspaceSearch",
    )
)

_SAFE_TOOL_PREFIXES_FS: tuple[str, ...] = tuple(
    s.lower()
    for s in (
        "read_file",
        "search_workspace",
        "grep",
        "glob",
        "head",
        "tail",
    )
)

_SAFE_TOOL_PREFIXES_API: tuple[str, ...] = tuple(
    s.lower()
    for s in (
        "read_internal",
        "describe",
        "introspect",
        "web_search",
        "aws___search_documentation",
        "aws___read_documentation",
        "aws___recommend",
        "aws___list_regions",
        "aws___get_regional_availability",
    )
)


@dataclass
class EvalRunner:
    """Runs multi-session evaluation scenarios.

    Args:
        provider_factory: Callable that creates an LLMProvider for a session key.
            Signature: (session_key: str) -> LLMProvider
        workspace_dir: Optional base dir for memory. A temp dir is used if None.
    """

    provider_factory: ProviderFactory
    workspace_dir: Path | None = None
    judge_enabled: bool = False

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        """Run a single scenario with fresh state.

        Note: Not safe for concurrent use — mutates ``os.environ`` to set
        ``KIROCREW_WORKSPACE`` for the duration of the run.
        """
        if self.workspace_dir:
            return await self._run_scenario_in(self.workspace_dir, scenario)

        with tempfile.TemporaryDirectory(prefix="kirocrew_eval_") as tmp:
            return await self._run_scenario_in(Path(tmp), scenario)

    async def _run_scenario_in(self, ws: Path, scenario: Scenario) -> ScenarioResult:
        """Run scenario inside *ws*, wiring the memory loop between sessions."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.context import ContextBuilder
        from kiro_crew.history import ConversationLog, HistoryConsolidator
        from kiro_crew.learn import LessonStore
        from kiro_crew.session import SessionManager
        from kiro_crew.vector_memory import VectorMemoryStore

        config = KiroCrewConfig.load()
        memory = MemoryStore(workspace=ws)
        memory.init()

        if scenario.seed:
            _seed_profile(ws, scenario.seed)

        # Set env so providers share the same memory directory
        # NOTE: os.environ mutation is process-global — not safe for concurrent runs.
        old_ws = os.environ.get("KIROCREW_WORKSPACE")
        os.environ["KIROCREW_WORKSPACE"] = str(ws)

        session_mgr = None
        vector_store = None
        try:
            # Memory-loop components
            conv_log = ConversationLog(base_dir=ws)
            conv_log.init()
            lesson_store = LessonStore(base_dir=ws)
            vector_store = VectorMemoryStore(db_path=ws / "vector_memory.db")
            # Off the loop: init() tightens the DB and its sidecars to
            # owner-only — blocking file IO alongside the sqlite connect and
            # migrations (the Windows lockdown itself is now in-process).
            # This runs once per scenario, so a synchronous call would stall
            # the loop on every one.
            await asyncio.to_thread(vector_store.init)

            # Wrap provider factory so all sessions share the same workspace root
            # (default factory creates per-session subdirs, which isolates memory)
            def shared_ws_factory(session_key: str, **kwargs: Any) -> LLMProvider:
                from kiro_crew.providers.acp import AcpProvider

                provider = self.provider_factory(session_key, **kwargs)
                if isinstance(provider, AcpProvider):
                    # Access private attribute directly to avoid modifying core provider
                    # files. TODO: add set_workspace() to AcpProvider.
                    if not (hasattr(provider, "_client") and hasattr(provider._client, "_work_dir")):
                        raise RuntimeError(
                            "AcpProvider internals changed — eval workspace override broken"
                        )
                    provider._client._work_dir = ws
                else:
                    logger.warning(
                        "Cannot override workspace for %s provider; "
                        "cross-session memory may not work.",
                        type(provider).__name__,
                    )
                return provider

            # SessionManager uses shared_ws_factory so consolidation sessions
            # also target the shared workspace.
            session_mgr = SessionManager(config, shared_ws_factory)
            await session_mgr.start_pool()

            consolidator = HistoryConsolidator(
                log=conv_log,
                memory=memory,
                sessions=session_mgr,
                lesson_store=lesson_store,
                vector_store=vector_store,
            )

            # Constructed on a running loop, where construction-time sync
            # skips itself; eval runs standalone with no gateway to own the
            # sync, so run the explicit seam in a worker thread (mirrors
            # gateway startup). The loader targets a scenario-local skills
            # dir: eval must never write quarantines into the user's real
            # skills home, and a self-contained dir keeps scenarios
            # reproducible across machines. A failed sync must not fail the
            # scenario before it runs.
            skills = SkillsLoader(skills_path=ws / "skills", install_builtins=False)
            try:
                await asyncio.to_thread(skills.sync_builtins)
            except Exception:
                logger.warning(
                    "builtin-skill sync failed; continuing without synced "
                    "builtins", exc_info=True,
                )
            ctx_builder = ContextBuilder(
                memory=memory,
                lessons=lesson_store,
                skills=skills,
                conversation_log=conv_log,
            )

            result = ScenarioResult(
                name=scenario.name,
                description=scenario.description,
                dimensions=scenario.dimensions,
            )
            t0 = time.monotonic()

            for idx, session_def in enumerate(scenario.sessions):
                session_result = await self._run_session(
                    session_def, ws,
                    provider_factory=shared_ws_factory,
                    ctx_builder=ctx_builder if idx > 0 else None,
                )
                result.sessions.append(session_result)

                # Persist turns into ConversationLog and consolidate
                log_key = f"eval_{session_def.name}"
                for turn in session_result.turns:
                    # Persisted row is an egress served to dashboard readers; the
                    # prompt comes from `turn_def.user` and stays verbatim.
                    conv_log.append(log_key, "user", redact_via_context(turn.user_message))
                    conv_log.append(log_key, "assistant", turn.agent_response)
                try:
                    # Use _consolidate directly instead of maybe_consolidate because
                    # eval sessions are short (1-7 turns) and won't exceed the
                    # message threshold that maybe_consolidate checks. We need to
                    # force consolidation so cross-session memory is available.
                    # TODO: This bypasses offset tracking — use
                    # maybe_consolidate(log_key, force=True) when that API is
                    # available.
                    await consolidator._consolidate(log_key, include_history=True)
                except Exception:
                    logger.warning("Consolidation failed for %s", log_key, exc_info=True)
                    result.consolidation_failures += 1
        finally:
            if session_mgr:
                await session_mgr.close_all()
            if vector_store:
                vector_store.close()
            if old_ws is None:
                os.environ.pop("KIROCREW_WORKSPACE", None)
            else:
                os.environ["KIROCREW_WORKSPACE"] = old_ws

        result.elapsed_secs = time.monotonic() - t0
        return result

    async def run_scenarios(self, scenarios: list[Scenario]) -> list[ScenarioResult]:
        """Run multiple scenarios sequentially."""
        results = []
        for scenario in scenarios:
            result = await self.run_scenario(scenario)
            results.append(result)
            status = "✅" if result.passed else "❌"
            logger.info(
                "%s %s — %s/%s assertions",
                status,
                result.name,
                result.passed_assertions,
                result.total_assertions,
            )
        return results

    async def _run_session(
        self,
        session_def: Session,
        ws: Path,
        *,
        provider_factory: ProviderFactory | None = None,
        ctx_builder: Any = None,
    ) -> SessionResult:
        """Run a single session — create provider, send turns, tear down."""
        factory = provider_factory or self.provider_factory
        session_key = f"eval_{session_def.name}_{time.monotonic_ns()}"
        provider = factory(session_key)
        await provider.start()

        # Build memory context once for the first turn of non-first sessions
        memory_context = ""
        if ctx_builder is not None:
            memory_context = ctx_builder.build_session_context(session_key=session_key)

        session_result = SessionResult(name=session_def.name)
        try:
            for i, turn_def in enumerate(session_def.turns):
                # Prepend memory context to the first turn of non-first sessions
                effective_turn = turn_def
                if i == 0 and memory_context:
                    effective_turn = Turn(
                        user=memory_context + turn_def.user,
                        assertions=turn_def.assertions,
                    )
                try:
                    turn_result = await self._run_turn(provider, effective_turn, session_key)
                    # Store the original user message for reporting (not the context-injected one)
                    turn_result.user_message = turn_def.user
                except Exception as exc:
                    logger.warning("Turn failed in %s: %s", session_def.name, exc)
                    turn_result = TurnResult(
                        user_message=turn_def.user,
                        agent_response="",
                        error=str(exc),
                    )
                session_result.turns.append(turn_result)
        finally:
            await provider.shutdown()

        return session_result

    @staticmethod
    def _classify_safe_tool(event: Any) -> str:
        """Classify a tool permission request. Returns 'exact', 'prefix_fs', 'prefix_api', or 'unsafe'."""
        tool_name = event.title.split("(")[0].split(":")[0].strip().lower()

        if tool_name in _SAFE_TOOL_EXACT:
            return "exact"
        if any(tool_name.startswith(p) for p in _SAFE_TOOL_PREFIXES_FS):
            return "prefix_fs"
        if any(tool_name.startswith(p) for p in _SAFE_TOOL_PREFIXES_API):
            return "prefix_api"
        return "unsafe"

    @staticmethod
    def _extract_path_from_input(tool_input: str) -> str:
        """Try to extract a file path from tool_input (JSON or plain text)."""
        if not tool_input:
            return ""
        try:
            data = json.loads(tool_input)
            if isinstance(data, dict):
                return data.get("path", "") or data.get("file", "") or data.get("target", "")
        except (json.JSONDecodeError, TypeError):
            pass
        # Fallback: look for path-like patterns
        for token in tool_input.split():
            if "/" in token and not token.startswith("http"):
                return token
        return ""

    async def _run_turn(
        self, provider: LLMProvider, turn_def: Turn, session_key: str,
    ) -> TurnResult:
        """Send a message and collect the response."""
        t0 = time.monotonic()
        chunks: list[str] = []
        tool_calls: list[str] = []

        async for event in provider.stream(turn_def.user):
            if event.kind == EVENT_TEXT_CHUNK:
                chunks.append(event.text)
            elif event.kind == EVENT_TOOL_CALL:
                tool_calls.append(event.text)
                sel().log_tool_invocation(
                    session_key=session_key,
                    tool_name=event.text,
                    outcome="invoked",
                    source="eval_runner",
                )
            elif event.kind == EVENT_PERMISSION_REQUEST:
                from kiro_crew.security import is_sensitive_path

                safety = self._classify_safe_tool(event)
                if safety in ("exact", "prefix_api"):
                    # Known read-only API — approve without path check
                    sel().log_tool_invocation(
                        session_key=session_key,
                        tool_name=event.title,
                        outcome="approved",
                        source="eval_runner",
                    )
                    await provider.approve_tool(event.request_id)
                elif safety == "prefix_fs":
                    # Filesystem operation — deny-by-default path check
                    target = self._extract_path_from_input(event.tool_input or "")
                    if target:
                        target = str(Path(target).expanduser().resolve())
                    if target and not is_sensitive_path(target):
                        sel().log_tool_invocation(
                            session_key=session_key,
                            tool_name=event.title,
                            outcome="approved",
                            source="eval_runner",
                        )
                        await provider.approve_tool(event.request_id)
                    else:
                        outcome = "rejected_sensitive" if target else "rejected_no_path"
                        logger.warning("Rejected tool (path check failed): %s", event.title)
                        sel().log_tool_invocation(
                            session_key=session_key,
                            tool_name=event.title,
                            outcome=outcome,
                            source="eval_runner",
                        )
                        await provider.reject_tool(event.request_id)
                else:
                    logger.warning("Rejected unsafe tool in eval: %s", event.title)
                    sel().log_tool_invocation(
                        session_key=session_key,
                        tool_name=event.title,
                        outcome="rejected",
                        source="eval_runner",
                    )
                    await provider.reject_tool(event.request_id)
            elif event.kind == EVENT_COMPLETE:
                break

        response = "".join(chunks).strip()
        elapsed = time.monotonic() - t0

        assertion_results = [
            (a, a.check(response))
            for a in turn_def.assertions
            if self.judge_enabled or a.type != AssertionType.JUDGE
        ]

        return TurnResult(
            user_message=turn_def.user,
            agent_response=response,
            tool_calls=tool_calls,
            assertion_results=assertion_results,
            elapsed_secs=elapsed,
        )


# ── Reporting ──


def format_results(results: list[ScenarioResult]) -> str:
    """Format results as a human-readable report."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    lines = ["# Eval Results", ""]
    total_pass = sum(1 for r in results if r.passed)
    lines.append(f"**{total_pass}/{len(results)} scenarios passed**\n")

    # Per-dimension scorecard
    dims = score_by_dimension(results)
    if dims:
        lines.append("## Scorecard by Dimension\n")
        lines.append("| Dimension | Passed | Total | Rate |")
        lines.append("|-----------|--------|-------|------|")
        for dim, s in sorted(dims.items()):
            lines.append(f"| {dim} | {s['passed']} | {s['total']} | {s['rate']:.0%} |")
        lines.append("")

    for r in results:
        status = "✅" if r.passed else "❌"
        lines.append(f"## {status} {r.name}")
        if r.description:
            lines.append(f"_{r.description}_")
        lines.append(
            f"Assertions: {r.passed_assertions}/{r.total_assertions} | "
            f"Time: {r.elapsed_secs:.1f}s"
        )
        if r.dimensions:
            lines.append(f"Dimensions: {', '.join(r.dimensions)}")
        if r.consolidation_failures > 0:
            lines.append(
                f"⚠️  {r.consolidation_failures} consolidation failure(s) — "
                f"cross-session memory may be incomplete"
            )
        lines.append("")

        for si, sr in enumerate(r.sessions):
            s_status = "✅" if sr.passed else "❌"
            lines.append(f"### {s_status} Session {si + 1}: {sr.name}")
            for ti, tr in enumerate(sr.turns):
                t_status = "✅" if tr.passed else "❌"
                lines.append(f"  {t_status} Turn {ti + 1}: `{tr.user_message[:60]}`")
                if tr.tool_calls:
                    lines.append(f"     Tools: {', '.join(tr.tool_calls[:5])}")
                for assertion, ok in tr.assertion_results:
                    a_status = "✅" if ok else "❌"
                    lines.append(
                        f"     {a_status} {assertion.type.value}: "
                        f"`{assertion.value[:50]}`"
                    )
                if not tr.passed:
                    snippet = tr.agent_response[:200].replace("\n", " ")
                    snippet, _ = redact_exfiltration_urls(snippet)
                    snippet, _ = redact_credentials(snippet)
                    lines.append(f"     Response: {snippet}")
            lines.append("")

    return "\n".join(lines)
