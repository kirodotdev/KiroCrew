"""The harness arms ``kirocrew eval run --suite tasks`` can drive, one adapter each.

An arm turns a case's seed into one headless command line, and turns what that
command printed into an :class:`ArmOutcome` the scorer reads (status, asks, cost).
The runner owns everything else: the eval home, the env, the wall cap, the
hidden check. Design: docs/request-for-change/rfc-builtin-eval.md §3.2 and §5.

- ``crew-chat`` and ``crew-conductor`` run ``kirocrew chat --headless`` (§12)
  against a gateway the runner started in the run's own data home; the
  conductor arm adds ``--mode conductor --until-done``, so the agent is whatever
  ``crew_mode_agent`` resolves, and the CLI's ``--json`` names it.
- ``claude-code`` runs ``claude -p`` in ``dontAsk`` mode with only the case's tools
  and no MCP server.
- ``kiro-cli-v3`` and ``codex`` are not built yet: each run of them is VOID with
  that reason, never a made-up result.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

#: What a Crew headless ``--json`` status means for the scorer (§12.5): a status
#: here is VOID (the harness could not run the task), every other one is scored.
_CREW_VOID_STATUSES = frozenset({"not_signed_in", "sandbox_refused", "no_gateway", "interrupted"})
#: Claude Code tools that wait on a person (§5, the ask detector's Claude column).
_CLAUDE_ASK_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})


@dataclass
class Caps:
    """The limits one run gets, already narrowed to the case's own values."""

    timeout_s: float
    credits: float
    usd: float


@dataclass
class ArmOutcome:
    """What one arm reported about its run, before the hidden check.

    ``status``: ``done`` | ``asked`` | ``timeout`` | ``budget`` | ``incomplete`` |
    ``error`` | ``void``. ``void`` means the run never really started (no sign-in,
    no gateway, an arm that is not built); it is rerun, never scored.
    """

    status: str
    asks: int = 0
    ask_events: list[dict[str, Any]] = field(default_factory=list)
    cost: float | None = None
    cost_unit: str = ""
    agent: dict[str, Any] | None = None
    note: str = ""


class Arm:
    """One harness. Subclasses fill in the command line and the result parser."""

    name = ""
    #: Whether the runner must start a Crew gateway in the run's data home first.
    needs_gateway = False
    #: Env variables passed through to the arm (its eval-only sign-in, §4.3).
    env_keys: tuple[str, ...] = ()
    cost_unit = ""

    def unavailable(self) -> str:
        """Why this arm cannot run on this host at all, or "" when it can."""
        return ""

    def missing_credential(self, env: dict[str, str]) -> str:
        """Why the eval sign-in this arm needs is absent from *env*, or ""."""
        return ""

    def argv(self, seed: str, repo: str, caps: Caps, model: str, tools: list[str]) -> list[str]:
        raise NotImplementedError

    def parse(self, returncode: int, stdout: str) -> ArmOutcome:
        raise NotImplementedError


class CrewChatArm(Arm):
    name = "crew-chat"
    needs_gateway = True
    env_keys = ("KIRO_API_KEY",)
    cost_unit = "credits"
    mode_flags: tuple[str, ...] = ()

    def missing_credential(self, env: dict[str, str]) -> str:
        return "" if env.get("KIRO_API_KEY") else "KIRO_API_KEY is not set (an eval-only key, §4.3)"

    def argv(self, seed: str, repo: str, caps: Caps, model: str, tools: list[str]) -> list[str]:
        from kiro_crew.platform_compat import isolated_python_argv

        cmd = isolated_python_argv("-P", "-m", "kiro_crew", "chat", "--headless", "-m", seed)
        cmd += [*self.mode_flags, "--approval-mode", "trust", "--cwd", repo]
        cmd += ["--timeout", f"{caps.timeout_s:g}", "--budget", f"{caps.credits:g}", "--json"]
        if model:
            cmd += ["--model", model]
        return cmd

    def parse(self, returncode: int, stdout: str) -> ArmOutcome:
        payload = _last_json_object(stdout)
        if payload is None:
            return ArmOutcome("error", note=f"no --json result (exit {returncode})")
        status = str(payload.get("status") or "error")
        events = payload.get("ask_events")
        outcome = ArmOutcome(
            "void" if status in _CREW_VOID_STATUSES else status,
            asks=int(payload.get("asks") or 0),
            ask_events=list(events) if isinstance(events, list) else [],
            cost=_number(payload.get("credits")),
            cost_unit=self.cost_unit,
            agent=payload.get("agent") if isinstance(payload.get("agent"), dict) else None,
            note=str(payload.get("error") or ("" if status == "done" else status))[:500],
        )
        return outcome


