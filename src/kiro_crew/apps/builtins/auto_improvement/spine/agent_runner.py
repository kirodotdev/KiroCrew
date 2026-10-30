"""Headless ``claude -p`` agent runner — the *intelligent* step of the loop.

Ported from the original framework's ``autoloop/claude_runner.py`` (which produced
17 perf + 18 bug draft CRs). The autonomous loop uses the model only for the steps
that need judgement — author a bug fix (reproducing test + source edit) inside an
isolated worktree, or refine a perf edit. Orchestration, gating (build + RED→GREEN /
A-B noise band), verification, dedup, and CR drafting stay deterministic Python in the
spine; THIS is the one place a model is invoked.

The runner is a thin subprocess wrapper that:
  - runs ``claude -p`` with a prompt (cwd'd into the candidate worktree),
  - bypasses interactive permission prompts (the worktree is throwaway + isolated and
    the clone is push-disabled — the blast radius is contained by gating + no-push),
  - enforces a timeout, accumulates cost so the driver's ``--max-cost`` ceiling is live,
  - NEVER raises on model-side failure (returns a result the caller inspects).

It is OPTIONAL and INJECTED: the spine works headless/offline without it (the offline
profile's ``propose`` realizes mechanical perf seeds and the bug ``propose`` returns
False = "awaiting the agent edit"). When a runner is wired (the backend app passes one),
bug candidates get a real authored fix and the loop produces real draft CRs — matching
the original framework end-to-end.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib as _importlib
import json
import logging
import os
import shlex  # noqa: F401
import shutil
import signal
import subprocess
import sys as _sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace  # noqa: F401
from typing import TYPE_CHECKING, Any  # noqa: F401

from kiro_crew import _facade_mirror
from kiro_crew.config import KiroCrewConfig
from kiro_crew.constants import (
    DENY_CAUSE_AUDIT_UNAVAILABLE,
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_POLICY,
    DENY_CAUSE_SURFACE_POLICY,
)
from kiro_crew.hooks import (  # noqa: F401
    TOOL_DENY,
    HookManager,
    hook_gate_kwargs,
    hooks_config_from_config_dict,
)
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.llm_helpers import _steer_host_deny
from kiro_crew.permission_floor import (
    OUTCOME_PENDING_APPROVAL,
    OUTCOME_REJECTED_TRANSPORT_FLOOR,
)
from kiro_crew.platform.context import redact_via_context
from kiro_crew.platform_compat import SIGKILL, kill_process_tree
from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv
from kiro_crew.subprocess_utf8 import UTF8_TEXT  # noqa: F401

from .agent_runner_authoring import _UNFINISHED_TURN_ERROR
from .agent_runner_feed import (
    _detail_is_richer,
    _LineBuffer,
    _summarize_stream_event,
    _tool_detail,
)
from .agent_runner_gate import (
    _allowlist_deny_reason,
    _bare_event,
    _governance_denial,
    _GovernanceDeny,
    _requested_command,
    _tool_permitted,
    shell_command_refusal,
)
from .git_safety import GIT_SAFE_CONFIG, hook_off_args, require_pinned  # noqa: F401

logger = logging.getLogger(__name__)


#: What the model is told when the unattended run refuses a call because its
#: audit record could not be written (see ``SessionAgentRunner._approve``:
#: audit-or-deny). Nothing judged the action; the host could not record it.
_AUDIT_UNAVAILABLE_REASON = (
    "this unattended auto-improvement run approves a tool only once the "
    "approval is on the Security Event Log, and that record could not be "
    "written, so the call was refused rather than run unaudited"
)

# The headless Claude Code binary. Overridable for tests / alternate installs.
CLAUDE_BIN = os.environ.get("AUTO_IMPROVEMENT_CLAUDE_BIN", "claude")

#: The stop reason on the ``EVENT_COMPLETE`` the ACP session handle yields when a prompt
#: times out before the backend finished the turn (``acp/session_handle_dispatch.py``). It has no
#: constant in ``acp.types``.
_STOP_REASON_PROMPT_TIMEOUT = "timeout"


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    error: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    raw: dict = field(default_factory=dict)


# Shared with the gate — see ``push_policy.strip_credential_env``.
from .push_policy import strip_credential_env  # noqa: E402


def _same_bytes(path: Path, desired: bytes) -> bool:
    """Does *path* already hold exactly *desired*? An unreadable file does not."""
    try:
        return path.read_bytes() == desired
    except OSError:
        return False


def _install_agent_copy(src: Path, dest: Path, desired: bytes, agent_name: str) -> bool:
    """Copy *src* to *dest* unless *dest* is a DIFFERENT file; report success.

    ``~/.kiro/agents/<name>.json`` is the user's own agent directory, and a file
    there that merely shares this app's agent name is theirs: overwriting it
    destroys their data. So the copy lands only when the destination is absent,
    and an existing destination counts as registered only when it is already
    byte-identical. On a real conflict the run proceeds with the default agent,
    as on any other registration failure: tool scoping is best-effort, not a
    licence to overwrite.
    """
    if dest.exists():
        if _same_bytes(dest, desired):
            return True
        logger.warning(
            "not self-registering agent %s: %s already exists with different "
            "content (leaving the user's file intact; using the default agent)",
            agent_name,
            dest,
        )
        return False
    shutil.copyfile(src, dest)
    return True


def _unlink_quietly(path: object) -> None:
    """Remove a sandbox launcher temp file. Never raises — losing the cleanup of a temp
    file must not fail an otherwise-successful agent run."""
    if not path:
        return
    try:
        Path(str(path)).unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove sandbox launcher temp file", exc_info=True)


def _audit_unattended_agent(*, cwd: str | None, model: str | None, max_turns: int) -> bool:
    """Record the launch of a permissionless agent in the SEL. False = do not launch.

    `--dangerously-skip-permissions` makes the whole subprocess a single unattended
    approval covering every tool it will use, so this one event is the audit trail for
    all of them. `critical=True` writes it synchronously and re-raises on a filesystem
    failure, which is what lets the caller deny rather than run untraced — the same
    audit-or-deny contract as the session path's per-tool approval.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="auto-improvement-subprocess",
            agent="auto-improvement",
            source="auto_improvement_loop",
            tool_name="claude-cli",
            tool_kind="agent_subprocess",
            outcome="auto_approved",
            resources=str(cwd or ""),
            metadata={
                "unattended": True,
                "skip_permissions": True,
                "model": str(model or ""),
                "max_turns": int(max_turns),
                "containment": "worktree+push_disabled_clone",
            },
            critical=True,
        )
        return True
    except Exception:  # noqa: BLE001 - audit failure must deny, not launch
        logger.warning("SEL audit failed for the unattended agent launch — refusing to launch")
        return False


