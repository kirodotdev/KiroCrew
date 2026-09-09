"""Advisor composition: reviewer results into guarded, severity-routed delivery.

``AdvisorDispatcher`` is the policy seam between a reviewer's raw output and
the parent session: validate the envelope (malformed output degrades, never
injects), admit each note through the emission guard, then route by severity
-- a ``blocker`` goes through advisory delivery (steer or preserve), while
``nit`` and ``concern`` become preserved Advisor cards plus pending context
and never steer.

``build_reviewer_runtime`` wires the real collaborators for the shared
reviewer pool: an ACP runtime running the packaged read-only
``kirocrew-advisor`` agent, orphan-sweep PID protection, and a prompt
function that feeds observation updates and parses the reviewer's envelope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew.advisor import read_gate
from kiro_crew.advisor.delivery import (
    ADVISORY_DISCARDED,
    ADVISORY_REVOKED,
    ADVISORY_STEERED,
    AdvisoryEnvelope,
    advisory_message,
    deliver_advisory,
    preserve_advisory,
)
from kiro_crew.advisor.guard import EmissionGuard
from kiro_crew.advisor.output import (
    AdvisorNote,
    MalformedReviewerOutput,
    parse_reviewer_envelope,
)
from kiro_crew.advisor.runtime import AdvisorReviewerRuntime, ReviewerSession
from kiro_crew.advisor.usage import advisor_usage_kwargs

# Module scope per top-level-imports: `kiro_crew.agent` imports nothing from
# the advisor package, so there is no cycle to except around.
from kiro_crew.agent import (
    apply_allowed_tools_ceiling,
    atomic_json_write,
    kiro_agents_dir_path,
)
from kiro_crew.agent_sdk import oneshot
from kiro_crew.agent_sdk.oneshot import AgentRuntimeHandle
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers import usage as usage_handlers
from kiro_crew.hooks import TOOL_DENY, HookManager, hooks_config_from_config_dict
from kiro_crew.platform.tool_paths import target_paths
from kiro_crew.sandbox import credential_mask_applies
from kiro_crew.security.paths import is_sensitive_path, path_contains_sensitive

logger = logging.getLogger(__name__)


class AdvisorSpecError(RuntimeError):
    """The reviewer agent spec could not be verified or persisted.

    Raised to ABORT runtime creation: launching with a spec whose security
    fields (tools, allowedTools, mcpServers, includeMcpJson) could not be
    restored or ceiling-filtered would fail open -- a stale auto-approval
    grant would bypass the PreToolUse gate."""


ADVISOR_AGENT_NAME = "kirocrew-advisor"
# Every build of the managed spec opens its description with this sentence;
# a regular file at the reserved path without it is somebody's configuration.
MANAGED_DESCRIPTION_PREFIX = "Hidden read-only session reviewer for the Advisor feature."


def _is_managed_spec_file(target: Path) -> bool:
    try:
        return str(
            json.loads(target.read_text(encoding="utf-8")).get("description", "")
        ).startswith(MANAGED_DESCRIPTION_PREFIX)
    except Exception:  # noqa: BLE001 - unparsable is not ours
        return False


def ensure_advisor_agent_installed(agents_dir: Path | None = None) -> Path:
    """Materialize the packaged reviewer agent spec into the agents dir.

    The reviewer runtime spawns with ``agent=ADVISOR_AGENT_NAME``, which only
    resolves once the JSON exists in the kiro agents directory. The packaged
    spec is the ONLY source: on every launch it is parsed, its ``allowedTools``
    filtered through the governance ceiling, and written over the managed file
    on disk (tmp+rename, so kiro-cli never reads a partial file and a planted
    symlink is replaced rather than followed). Nothing on disk flows into the
    spec, so no stale grant, hand edit or link can widen the read-only
    boundary. The one thing read from disk is whether a REGULAR file already
    at the reserved path is the managed spec at all: a user file squatting the
    name is refused with a name-collision error rather than destroyed.
    This is a managed agent, not a user-customizable one.
    """
    if not credential_mask_applies("strict", is_kiro_cli=True):
        # The strict sandbox's credential mask is the kernel-enforced floor
        # under the preToolUse read gate. A host that cannot apply it gets no
        # reviewer rather than one whose reads can reach ~/.ssh.
        raise AdvisorSpecError(
            "the Advisor reviewer requires the strict sandbox's credential mask and this "
            "host cannot apply it (no sandbox backend, sandboxing off, or Kiro's own sandbox "
            "takes over the child on macOS and Windows)"
        )
    if agents_dir is None:
        agents_dir = kiro_agents_dir_path()
    agents_dir = Path(agents_dir)
    target = agents_dir / f"{ADVISOR_AGENT_NAME}.json"
    if target.is_file() and not target.is_symlink() and not _is_managed_spec_file(target):
        raise AdvisorSpecError(
            f"advisor agent name collision: {target} exists and is not the managed reviewer "
            "spec; rename or remove it"
        )
    cwd = reviewer_process_cwd()
    if (cwd / ".kiro").exists():
        # kiro-cli resolves ``--agent`` (and steering, MCP config) against
        # ``<cwd>/.kiro`` before the global directories. The cwd is on the
        # agents' write-protected list; if something got there anyway, refuse
        # to spawn a reviewer whose spec could be the planted one.
        raise AdvisorSpecError(
            f"advisor cwd {cwd} contains a .kiro tree that could shadow the managed spec; "
            "remove it"
        )
    # kiro-cli approves builtin reads natively but runs the spec's preToolUse
    # hooks first and blocks on exit 2; a hook command it cannot execute is
    # fail-OPEN (the read runs ungated), so the interpreter is checked here.
    interpreter = Path(read_gate._interpreter())
    if not (interpreter.is_file() and os.access(interpreter, os.X_OK)):
        raise AdvisorSpecError(
            f"advisor read-gate hook interpreter {interpreter} is not executable; "
            "refusing to spawn a reviewer whose builtin reads would run ungated"
        )
    cwd.mkdir(parents=True, exist_ok=True)
    failure = read_gate.self_test(cwd=cwd)
    if failure:
        raise AdvisorSpecError(f"advisor read-gate hook self-test failed: {failure}")
    packaged = Path(__file__).parent / "agents" / f"{ADVISOR_AGENT_NAME}.json"
    try:
        spec = json.loads(packaged.read_text(encoding="utf-8"))
        apply_allowed_tools_ceiling(spec, source="advisor.ensure_agent_installed")
        spec["hooks"] = {
            "preToolUse": [
                {"matcher": tool, "command": read_gate.hook_command()}
                for tool in sorted(_REVIEWER_READ_ONLY_TOOLS)
            ]
        }
        agents_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(target, spec)
        cwd.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 - never spawn against an unverified spec
        raise AdvisorSpecError(f"advisor spec install failed for {target}") from exc
    return target


def render_update_prompt(update: Any) -> str:
    """One observation update as the reviewer's next prompt."""
    phase = (
        (
            f"FINAL update (host-fabricated terminal, stop reason: "
            f"{update.stop_reason or 'unknown'}) -- the turn did not complete "
            "on its own; treat completion-dependent conclusions with caution"
            if update.synthetic
            else f"FINAL update (turn completed, stop reason: {update.stop_reason or 'unknown'})"
        )
        if not update.in_progress
        else "in progress"
    )
    lines = [
        f"[Session update seq={update.seq} epoch={update.epoch} "
        f"turn={update.turn_id or '?'} -- {phase}]",
    ]
    for segment in update.segments:
        lines.append(f"Assistant said:\n{segment}")
    for record in update.tool_results:
        suffix = " (truncated)" if record.truncated else ""
        lines.append(f"Tool {record.tool_name} returned{suffix}:\n{record.payload}")
    lines.append(
        "Review the work so far. Respond ONLY with the JSON envelope "
        '{"version": 1, "notes": [...]}; an empty notes list means the work '
        "is sound."
    )
    return "\n\n".join(lines)