class CrewConductorArm(CrewChatArm):
    name = "crew-conductor"
    mode_flags = ("--mode", "conductor", "--until-done")


class ClaudeCodeArm(Arm):
    name = "claude-code"
    env_keys = (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_REGION",
    )
    cost_unit = "usd"

    def binary(self) -> str:
        """``claude`` by its real path: a PATH shim may not work under the run's HOME."""
        found = shutil.which("claude")
        return os.path.realpath(found) if found else ""

    def unavailable(self) -> str:
        return "" if self.binary() else "claude is not on PATH"

    def missing_credential(self, env: dict[str, str]) -> str:
        if env.get("ANTHROPIC_API_KEY"):
            return ""
        if env.get("CLAUDE_CODE_USE_BEDROCK") and env.get("AWS_BEARER_TOKEN_BEDROCK"):
            return ""
        return (
            "neither ANTHROPIC_API_KEY nor CLAUDE_CODE_USE_BEDROCK with "
            "AWS_BEARER_TOKEN_BEDROCK is set (an eval-only key, §4.3)"
        )

    def argv(self, seed: str, repo: str, caps: Caps, model: str, tools: list[str]) -> list[str]:
        cmd = [self.binary() or "claude", "-p", seed, "--output-format", "stream-json", "--verbose"]
        cmd += ["--permission-mode", "dontAsk", "--permission-prompts", "none"]
        cmd += ["--allowedTools", ",".join(tools), "--strict-mcp-config"]
        cmd += ["--max-budget-usd", f"{caps.usd:g}"]
        if model:
            cmd += ["--model", model]
        return cmd

    def parse(self, returncode: int, stdout: str) -> ArmOutcome:
        events: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None
        for line in stdout.splitlines():
            obj = _json_line(line)
            if obj is None:
                continue
            if obj.get("type") == "result":
                result = obj
            elif obj.get("type") == "assistant":
                for block in (obj.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") in _CLAUDE_ASK_TOOLS:
                            events.append({"kind": "question", "text": str(block.get("name"))})
        if result is None:
            return ArmOutcome(
                "error",
                ask_events=events,
                asks=len(events),
                note=f"no result line (exit {returncode})",
            )
        denials = result.get("permission_denials")
        for denial in denials if isinstance(denials, list) else []:
            tool = denial.get("tool_name") if isinstance(denial, dict) else ""
            events.append({"kind": "approval", "text": str(tool or "denied")})
        subtype = str(result.get("subtype") or "")
        if subtype == "error_max_budget_usd":
            status = "budget"
        elif result.get("is_error") or subtype != "success":
            status = "error"
        else:
            status = "done"
        return ArmOutcome(
            status,
            asks=len(events),
            ask_events=events,
            cost=_number(result.get("total_cost_usd")),
            cost_unit=self.cost_unit,
            note="" if status == "done" else subtype,
        )


class NotBuiltArm(Arm):
    """An arm the RFC names that v1 does not drive yet: every run is VOID."""

    def __init__(self, name: str) -> None:
        self.name = name

    def unavailable(self) -> str:
        return f"{self.name} is not available in this version of kirocrew eval run"


ARMS: dict[str, Arm] = {
    arm.name: arm
    for arm in (
        CrewChatArm(),
        CrewConductorArm(),
        NotBuiltArm("kiro-cli-v3"),
        ClaudeCodeArm(),
        NotBuiltArm("codex"),
    )
}


def _json_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        obj = _json_line(line)
        if obj is not None:
            return obj
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