class AgentRunner:
    """A cost-accumulating ``claude -p`` invoker. One instance per run so the driver's
    cost meter can read cumulative spend (``--max-cost`` ceiling). Thread-safe cost add
    so a future parallel fan-out stays correct."""

    def __init__(
        self,
        *,
        model: str | None = None,
        default_timeout_s: float = 1800.0,
        stop_check=None,
        on_activity=None,
    ) -> None:
        self.model = model
        self.default_timeout_s = default_timeout_s
        self._cost_lock = threading.Lock()
        self._total_cost_usd = 0.0
        # Optional callable() -> bool polled while waiting on the agent subprocess so a
        # clean-stop request terminates the in-flight ``claude -p`` instead of letting it
        # run to ``--max-turns`` (each turn is real cost). Default: no stop check; the
        # subprocess runs to its own timeout. The driver wires this to its ``_stop``
        # flag so a UI Stop click actually aborts pending agent calls.
        self._stop_check = stop_check
        # Optional callable(dict) -> None invoked for each streamed agent turn (assistant
        # text, a tool_use, a tool_result). This is what turns the dead "proposing
        # fixes…" placeholder into a live feed of what the agent is actually doing — the
        # backend wires it to a ring buffer the UI polls. When set, the runner uses
        # ``--output-format stream-json`` and parses stdout line-by-line; when None it
        # keeps the cheaper one-shot ``json`` path.
        self._on_activity = on_activity if callable(on_activity) else None

    def total_cost_usd(self) -> float:
        with self._cost_lock:
            return self._total_cost_usd

    @staticmethod
    def _terminate_group(popen: subprocess.Popen) -> None:
        """Kill the agent subprocess AND any children it spawned. ``claude -p`` forks an
        editor + helpers; a plain ``popen.kill()`` only signals the parent and leaves the
        children running (the symptom we hit: agents kept costing money after Stop).
        Using ``start_new_session=True`` made the child a process-group leader, so a
        SIGTERM to the negative pid signals every descendant cleanly.

        Uses platform_compat.kill_process_tree for cross-platform support (Windows
        lacks os.killpg/os.getpgid)."""
        try:
            kill_process_tree(popen.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError, ValueError):
            try:
                popen.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            popen.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                kill_process_tree(popen.pid, SIGKILL)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def available() -> bool:
        """True iff the headless agent binary is on PATH — lets the backend decide
        whether to wire a real runner or stay offline (no fabricated fixes)."""
        return shutil.which(CLAUDE_BIN) is not None

    def _spawn_sandboxed_agent(self, cmd: list[str], cwd: str | None):
        """Launch the agent CLI inside the OS sandbox. Returns ``(popen, cleanup_path)``.

        A separate, uniquely-named method rather than inline in ``run``: the spawn audit
        keys findings by ``file::function``, and this module has TWO ``run`` methods
        (``AgentRunner`` and ``SessionAgentRunner``). Inline, the sandboxed spawn was
        attributed to the wrong one and read as unrouted — a real reporting gap, since the
        audit is what guarantees every agent-influenced spawn is sandboxed.

        This agent runs with ``--dangerously-skip-permissions``, so its Bash tool is
        unattended: the worktree stays VISIBLE (there would be nothing to edit otherwise)
        while the operator's credential directories are hidden and the environment is
        scrubbed. Sandboxing is what makes keeping this fallback safe — a malicious
        repository prompt cannot reach credentials outside the worktree — so the only
        path that works when no in-process provider is configured stays available.
        """
        root = str(Path(cwd).resolve()) if cwd else None
        # STRICT mode, not the default "standard": "standard" deliberately leaves ~/.aws
        # visible so a test suite can use the AWS CLI, but the fix-AUTHORING agent has no
        # such need and runs `--dangerously-skip-permissions`, so it should see no
        # credential dir at all. Verified on this host that "standard" left ~/.aws
        # readable to the child; "strict" hides it (+ .gnupg, gh, gcloud, kube, …).
        # strip_python_env so the agent's own tooling can't inherit Kiro Crew's interpreter
        # paths and import from this process's tree.
        sandboxed, scrubbed_env, cleanup = sandboxed_spawn_argv(
            list(cmd),
            mode="strict",
            strip_python_env=True,
            extra_visible_dirs=((root,) if root else ()),
        )
        # Strip credential-shaped names the SHARED scrub list misses. Measured on this
        # host: `GITHUB_TOKEN` survives `scrub_env` (its list covers AWS_SECRET/SLACK_*/
        # TELEGRAM_* but not GITHUB_*), so an exported token reached this agent — which runs
        # `--dangerously-skip-permissions`, meaning a repository instruction could use it.
        # Review raised this repeatedly; the gate already defends this way
        # (`profiles/github_repo/profile.py:strip_credential_env`) and the agent spawn is
        # the other place untrusted content executes, so it gets the same treatment.
        scrubbed_env = strip_credential_env(scrubbed_env)
        popen = popen_limited(
            # argv LIST, no shell=True, so there is no shell to inject into and
            # shlex.quote() does not apply. Every element is either a literal or a value
            # the app itself computed (prompt text, model name, worktree paths); an argv
            # element can only ever be one argument to CLAUDE_BIN.
            sandboxed,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered so streamed lines arrive promptly
            env=scrubbed_env,
            # Make the child its own process group so we can kill it (and any children it
            # spawned) cleanly on stop without taking the parent down.
            start_new_session=True,
        )
        return popen, cleanup

    def run(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        append_system: str | None = None,
        max_turns: int = 40,
        timeout_s: float | None = None,
        add_dirs: list[str] | None = None,
    ) -> AgentResult:
        """Invoke ``claude -p``. Returns an :class:`AgentResult`; never raises on a
        model-side failure (mirrors the original ``run_claude`` contract)."""
        timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
        streaming = self._on_activity is not None
        cmd = (
            [
                CLAUDE_BIN,
                "-p",
                prompt,
                # stream-json emits one JSON object per line as the agent works (assistant
                # turns, tool_use, tool_result), so the UI can show a live feed instead of a
                # frozen "proposing fixes…". --verbose is required for stream-json with -p.
                # Without an activity sink we keep the cheaper one-shot json envelope.
                "--output-format",
                "stream-json",
                "--verbose",
            ]
            if streaming
            else [
                CLAUDE_BIN,
                "-p",
                prompt,
                "--output-format",
                "json",
            ]
        )
        cmd += [
            "--max-turns",
            str(max_turns),
            # Unattended loop; worktrees are throwaway + isolated and the clone is
            # push-disabled, so bypass interactive prompts. Blast radius = the worktree.
            "--dangerously-skip-permissions",
            # NO MCP servers. The fix-authoring agent only needs file/shell tools
            # (Bash/Read/Edit/Write/Grep/Glob); without this it inherits the user's full
            # ~/.claude.json MCP config (15+ servers — aim, badger, node servers, …) and
            # spends MINUTES booting that stack per candidate (the "stuck on write-a-fix"
            # symptom). An empty strict config boots with zero servers → fast startup.
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ]
        # `if allowed_tools:` is WRONG for an empty list: `[]` means "no tools at all"
        # (agent_discovery's forced-answer pass), but falsy-empty omits the flag entirely
        # and this path also passes `--dangerously-skip-permissions` — so the supposedly
        # tool-free agent would get everything. The SESSION path's `_tool_permitted` makes
        # the same decision; the subprocess path has its own copy of it. `None` still means
        # "no restriction imposed".
        if allowed_tools is not None:
            if allowed_tools:
                cmd += ["--allowed-tools", *allowed_tools]
            else:
                # Deny every tool. `--allowed-tools` with an empty value is not a reliable
                # way to say that, so name a tool that does not exist: nothing can match it.
                cmd += ["--allowed-tools", "__none__"]
        if append_system:
            cmd += ["--append-system-prompt", append_system]
        if self.model:
            cmd += ["--model", self.model]
        for d in add_dirs or []:
            cmd += ["--add-dir", d]

        t0 = time.monotonic()
        # AUDIT-OR-DENY, matching `SessionAgentRunner._approve`. This fallback passes
        # `--dangerously-skip-permissions`, so the whole subprocess is one unattended
        # blanket approval and the SEL entry is the only record it happened. Without it
        # the two runner paths disagree: the session path refuses an unauditable tool
        # while this one would run a permissionless agent silently, an asymmetry that
        # amounts to a sandbox bypass.
        #
        # The containment argument for the spawn itself is unchanged and recorded in
        # test_spawn_audit (argv[0] is a module constant, the agent edits only files
        # inside a throwaway worktree of a push-disabled clone, and executing those files
        # is routed separately through the sandbox). What was missing was the trail.
        if not _audit_unattended_agent(cwd=cwd, model=self.model, max_turns=max_turns):
            return AgentResult(
                ok=False,
                error="refusing to launch an unattended agent that cannot be audited",
                duration_s=time.monotonic() - t0,
            )
        # Route the fallback agent through the OS sandbox, the same chokepoint the gate's
        # test execution uses (`profiles/github_repo/profile.py::_run`). This agent runs
        # with `--dangerously-skip-permissions`, so its Bash tool is unattended: the
        # worktree must stay VISIBLE (there would be nothing to edit otherwise) while the
        # operator's credential directories are hidden and the environment is scrubbed.
        #
        # Sandboxed rather than deleted: the sandbox keeps a malicious repository prompt
        # from reaching credentials outside the worktree without removing the only path
        # that works when no in-process provider is configured. Deleting it would turn
        # "no provider" from "degraded but functional" into "silently does nothing".
        try:
            popen, cleanup = self._spawn_sandboxed_agent(cmd, cwd)
        except FileNotFoundError:
            _unlink_quietly(cleanup)
            return AgentResult(
                ok=False,
                error=f"agent binary not found: {CLAUDE_BIN}",
                duration_s=time.monotonic() - t0,
            )
        except Exception as e:  # noqa: BLE001
            _unlink_quietly(cleanup)
            return AgentResult(
                ok=False, error=f"{type(e).__name__}: {e}", duration_s=time.monotonic() - t0
            )

        # `sandboxed_spawn_argv` may write a launcher script the caller owns; its contract
        # is that the caller unlinks it once the child is done with it. The streaming
        # path has many exits, so this is a finally rather than a call per return.
        try:
            if streaming:
                return self._run_streaming(popen, t0, timeout_s, cwd=cwd)
        finally:
            _unlink_quietly(cleanup)

        # ── one-shot json path (no activity sink) ──────────────────────────────
        deadline = t0 + timeout_s
        try:
            while True:
                try:
                    stdout, stderr = popen.communicate(timeout=2.0)
                    break  # process exited; stdout/stderr captured
                except subprocess.TimeoutExpired:
                    pass  # not done yet
                if self._stop_check is not None and self._stop_check():
                    self._terminate_group(popen)
                    return AgentResult(
                        ok=False, error="stopped by request", duration_s=time.monotonic() - t0
                    )
                if time.monotonic() > deadline:
                    self._terminate_group(popen)
                    return AgentResult(
                        ok=False,
                        error=f"timeout after {timeout_s}s",
                        duration_s=time.monotonic() - t0,
                    )
        except Exception as e:  # noqa: BLE001
            self._terminate_group(popen)
            return AgentResult(
                ok=False, error=f"{type(e).__name__}: {e}", duration_s=time.monotonic() - t0
            )

        proc = type("P", (), {"returncode": popen.returncode, "stdout": stdout, "stderr": stderr})()

        dur = time.monotonic() - t0
        if proc.returncode != 0:
            # Redact BEFORE the tail cut: a credential straddling the bound keeps its
            # right half otherwise, a fragment no downstream pass can match. Tail (not
            # a head bound) because the END of stderr carries the
            # actionable error; slicing redacted text can at worst split a marker.
            return AgentResult(
                ok=False,
                error=f"exit {proc.returncode}: {redact_via_context(proc.stderr or '')[-400:]}",
                duration_s=dur,
            )
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return AgentResult(
                ok=False,
                error="unparseable claude json envelope",
                duration_s=dur,
                raw={"stdout": proc.stdout[-400:]},
            )

        result_text = envelope.get("result", "")
        cost = float(envelope.get("total_cost_usd", 0.0) or 0.0)
        with self._cost_lock:
            self._total_cost_usd += cost
        if envelope.get("is_error"):
            return AgentResult(
                ok=False,
                text=str(result_text),
                error="claude reported is_error",
                cost_usd=cost,
                duration_s=dur,
                raw=envelope,
            )
        return AgentResult(
            ok=True,
            text=result_text if isinstance(result_text, str) else json.dumps(result_text),
            cost_usd=cost,
            duration_s=dur,
            raw=envelope,
        )

    def _emit_activity(self, ev: dict) -> None:
        try:
            self._on_activity(ev)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — a sink error must never break the run
            pass

    def _run_streaming(
        self, popen, t0: float, timeout_s: float, *, cwd: str | None = None
    ) -> AgentResult:
        """Read ``stream-json`` stdout line-by-line, emit one activity event per turn,
        and assemble the final result. Honors stop + timeout the same as the one-shot
        path. The final ``type:"result"`` line carries the cost + result text."""
        result_text = ""
        cost = 0.0
        is_error = False
        deadline = t0 + timeout_s
        # Drain stderr CONCURRENTLY on a daemon thread into a bounded tail. Reading only
        # stdout below would let a chatty child fill the 64KB stderr pipe and BLOCK —
        # a silent deadlock that only the timeout SIGTERM would break.
        stderr_chunks: deque = deque(maxlen=16)

        def _drain_stderr() -> None:
            try:
                if popen.stderr is None:
                    return
                for chunk in popen.stderr:
                    stderr_chunks.append(chunk)
            except Exception:  # noqa: BLE001 — draining must never raise into the run
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()
        try:
            for line in popen.stdout:  # iterates as lines arrive (line-buffered)
                if self._stop_check is not None and self._stop_check():
                    self._terminate_group(popen)
                    return AgentResult(
                        ok=False, error="stopped by request", duration_s=time.monotonic() - t0
                    )
                if time.monotonic() > deadline:
                    self._terminate_group(popen)
                    return AgentResult(
                        ok=False,
                        error=f"timeout after {timeout_s}s",
                        duration_s=time.monotonic() - t0,
                    )
                obj = parse_json_object_line(line)
                if obj is None:
                    continue
                act = _summarize_stream_event(obj)
                if act:
                    self._emit_activity(act)
                    # AUDIT each tool the agent actually used, not just the one blanket
                    # launch event. The launch is logged `critical=True` before spawn, but
                    # that records "an unattended agent started" — it says nothing about
                    # WHICH tools it then ran, so a forensic query could not answer "did
                    # this run touch a shell?". The stream already carries `tool_use`
                    # blocks (that is what drives the UI feed), so the information was
                    # present and simply not persisted. This brings the subprocess
                    # fallback to the same per-tool visibility the session path gets from
                    # its approval hook.
                    if act.get("kind") == "tool":
                        _audit_fallback_tool(
                            tool=str(act.get("tool") or "tool"),
                            detail=str(act.get("detail") or ""),
                            cwd=cwd,
                        )
                if obj.get("type") == "result":
                    result_text = obj.get("result", "") or ""
                    cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
                    is_error = bool(obj.get("is_error"))
        except Exception as e:  # noqa: BLE001
            self._terminate_group(popen)
            return AgentResult(
                ok=False, error=f"{type(e).__name__}: {e}", duration_s=time.monotonic() - t0
            )
        try:
            popen.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self._terminate_group(popen)
        stderr_thread.join(timeout=2.0)  # let the drain finish; tail comes from its buffer
        # Redact BEFORE the tail cut, same reason as the non-streaming path above.
        stderr_tail = redact_via_context("".join(stderr_chunks))[-400:]

        dur = time.monotonic() - t0
        with self._cost_lock:
            self._total_cost_usd += cost
        if popen.returncode not in (0, None):
            return AgentResult(
                ok=False,
                error=f"exit {popen.returncode}: {stderr_tail}",
                cost_usd=cost,
                duration_s=dur,
            )
        if is_error:
            return AgentResult(
                ok=False,
                text=str(result_text),
                error="claude reported is_error",
                cost_usd=cost,
                duration_s=dur,
            )
        return AgentResult(
            ok=True,
            text=result_text if isinstance(result_text, str) else "",
            cost_usd=cost,
            duration_s=dur,
        )