class AdvisorDispatcher:
    """Routes validated, guard-admitted reviewer notes to the parent."""

    def __init__(self, guard: EmissionGuard) -> None:
        self._guard = guard

    async def dispatch(
        self,
        state: Any,
        slot: Any,
        raw_result: object,
        *,
        advisor_update_id: str,
        steer_allowed: bool = True,
        authorized: Callable[[], bool] | None = None,
    ) -> dict[str, int]:
        """Deliver one reviewer result; return outcome counts.

        Outcome keys: ``steered``, ``preserved``, ``degraded``, ``revoked``.
        An empty dict means nothing needed delivery (no notes, or all
        suppressed).

        ``authorized`` is the LIVE authorization predicate, re-evaluated
        before every note: a blocker steer awaits the running turn, so an
        opt-out, reset, compaction or slot rebind can land between two notes
        of one review. Once it returns False the remaining notes are counted
        ``revoked`` and nothing more persists, stages or steers.
        """
        self._guard.begin_update()
        try:
            notes = parse_reviewer_envelope(raw_result)
        except MalformedReviewerOutput:
            logger.warning(
                "advisor reviewer returned malformed output for slot %s; degrading",
                getattr(slot, "key", "?"),
            )
            return {"degraded": 1}
        logger.info(
            "advisor reviewer returned %d note(s) for slot %s",
            len(notes),
            getattr(slot, "key", "?"),
        )
        outcomes: dict[str, int] = {}
        revoked = False
        for note in notes:
            if revoked or (authorized is not None and not authorized()):
                revoked = True
                outcomes["revoked"] = outcomes.get("revoked", 0) + 1
                continue
            admitted = self._guard.admit(note)
            if admitted is None:
                continue
            outcome = await self._deliver(
                state,
                slot,
                admitted,
                advisor_update_id,
                steer_allowed=steer_allowed,
                authorized=authorized,
            )
            if outcome == "revoked":
                revoked = True
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return outcomes

    async def _deliver(
        self,
        state: Any,
        slot: Any,
        note: AdvisorNote,
        advisor_update_id: str,
        *,
        steer_allowed: bool = True,
        authorized: Callable[[], bool] | None = None,
    ) -> str:
        envelope = AdvisoryEnvelope(
            severity=note.severity,
            advisor_update_id=f"{advisor_update_id}:{uuid.uuid4().hex[:8]}",
            note_text=note.text,
            evidence=note.evidence or "",
        )
        if note.severity == "blocker" and steer_allowed and self._guard.may_interrupt():
            outcome = await deliver_advisory(state, slot, note, envelope, proceed=authorized)
            if outcome == ADVISORY_STEERED:
                self._guard.note_interruption()
                return "steered"
            if outcome in (ADVISORY_DISCARDED, ADVISORY_REVOKED):
                return outcome
            return "preserved"
        # nit / concern: visible card + staged context, never a steer.
        preserve_advisory(state, slot, advisory_message(note), envelope)
        return "preserved"