def _audit_fallback_tool(*, tool: str, detail: str, cwd: str | None) -> None:
    """Record ONE tool the subprocess fallback's agent used.

    NOT ``critical=True``, unlike the launch event: by the time this fires the tool has
    already run inside the sandbox, so there is nothing left to deny and raising here would
    only turn an audit-sink problem into a failed run. The audit-or-DENY decision for this
    path is the launch event plus the pre-spawn governance gate
    (:func:`_governance_denial`) and the shell denylist; this is the audit-or-RECORD half.

    ``detail`` is the same short target hint the UI feed shows (a path, a pattern, a
    command) — agent-influenced text, so it is redacted before it reaches a log that is
    signed as-written, and truncated because a tool input can be arbitrarily long.
    """
    try:
        from kiro_crew.security import redact

        safe_detail = redact(detail)[:200]
    except Exception:  # noqa: BLE001 — never let a redactor failure emit raw agent text
        safe_detail = "[redaction unavailable]"
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="auto-improvement-subprocess",
            agent="auto-improvement",
            source="auto_improvement_loop",
            tool_name=tool,
            tool_kind=tool,
            outcome="invoked",
            resources=str(cwd or ""),
            metadata={"unattended": True, "target": safe_detail},
        )
    except Exception:  # noqa: BLE001 — the tool already ran; a log failure must not fail it
        logger.debug("SEL audit failed for fallback tool %r", tool, exc_info=True)


class SessionAgentRunner:
    """A backend-AGNOSTIC agent runner that drives a Kiro Crew **provider/session** instead
    of shelling out to ``claude -p`` directly (task #23). The provider is whatever the
    Kiro Crew config selects (whichever LLM provider backend the user configured), so ANY
    backend can power the autonomous loop — not just a single CLI on PATH.

    It matches :class:`AgentRunner`'s surface exactly (``run`` → :class:`AgentResult`,
    ``available``, ``total_cost_usd``, ``stop_check``, ``on_activity``), so it is a drop-in
    where the spine constructs a runner. Internally it bridges the spine's SYNC ``run`` to
    the provider's ASYNC ``stream``: it spawns the provider cwd'd into the throwaway
    worktree, streams the task, auto-approves ONLY the allowlisted tools, accumulates the
    assistant text + cost, and honors stop/timeout — then tears the provider down.

    SAFETY / PERMISSIONS: the subagent runs UNATTENDED, so it AUTO-APPROVES every tool/MCP
    the provider requests — blocking on a permission prompt would stall the loop (and there
    is no human to answer). This is safe because the blast radius is already contained: the
    agent works ONLY inside the throwaway, push-disabled worktree, and the spine's edit
    allowlist + RED→GREEN / A-B gate decide what actually ships. It is the session-layer
    equivalent of the subprocess path's ``--dangerously-skip-permissions``."""

    def __init__(
        self,
        *,
        model: str | None = None,
        agent_name: str = "auto-improvement-discovery",
        default_timeout_s: float = 1800.0,
        stop_check=None,
        on_activity=None,
        provider_factory=None,
    ) -> None:
        self.model = model
        # The KIRO AGENT this runner drives. It MUST be an app agent whose tool set
        # is empty (``agents/discovery.json`` ships ``"tools": []``), so the session
        # gets only file/shell tools and NOT the gateway's kirocrew-core MCP tools.
        # Without this the session inherited the default agent's full toolset,
        # including ``spawn_sub_agents`` — and the discovery agent would spawn
        # subagents that orphan (they get an empty parent_session because this
        # runner has no dashboard session), then hang the run polling for them.
        self.agent_name = agent_name
        self.default_timeout_s = default_timeout_s
        self._cost_lock = threading.Lock()
        self._total_cost_usd = 0.0
        self._stop_check = stop_check
        self._on_activity = on_activity if callable(on_activity) else None
        # The Kiro Crew provider factory (``cfg.create_provider_factory()``). Injectable for
        # tests; resolved lazily from config when None so importing this module never loads
        # the whole config/provider stack.
        self._provider_factory = provider_factory

    def total_cost_usd(self) -> float:
        with self._cost_lock:
            return self._total_cost_usd

    @staticmethod
    def available() -> bool:
        """True iff a Kiro Crew provider factory can be built (a backend is configured).
        Lets the backend prefer this runner; there is no subprocess fallback left, so a
        False here means the backend stays offline."""
        try:

            cfg = KiroCrewConfig.load()
            return cfg.create_provider_factory() is not None
        except Exception:  # noqa: BLE001 — any failure → not available, caller falls back
            # Do NOT discard this. ``create_provider_factory`` has a single method-level
            # return and cannot yield None, so False is reachable ONLY from this handler —
            # i.e. only when something raised. The backend's offline reason already tells
            # the operator that "the gateway config load or the provider-factory
            # construction raised", and without this line it can never say WHAT raised.
            # The realistic cause is the acp → client → session → config.loader circular
            # import the loader documents, which resolves only when ``acp`` is imported
            # first.
            logger.warning(
                "SessionAgentRunner.available(): provider factory could not be built, "
                "reporting the agent runner as unavailable",
                exc_info=True,
            )
            return False

    def ensure_agent_registered(self) -> bool:
        """Make ``self.agent_name`` resolvable to kiro-cli by its bare name.

        kiro-cli reads agent definitions from ``~/.kiro/agents/`` and activates one
        via ``session/set_mode`` — an unknown name silently falls back to the
        DEFAULT agent, which carries the full kirocrew-core toolset (including
        ``spawn_sub_agents``). That fallback is exactly what made the discovery
        agent spawn subagents that orphan and hang the run.

        The app bridge namespaces builtin agents (``app--name``) AND does not copy
        a builtin's ``agents/`` into its install dir at all, so neither the bare
        name nor the namespaced one is reliably present. Rather than depend on
        that, write the app's own agent JSON straight to ``~/.kiro/agents/<name>.json``
        under the exact bare name this runner requests. Idempotent, best-effort:
        on failure the run still proceeds (with the default agent), so this
        hardens the tool scoping without becoming a hard dependency.
        """

        try:
            src = Path(__file__).resolve().parent.parent / "agents" / "discovery.json"
            if not src.is_file():
                return False
            data = json.loads(src.read_text(encoding="utf-8"))
            if str(data.get("name") or "") != self.agent_name:
                # Only self-register the agent this runner actually drives.
                return False
            # kiro_agents_dir() honors KIRO_HOME; hard-coding ~/.kiro would write
            # into the real agent home during tests and break agent-home isolation.
            from kiro_crew import agent as agent_mod
            from kiro_crew.config.paths import kiro_agents_dir

            dest_dir = kiro_agents_dir()
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{self.agent_name}.json"
            desired = src.read_bytes()
            # The shared agents dir belongs to the instance that owns it: from a data
            # home that does not, write nothing and run with whatever the owner left,
            # under the same decision every other writer of that directory asks.
            with agent_mod.agents_spec_lock(dest_dir):
                if agent_mod._declined_foreign_spec_write(dest):
                    return dest.is_file() and _same_bytes(dest, desired)
                return _install_agent_copy(src, dest, desired, self.agent_name)
        except Exception:  # noqa: BLE001 — never block a run on agent registration
            logger.warning("could not self-register agent %s", self.agent_name, exc_info=True)
            return False

    def _resolve_factory(self):
        if self._provider_factory is not None:
            return self._provider_factory

        cfg = KiroCrewConfig.load()
        self._provider_factory = cfg.create_provider_factory()
        return self._provider_factory

    def _allows_tool(self, event: object, tool: str, allowed: list[str] | None) -> bool:
        if getattr(event, "is_shell", False):
            return _tool_permitted("Bash", allowed)
        return _tool_permitted(tool, allowed)

    def _emit_activity(self, ev: dict) -> None:
        if self._on_activity is None:
            return
        try:
            self._on_activity(ev)
        except Exception:  # noqa: BLE001 — a sink error must never break the run
            pass

    def run(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        append_system: str | None = None,
        max_turns: int = 40,
        timeout_s: float | None = None,
        add_dirs: list[str] | None = None,
    ) -> AgentResult:
        """Drive the configured provider through the agentic tool-loop for ``prompt``,
        cwd'd into ``cwd``. Returns an :class:`AgentResult`; never raises on a provider-side
        failure (mirrors :meth:`AgentRunner.run`)."""
        timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
        t0 = time.monotonic()
        # This thread runs its own event loop, which the gateway's kiro-cli floor
        # gate cannot hold. Wait here, bounded, BEFORE the config load: that load
        # resolves a defaulted backend, and must see the floor's verdict.
        from kiro_crew.agent_sdk.kas_floor_wait import wait_kas_floor_verdict_blocking

        wait_kas_floor_verdict_blocking()
        try:
            factory = self._resolve_factory()
        except Exception as e:  # noqa: BLE001
            return AgentResult(
                ok=False,
                error=f"provider factory unavailable: {e}",
                duration_s=time.monotonic() - t0,
            )
        if factory is None:
            return AgentResult(
                ok=False, error="no Kiro Crew provider configured", duration_s=time.monotonic() - t0
            )
        try:
            return asyncio.run(
                self._run_async(
                    prompt,
                    factory=factory,
                    cwd=cwd,
                    allowed_tools=allowed_tools,
                    append_system=append_system,
                    timeout_s=timeout_s,
                    t0=t0,
                    max_turns=max_turns,
                )
            )
        except Exception as e:  # noqa: BLE001 — never raise into the spine loop
            return AgentResult(
                ok=False, error=f"{type(e).__name__}: {e}", duration_s=time.monotonic() - t0
            )

    async def _run_async(
        self,
        prompt,
        *,
        factory,
        cwd,
        append_system,
        timeout_s,
        t0,
        max_turns=0,
        allowed_tools=None,
        session_key=None,
        provider=None,
        governance_agent: str = "",
    ) -> AgentResult:
        # ``governance_agent`` is the identity the platform governance gate judges
        # tool requests under; it defaults to ``self.agent_name``. A crew member
        # runs on a shared TEMPLATE (``self.agent_name`` selects the provider and
        # the tool set) but is governed under its own member ALIAS, which the
        # dashboard and messaging paths also key their profiles on — so a member
        # whose alias carries a task-scoped profile is held to that profile here too.
        governance_agent = governance_agent or self.agent_name
        # Build a provider for THIS task. The factory's FIRST positional is the
        # session_key (namespaces the provider's work dir); ``agent`` selects the
        # KIRO AGENT — which is what scopes the tool set. Passing the app's
        # tool-restricted agent here is the fix for the subagent-orphan hang: an
        # agent with ``"tools": []`` gets file/shell tools only, so it cannot reach
        # ``spawn_sub_agents``. (The previous call passed the agent NAME as the
        # session_key and the MODEL as ``agent``, so no scoped agent was selected
        # and the session inherited the default agent's full kirocrew-core toolset.)
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            STOP_REASON_STALE_RECOVER,
        )

        # Stable across processes (Python's builtin hash() is per-run salted): the
        # session key only needs to be unique per worktree, not secret.
        digest = hashlib.sha256((cwd or self.agent_name).encode()).hexdigest()[:12]
        session_key = session_key or f"auto-improvement-{digest}"
        owns_provider = provider is None
        cost = 0.0
        cost_accounted = False
        try:
            if owns_provider:
                try:
                    provider = factory(session_key, agent=self.agent_name, cwd=cwd)
                except TypeError:
                    # Older/other factories may not accept cwd / agent kwargs.
                    try:
                        provider = factory(session_key, agent=self.agent_name)
                    except TypeError:
                        provider = factory(session_key)
                await provider.start()
            text_parts: list[str] = []
            # Streamed assistant text arrives as MANY tiny provider chunks (often sub-word
            # token slivers like "ismatched-" / "ncation would"). Emitting one activity
            # event per raw chunk floods the feed with unreadable fragments. Instead BUFFER
            # the chunks and flush only COHERENT lines (on a newline / sentence boundary, or
            # when the buffer gets long), so the live feed reads as whole thoughts.
            text_buf = _LineBuffer(
                lambda line: self._emit_activity({"kind": "text", "detail": line})
            )
            cost = 0.0
            tool_calls = 0  # ACP turn counter — enforces max_turns on the session path
            # Per-tool_call_id detail already shown in the feed, so the EVENT_TOOL_CALL_UPDATE
            # refinement only emits an UPGRADED line when it adds a real target (the file
            # path), not a duplicate. Fixes "⚙ read · Read File" (no filename): the initial
            # tool_call has empty rawInput; the path arrives in the refinement.
            announced_tool_detail: dict[str, str] = {}
            # Kiro permission frames can omit kind. Retain the classification
            # from the preceding tool_call with the SAME id, never its title.
            announced_tool_kind: dict[str, str] = {}
            deadline = t0 + timeout_s
            full_prompt = (append_system + "\n\n" + prompt) if append_system else prompt
            # HARD WALL-CLOCK WATCHDOG (operator directive): a plain `async for` only checks
            # the deadline BETWEEN events, so a long in-turn await (the agent
            # thinking/reading for minutes inside one stream step) can blow past timeout_s
            # unbounded — a runaway multi-minute discovery run. Drive the stream as an
            # explicit async iterator and bound EACH event fetch with asyncio.wait_for on the
            # REMAINING budget. A stall is force-cancelled and we return the accumulated text
            # (so any findings already streamed survive). Falls back to the plain async-for if
            # the provider stream isn't a true async iterator.
            stream = provider.stream(full_prompt)
            ait = stream.__aiter__() if hasattr(stream, "__aiter__") else None

            def _finish(*, ok: bool, error: str = "") -> AgentResult:
                # Single exit point: fold the cost ALREADY SPENT into the running total on
                # EVERY path (stop / timeout / max_turns / success), so the driver's live
                # ``--max-cost`` meter (cost_meter=runner.total_cost_usd) sees real spend.
                # The tool calls billed before an early return are real money — omitting
                # them (accruing only on success) would make the ceiling under-count
                # for timeout/max_turns, the EXPECTED common outcomes.
                nonlocal cost_accounted
                with self._cost_lock:
                    self._total_cost_usd += cost
                cost_accounted = True
                return AgentResult(
                    ok=ok,
                    error=error,
                    text="".join(text_parts),
                    cost_usd=cost,
                    duration_s=time.monotonic() - t0,
                )

            # Stop reasons with which the backend ends a turn it never finished. Set
            # ``unfinished`` when the turn ends that way, or when the stream ends with no
            # EVENT_COMPLETE at all.
            unfinished_reasons = (_STOP_REASON_PROMPT_TIMEOUT, STOP_REASON_STALE_RECOVER)
            unfinished = ""

            while True:
                if self._stop_check is not None and self._stop_check():
                    return _finish(ok=False, error="stopped by request")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Wall-clock expired between events — return accumulated text (the
                    # discovered=0 fix: never discard a late/partial JSON answer).
                    return _finish(ok=False, error=f"timeout after {timeout_s}s")
                if ait is None:  # non-iterator stream — can't watchdog; degrade gracefully
                    break
                try:
                    ev = await asyncio.wait_for(ait.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    unfinished = f"{_UNFINISHED_TURN_ERROR} (the provider stream ended first)"
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    # In-turn stall exceeded the budget — force-cancel and harvest text.
                    return _finish(ok=False, error=f"timeout after {timeout_s}s")
                kind = getattr(ev, "kind", "")
                reported_cost = getattr(ev, "cost_usd", 0.0) or getattr(
                    getattr(ev, "usage", None), "cost_usd", 0.0
                )
                if reported_cost:
                    cost = float(reported_cost) or cost
                if kind == EVENT_PERMISSION_REQUEST:
                    # AUTO-APPROVE EVERY tool/MCP the provider asks for — never block on a
                    # permission prompt (the subagent runs unattended; a blocked tool would
                    # stall the whole loop). The blast radius is already contained: the
                    # agent works ONLY inside the throwaway, push-disabled worktree, and the
                    # spine's edit-allowlist + RED→GREEN/A-B gate decide what actually ships.
                    # This is the session-layer equivalent of the old subprocess path's
                    # ``--dangerously-skip-permissions``.
                    #
                    # Approve INLINE (await), exactly as the production task-executor /
                    # channel consumers do (``async for event in client.stream(): await
                    # client.approve_tool(event.request_id)``). approve_tool is just a stdin
                    # write + drain — it does NOT require the stream generator to advance, so
                    # it cannot deadlock. An earlier ``create_task`` variant let the tool run
                    # (the file got written) but the turn never reached EVENT_COMPLETE: the
                    # approval landed out-of-order relative to the read loop, the agent never
                    # saw its tool result, and the run hung to the timeout. Inline await is
                    # the proven pattern and completes the turn.
                    # Identity comes from the provider's ``kind`` only, never from
                    # ``tool_purpose``: that is agent-written display text, and the
                    # allowlist below substring-matches it, so a purpose such as
                    # "Read the module" would pass a ``["Read"]`` allowlist for a
                    # write. An unnamed request stays unnamed and is refused.
                    tool = getattr(ev, "tool_kind", "") or announced_tool_kind.get(
                        getattr(ev, "tool_call_id", ""), ""
                    )
                    rid = getattr(ev, "request_id", "")
                    # ENFORCE the caller's allowlist. `allowed_tools` was accepted by `run`
                    # and never forwarded here, so every request was granted whatever it
                    # asked for. That matters most for a WATCHER: it reads PR comments
                    # through `gh`, so a malicious comment could ask for a shell and get it,
                    # against an authenticated GitHub session. Deny-by-default applies only
                    # when a caller supplied a list — an absent list imposes no allowlist.
                    # FIRST gate: the platform governance chokepoint (`hooks.on_tool_call`)
                    # — the enterprise ceiling, builtin denied rules, and sensitive-path
                    # (~/.aws/~/.ssh) blocks that the dashboard/Slack paths honor. This
                    # unattended runner must not rely only on the app-local checks below.
                    gov = _governance_denial(
                        ev, session_key=session_key, agent=governance_agent, tool_kind=tool
                    )
                    if gov:
                        logger.warning("refusing tool %r — governance: %s", tool, gov)
                        # The gate names its own cause: a hook's deny judged
                        # the call (policy), a hook LAYER that raised judged
                        # nothing (hook_error). A bare string from the gate
                        # is a hook verdict.
                        gov_cause = (
                            gov.cause if isinstance(gov, _GovernanceDeny) else DENY_CAUSE_POLICY
                        )
                        await self._reject(
                            provider,
                            rid,
                            tool=tool,
                            session_key=session_key,
                            event=ev,
                            cause=gov_cause,
                            reason=gov,
                            error=(
                                "governance_hook_unavailable"
                                if gov_cause == DENY_CAUSE_HOOK_ERROR
                                else "governance_deny"
                            ),
                        )
                        self._emit_activity(
                            {"kind": "tool", "tool": tool or "tool", "detail": f"refused: {gov}"}
                        )
                        continue
                    refusal = shell_command_refusal(_requested_command(ev))
                    if refusal:
                        logger.warning("refusing tool %r — %s", tool, refusal)
                        # The app-local shell denylist judged the command itself.
                        await self._reject(
                            provider,
                            rid,
                            tool=tool,
                            session_key=session_key,
                            event=ev,
                            cause=DENY_CAUSE_POLICY,
                            reason=refusal,
                            error="shell_denylist",
                        )
                        self._emit_activity(
                            {
                                "kind": "tool",
                                "tool": tool or "tool",
                                "detail": f"refused: {refusal}",
                            }
                        )
                        continue
                    if not self._allows_tool(ev, tool, allowed_tools):
                        logger.warning("refusing tool %r — not in the caller's allowed_tools", tool)
                        # The caller's allowlist is what this SURFACE permits;
                        # nothing about the call was judged.
                        await self._reject(
                            provider,
                            rid,
                            tool=tool,
                            session_key=session_key,
                            event=ev,
                            cause=DENY_CAUSE_SURFACE_POLICY,
                            reason=_allowlist_deny_reason(allowed_tools),
                        )
                        self._emit_activity(
                            {"kind": "tool", "tool": tool or "tool", "detail": "refused"}
                        )
                        continue
                    await self._approve(provider, rid, tool=tool, session_key=session_key, event=ev)
                    if tool:
                        text_buf.flush()  # close the current thought before the tool line
                        self._emit_activity({"kind": "tool", "tool": tool, "detail": "approved"})
                elif kind == EVENT_TOOL_CALL:
                    # The INITIAL tool_call from claude-agent-acp often has empty rawInput, so
                    # the title is the GENERIC tool label ("Read File") with no path and
                    # tool_input is "" — that's the bare "⚙ read · Read File" the operator
                    # kept seeing. The real target (e.g. the file path) arrives in the
                    # follow-up EVENT_TOOL_CALL_UPDATE refinement. So: emit the best detail we
                    # have NOW, remember what we showed per tool_call_id, and let the
                    # refinement below UPGRADE the line once the path lands.
                    detail = _tool_detail(ev)
                    tcid = getattr(ev, "tool_call_id", "") or ""
                    if tcid:
                        announced_tool_detail[tcid] = detail
                        announced_tool_kind[tcid] = getattr(ev, "tool_kind", "") or ""
                    text_buf.flush()  # close the current thought before the tool line
                    self._emit_activity(
                        {"kind": "tool", "tool": getattr(ev, "tool_kind", "tool"), "detail": detail}
                    )
                    # ENFORCE max_turns on the ACP/session path. Unlike the subprocess runner
                    # (which passes --max-turns to claude), the streaming provider has no turn
                    # limit of its own — so a thinking agent with no terminal commitment reads
                    # tools until the wall-clock (the discovered=0 over-investigation).
                    # Counting tool calls and stopping at the cap is the real
                    # convergence lever: it ends the stream and returns the accumulated text
                    # (a late JSON answer survives). max_turns<=0 disables the cap.
                    tool_calls += 1
                    if max_turns and tool_calls >= max_turns:
                        text_buf.flush()
                        return _finish(ok=False, error=f"max_turns ({max_turns}) reached")
                elif kind == EVENT_TOOL_CALL_UPDATE:
                    # The refinement carries the REAL target (rawInput now populated → the
                    # file path / command). If it adds detail the initial bare line lacked,
                    # emit an UPGRADED line so the feed shows WHICH file was read — the fix for
                    # "⚙ read · Read File" with no filename. Dedup by tool_call_id so a
                    # refinement that adds nothing new doesn't double the feed.
                    refined = _tool_detail(ev)
                    tcid = getattr(ev, "tool_call_id", "") or ""
                    prior = announced_tool_detail.get(tcid, "") if tcid else ""
                    if refined and refined != prior and _detail_is_richer(refined, prior):
                        if tcid:
                            announced_tool_detail[tcid] = refined
                        text_buf.flush()
                        self._emit_activity(
                            {
                                "kind": "tool",
                                "tool": getattr(ev, "tool_kind", "tool"),
                                "detail": refined,
                            }
                        )
                elif kind == EVENT_TEXT_CHUNK:
                    txt = getattr(ev, "text", "") or ""
                    if txt:
                        text_parts.append(txt)  # full text kept for the return value
                        text_buf.feed(txt)  # feed → flushes whole lines to the feed
                elif kind == EVENT_COMPLETE:
                    stop_reason = str(getattr(ev, "stop_reason", "") or "")
                    if stop_reason in unfinished_reasons:
                        unfinished = f"{_UNFINISHED_TURN_ERROR} (stop reason {stop_reason!r})"
                    break
            text_buf.flush()  # emit any trailing partial line at turn end
            if unfinished:
                # Not a success, whatever text arrived. The partial text is kept for the
                # caller, as the watchdog's timeout keeps it.
                return _finish(ok=False, error=unfinished)
            return _finish(ok=True)
        finally:
            if not cost_accounted:
                with self._cost_lock:
                    self._total_cost_usd += cost
            if provider is not None and owns_provider:
                try:
                    await provider.shutdown()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    async def _reject(
        provider,
        rid,
        *,
        cause: str | None,
        reason: str = "",
        error: str = "not_in_allowed_tools",
        event: object | None = None,
        tool: str = "",
        session_key: str = "",
    ) -> None:
        """Refuse a tool request, RECORD it, tell the model WHO refused it, answer the wire.

        A refusal is more interesting than an approval — it is what an injected instruction
        looks like — so it is audited even though the tool never ran. Not ``critical``: the
        tool is already being denied, so failing the whole run because the log is
        unwritable would trade a working refusal for an outage.

        *cause* is REQUIRED and says whether the HOST refused this call. A rejected
        permission reaches the model as kiro-cli's fixed "User denied tool execution";
        every deny on this unattended surface is a host deny (no person is attached to
        say no), so without a notice the model reads a refusal that never happened and
        abandons or routes around a call nobody objected to. A host cause
        (``DENY_CAUSE_POLICY`` for a hook's or the shell denylist's verdict on the call
        itself, ``DENY_CAUSE_HOOK_ERROR`` for a governance hook layer that raised,
        ``DENY_CAUSE_SURFACE_POLICY`` for the caller's allowlist refusing a tool it
        does not permit) steers the in-band notice through
        ``llm_helpers._steer_host_deny`` BEFORE the reject -- while the permission
        request is still unanswered the turn is provably in flight, which is what gets
        the notice queued rather than dropped (see ``kiro_crew.deny_notice``). ``None``
        is the explicit verdict that no notice is owed; no caller in this module writes
        it today, and one that does has to say so rather than inherit a default.
        *reason* is the host's own wording for the notice; *error* is the SEL row's
        closed-enum tag for WHICH gate refused (``governance_deny``,
        ``governance_hook_unavailable``, ``shell_denylist``, or the allowlist
        default), so an operator reading the ledger can tell a hook outage from a
        policy refusal. *event* is the permission event (its title names the
        call in the notice, its ``request_id`` is what the helper answers if the steer
        is cancelled mid-flight); without one the funnel names the call by *tool*.

        The audit lands FIRST, before the steer and the reject, as on every other deny
        surface: the steer is one more bounded await on the ACP pipe, and a backend that
        stops reading stdin cancels this coroutine at the turn deadline with the
        decision acted on and never audited if the row came last.
        """
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=session_key or "auto-improvement",
                agent="auto-improvement",
                source="auto_improvement_loop",
                tool_name=tool or "tool",
                tool_kind=str(tool or ""),
                outcome="denied",
                request_id=rid,
                error=error,
            )
        except Exception:  # noqa: BLE001 - the denial stands even if the audit fails
            logger.warning("SEL audit failed for a REFUSED tool %r", tool)
        if cause is not None:
            await _steer_host_deny(
                provider,
                event if event is not None else _bare_event(rid, tool),
                reason,
                cause=cause,
            )
        try:
            await provider.reject_tool(rid)
        except Exception:  # noqa: BLE001 - the agent's own timeout covers this
            logger.debug("reject_tool failed for %r", tool, exc_info=True)

    @staticmethod
    async def _approve(
        provider, rid, *, tool: str = "", session_key: str = "", event: object | None = None
    ) -> None:
        """Auto-approve a tool permission, but only once the approval is on the record.

        AUDIT-OR-DENY. The approval is unattended and unconditional, so the Security
        Event Log is the only remaining record that it happened — which is exactly the
        case ``sel().log_tool_invocation(critical=True)`` documents. If the event cannot
        be written we REJECT the tool instead of running it unaudited: an unattended
        auto-approve that leaves no trace is the thing the log exists to prevent.

        The blast radius of the approval itself is still contained elsewhere (throwaway
        push-disabled worktree, edit allowlist, RED→GREEN gate); this adds the audit
        trail those controls cannot provide.

        Never raises: a failed approval just means that one tool waits out the agent's
        own retry/timeout, which is a stalled candidate rather than a broken run.
        """
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=session_key or "auto-improvement",
                agent="auto-improvement",
                source="auto_improvement_loop",
                tool_name=tool or "tool",
                tool_kind=tool,
                outcome=OUTCOME_PENDING_APPROVAL,
                request_id=rid,
                metadata={"unattended": True, "containment": "worktree+allowlist+gate"},
                critical=True,
            )
        except Exception as exc:  # noqa: BLE001 - audit failure must deny, not approve
            logger.warning("SEL audit failed for tool %r — rejecting instead of approving", tool)
            # A HOST deny with nothing judged: tell the model so in-band before
            # the wire hands it kiro-cli's "User denied tool execution". Not
            # routed through ``_reject`` -- its own audit row is exactly what
            # just failed to write.
            await _steer_host_deny(
                provider,
                event if event is not None else _bare_event(rid, tool),
                _AUDIT_UNAVAILABLE_REASON,
                cause=DENY_CAUSE_AUDIT_UNAVAILABLE,
            )
            try:
                await provider.reject_tool(rid)
            except Exception:  # noqa: BLE001 - the agent's own timeout covers this
                logger.debug("reject_tool after audit failure also failed: %s", exc)
            return
        try:
            # ONE-SHOT, never `always=True`. Persistent approval tells the provider to stop
            # sending permission requests for matching calls (the base contract: "the user
            # picked 'always allow'", and ACP backends may turn it into an `addRules`
            # suggestion) — so every LATER call would skip both gates above: the per-tool
            # allowlist/denylist check AND the `critical=True` audit-or-deny write. The
            # unattended loop is exactly the caller that must not buy a blanket exemption
            # with its first approval; re-deciding per call is the whole point of routing
            # through here.
            approval_sent = await provider.approve_tool(rid)
            outcome = (
                OUTCOME_REJECTED_TRANSPORT_FLOOR if approval_sent is False else "auto_approved"
            )
            sel().log_tool_invocation(
                session_key=session_key or "auto-improvement",
                agent="auto-improvement",
                source="auto_improvement_loop",
                tool_name=tool or "tool",
                tool_kind=tool,
                outcome=outcome,
                request_id=rid,
                metadata={"unattended": True, "containment": "worktree+allowlist+gate"},
            )
        except Exception:  # noqa: BLE001
            pass