#: The reviewer's entire tool surface. kiro-cli approves these builtin reads
#: natively (no permission request), so the managed spec gates them BEFORE
#: execution with a kiro-cli ``preToolUse`` hook running
#: :mod:`kiro_crew.advisor.read_gate`, which applies ``advisor_permission_gate``
#: -- the read-only ceiling, the platform's governance hook, and
#: ``is_sensitive_path`` on every path argument -- and blocks on exit 2. The
#: STRICT sandbox's credential mask stays as the kernel-enforced floor
#: (``ensure_advisor_agent_installed`` refuses without it).
_REVIEWER_READ_ONLY_TOOLS = frozenset({"fs_read", "grep"})
#: The read-only tools that recurse from a root and so default to the cwd.
_REVIEWER_RECURSIVE_TOOLS = frozenset({"grep"})


#: Argument keys that carry filesystem paths for the reviewer's tools.
def _hook_manager() -> Any:
    """The platform's PreToolUse gate, built the way the unattended precedents
    build it: ``hooks_config_from_config_dict`` reads the operator's denied
    command state from the keystone file, which the plain config section does
    not. Module-level indirection so tests can substitute a stub."""
    cfg = KiroCrewConfig.load()
    return HookManager(hooks_config_from_config_dict(getattr(cfg, "hooks", {}) or {}))