# This module is the facade of the spine agent runner and the path every caller imports.
# ``AgentRunner``, ``SessionAgentRunner`` and the helpers only they call stay here; the rest
# lives in owner modules beside it, and new work goes to the owner of its responsibility:
#
#   ``agent_runner_gate`` -- whether a tool request is refused: the governance chokepoint,
#   the shell-command denylist and the caller's allowlist
#   ``agent_runner_feed`` -- the activity feed's whole lines and tool-call details
#   ``agent_runner_authoring`` -- authoring a bug or perf fix in a candidate worktree
#
# A name this module binds is read here, and a moved name it does not read is served from
# its owner. A write or delete through this module -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner that binds the same object
# (``kiro_crew._facade_mirror``), so a patch of ``agent_runner.HookManager`` or
# ``agent_runner.require_pinned`` reaches the moved code as it did when this was one
# module. The imports at the top stay bound even where this module's own code does not
# read them (``# noqa: F401``): they are its surface, which callers read and tests patch.
# Owners never import this module at run time. ``test/test_agent_runner_owner_bindings.py``
# pins the owners, the mirror and that direction.
#: The owner modules, lowest layer first: the first holder of a name answers its reads.
#: Named from this module's own package, so the family also loads under the app loader's name.
_OWNERS = (
    f"{__package__}.agent_runner_gate",
    f"{__package__}.agent_runner_feed",
    f"{__package__}.agent_runner_authoring",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner_authoring import (  # noqa: F401
        _GIT_SAFE_CONFIG,
        _adopt_authored_test,
        _repro_test_dir,
        author_bug_fix,
        author_perf_fix,
    )
    from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner_feed import (  # noqa: F401
        _LINE_FLUSH_LEN,
        _TOOL_TARGET_KEYS,
    )
    from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner_gate import (  # noqa: F401
        _COMMAND_WRAPPERS,
        _FORBIDDEN_BINARIES,
        _FORBIDDEN_SUBCOMMANDS,
        _MAX_UNWRAP_DEPTH,
        _SHELL_BINARIES,
        _VALUE_TAKING_OPTIONS,
        _WRAPPER_VALUE_TAKING_LONG_OPTIONS,
        _refusal,
        _shell_words,
    )