def _path_args(ev: object) -> list[str] | None:
    """Every filesystem path in the request's arguments, at any nesting depth.

    Uses the platform's exhaustive walker (``target_paths``): ``fs_read``
    carries its targets inside ``operations[]``, so a top-level scan saw no
    path and never reached the sensitive-path check. Returns ``None`` when the
    arguments cannot be verified -- no argument dictionary at all, or a walk
    the platform had to truncate -- and the caller denies.
    """
    sources: list[dict[str, Any]] = []
    raw = getattr(ev, "raw_tool_params", None)
    if isinstance(raw, dict):
        sources.append(raw)
    tool_input = getattr(ev, "tool_input", "") or ""
    if isinstance(tool_input, str) and tool_input.strip():
        try:
            parsed = json.loads(tool_input)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            sources.append(parsed)
    if not sources:
        return None
    out: list[str] = []
    for src in sources:
        found = target_paths(src)
        if found.truncated:
            return None  # an incomplete walk cannot be fully path-checked
        out.extend(found)
        # fs_read batches ``operations[]``: every operation must expose a
        # target, or a shape the walker does not know rides in beside a clean
        # one and is read unjudged.
        ops = src.get("operations")
        if isinstance(ops, list):
            for op in ops:
                if not isinstance(op, dict) or not target_paths(op):
                    return None
    return out


def advisor_permission_gate(ev: object, *, cwd: str | None) -> str:
    """Judge one reviewer permission request. ``""`` approves once; a reason denies.

    Fail CLOSED at every step: an unknown tool, an unparseable argument set,
    a hook-layer failure, or any path resolving into a sensitive location is a
    denial. The reviewer runs unattended on an injected checkpoint -- a
    credential read it is talked into must never reach the reviewer model.
    """
    # Authorize on the trusted tool NAME only: ACP reports fs_read under the
    # generic kind "read", so the kind cannot select the ceiling -- and a
    # request with no name is not authorized by a self-declared kind either.
    # tool_kind still reaches the hook separately.
    tool = str(getattr(ev, "tool_name", "") or "").strip()
    tool_kind = str(getattr(ev, "tool_kind", "") or "").strip()
    if tool not in _REVIEWER_READ_ONLY_TOOLS:
        return f"Blocked: reviewer tool {tool or '<unknown>'!r} is outside the read-only ceiling"
    paths = _path_args(ev)
    if paths is None:
        return "Blocked: reviewer tool arguments could not be verified (an operation exposes no target)"
    if not paths:
        if tool not in _REVIEWER_RECURSIVE_TOOLS:
            # A read that exposes no target (an unknown argument shape) cannot
            # be path-checked; deny rather than approve unverified.
            return "Blocked: reviewer read has no verifiable target path"
        # A pathless recursive search defaults to the reviewer cwd: that
        # implicit root gets the same check as an explicit one, and an
        # unknown root cannot be checked at all.
        if not cwd:
            return "Blocked: reviewer search has no verifiable root"
        paths = [cwd]
    for candidate in paths:
        if is_sensitive_path(candidate, base_dir=cwd):
            return f"Blocked: access to sensitive path: {candidate}"
        # grep recurses: a search rooted at an ANCESTOR of a fenced leaf
        # (~, /home/<user>) reaches what a direct read is denied.
        if path_contains_sensitive(candidate, base_dir=cwd):
            return f"Blocked: path contains a sensitive location: {candidate}"
    try:
        manager = _hook_manager()
        result = manager.on_tool_call(
            (str(getattr(ev, "title", "") or "") or tool).strip(),
            session_key="advisor:reviewer",
            agent="kirocrew-advisor",
            app="advisor",
            tool_kind=tool_kind or tool,
            # Per-tool policy matches on the verified MCP identity (as every
            # other caller passes it) -- a benign title must not bypass it.
            mcp_server_name=str(getattr(ev, "mcp_server_name", "") or ""),
            mcp_tool_name=tool,
            mcp_identity_trusted=bool(getattr(ev, "mcp_identity_trusted", False)),
            raw_params=getattr(ev, "raw_tool_params", None),
            diff_path="",
            command=None,
            is_shell=False,
        )
        if getattr(result, "action", "") == TOOL_DENY:
            return (getattr(result, "reason", "") or "denied by governance policy").strip()
    except Exception as exc:  # noqa: BLE001 - a broken gate must DENY, not authorize
        logger.warning("advisor permission gate: hook layer unavailable; denying", exc_info=True)
        return f"governance hook unavailable: {exc}"
    return ""


def reviewer_process_cwd() -> Path:
    """The reviewer PROCESS cwd: a crew-owned directory outside the agents tree.

    kiro-cli resolves ``--agent`` against ``<cwd>/.kiro/agents`` first, so the
    cwd must be somewhere no agent can plant a same-named spec: this directory
    is mounted read-only inside every sandbox (``sandbox._CREW_READONLY_LEAVES``),
    is on the agents' file-edit write-protected list (``security.paths``), and
    the installer refuses to spawn while anything sits under its ``.kiro`` tree.
    It must also not be (or
    contain, or sit inside) the agents directory, which the delegated sandboxes
    (Windows, macOS internal) refuse as a workspace. Created off-loop by
    :func:`ensure_advisor_agent_installed`, which runs before every spawn.
    """
    return config_dir() / "advisor"


def build_reviewer_runtime(reviewer_model: str, work_dir: str | None = None):
    """The real reviewer pool: ACP runtime + envelope prompt.

    One process shared across every enabled parent (one reviewer session per
    parent), spawned with the packaged read-only reviewer agent under the auto
    sandbox. The PROCESS cwd is :func:`reviewer_process_cwd`: kiro-cli resolves
    ``--agent`` against ``<cwd>/.kiro/agents`` before the global directory, so
    the cwd is a crew-owned directory no agent may write into -- and not the
    agents tree itself, which the delegated sandboxes refuse as a workspace.
    The observed workspace is only ever the SESSION cwd. Import-light so the advisor package stays loadable without a
    live ACP stack; failures surface at first acquire, where the pool degrades
    the session visibly.
    """

    async def pre_spawn() -> None:
        await asyncio.to_thread(ensure_advisor_agent_installed)

    def runtime_factory() -> AgentRuntimeHandle:
        return oneshot.create_agent_runtime(
            agent=ADVISOR_AGENT_NAME,
            work_dir=str(reviewer_process_cwd()),
            model=reviewer_model or None,
        )

    async def prompt_fn(session: ReviewerSession, payload: dict[str, Any]) -> object:
        """Feed one observation update; return the reviewer's final text.

        Delegates the session lifecycle to the SDK's one-shot surface -- the
        dispatcher extracts and validates the envelope from the raw text.
        Read-only agent under the strict sandbox; any permission request the
        backend raises is judged by ``advisor_permission_gate`` and approved
        once. An unset work dir means the crew default workspace, as for
        ``AcpRuntime``.
        """
        runtime = payload.pop("_runtime")
        cwd = payload.get("work_dir") or work_dir or str(config_dir() / "workspace")
        try:
            reply = await oneshot.prompt_for_reply(
                runtime,
                cwd=cwd,
                prompt=payload["prompt"],
                permission_gate=lambda ev: advisor_permission_gate(ev, cwd=cwd),
                # The pool's live authorization (opt-out / reset), re-checked
                # after the reviewer session opens and before the prompt is sent.
                proceed=payload.get("_authorized"),
            )
        except oneshot.OneShotCancelled:
            logger.info("advisor review cancelled: authorization revoked while the session opened")
            return None
        except oneshot.OneShotAuditFailed:
            logger.warning("advisor review abandoned: a reviewer tool call could not be audited")
            return None
        if reply.terminal is not None:
            try:
                await usage_handlers.persist_token_record_async(
                    model=reply.model or reviewer_model,
                    event=reply.terminal,
                    **advisor_usage_kwargs(session),
                )
            except Exception:
                logger.warning("advisor usage persistence failed", exc_info=True)
        return reply.text

    return AdvisorReviewerRuntime(
        runtime_factory=runtime_factory,
        prompt_fn=prompt_fn,
        pre_spawn=pre_spawn,
    )
