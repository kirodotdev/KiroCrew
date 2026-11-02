"""The KAS workflow router's types, limits and pure helpers.

:mod:`kiro_crew.kas_workflows.router` imports every name here, which keeps it the
import path callers and tests use (``router.PendingLaunch``,
``router.RECONNECT_INTERVAL_SECS``). Nothing here redacts: the router's redaction
sites stay in ``router.py``, the module the security posture registry names as
their sink.
"""

from __future__ import annotations

import json
import re
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Protocol

from kiro_crew.kas_workflows import asks, wire

if TYPE_CHECKING:
    from kiro_crew.kas_workflows.router import KasWorkflowRouter

METHOD_REQUEST_PERMISSION = "session/request_permission"


#: KAS's structured question on a step session (``acp.kas_wire.METHOD_USER_INPUT``),
#: sent instead of the legacy bridge while the process announces ``userInput``.
METHOD_USER_INPUT = "_kiro/userInput"


#: Approvals held for a person at once. Past this a step's request is rejected:
#: an unattended run must not grow gateway memory without bound.
MAX_PENDING_APPROVALS = 64


#: Launches registered but not yet taken by their chat's turn, and how long one
#: waits. A launch that was never picked up (the chat was closed, the turn
#: failed before claiming a session) expires instead of lingering.
MAX_PENDING_LAUNCHES = 32


LAUNCH_TTL_SECS = 600.0


#: Longest title / command text kept for display.
_MAX_DISPLAY_CHARS = 2000


#: A plan lists every step with its agent, model and first task line.
_MAX_PLAN_CHARS = 16000


#: Option kinds a person may pick. Anything else on a request is not offered.
_OFFERED_KINDS = frozenset({"allow_once", "allow_always", "reject_once", "reject_always"})


#: Control actions a person may send, and the method each maps to.
CONTROL_METHODS = {
    "pause": wire.METHOD_PAUSE,
    "resume": wire.METHOD_RESUME,
    "cancel": wire.METHOD_CANCEL,
    "delete": wire.METHOD_DELETE,
}


#: How a refused control names its verb (:meth:`KasWorkflowRouter._require`).
_VERBS = {"pause": "pause", "resume": "resume", "cancel": "stop", "delete": "delete"}


#: The message that starts a launch turn. Readable, because it is the user turn
#: the chat shows; the token is what authorizes it, so typing the shape by hand
#: without a token this router issued for that chat starts nothing.
_LAUNCH_RE = re.compile(r"^Start workflow .* \(launch ([0-9a-f]{32})\)\s*$", re.DOTALL)


_RECONNECT_RE = re.compile(r"^Reconnect workflow runs \(reconnect ([0-9a-f]{32})\)\s*$")


#: The message that starts an OBSERVE turn: the chat turn that shows the model
#: turn KAS starts on its own when a run ends (:meth:`KasWorkflowRouter.prepare_observe`).
#: Same rule as a launch: only a token this router issued for that chat counts.
_OBSERVE_RE = re.compile(r"^\[Workflow `[^`\n]*` [a-z]+\] .* \(observe ([0-9a-f]{32})\)\s*$")


#: The word an observe row carries for a step's waking note, by severity. The
#: dashboard draws each as its own muted line (``KasTurnMarker``).
_NOTE_VERBS = {"info": "update", "warning": "waiting", "error": "error"}


#: Every word an observe row may carry; anything else is shown as "ended".
_OBSERVE_VERBS = wire.TERMINAL_RUN_STATUSES | frozenset(_NOTE_VERBS.values())


#: Observe turns waiting for their chat, and runs whose end already got one.
MAX_PENDING_OBSERVES = 32


MAX_OBSERVED_RUNS = 512


#: ``(slot_key, message) -> started``: starts a chat turn with *message* when the
#: chat is open and idle. The dashboard installs it (:meth:`KasWorkflowRouter.set_turn_starter`).
TurnStarter = Callable[[str, str], bool]


#: The pause reason KAS stamps on a run whose owning process died mid-run
#: (``STALE_RUNNING_PAUSE_REASON`` in kiro-agent ``workflow/run-liveness.ts``).
#: Only a run paused for exactly this reason is resumed automatically: any other
#: pause is someone's decision, and reconnecting leaves it paused.
STALE_RUNNING_PAUSE_REASON = wire.STALE_RUNNING_PAUSE_REASON


RECONNECT_REASON = "Reconnected after Kiro Agent restart"


#: Automatic reconnects: at most one per chat per interval, and at most this many
#: tries for one loss, so a run another process still holds is not chased forever.
RECONNECT_INTERVAL_SECS = 120.0


RECONNECT_MAX_TRIES = 3


#: KAS ``STALE_AFTER_MS`` (kiro-agent ``workflow/run-liveness.ts``: 4.5 x
#: ``BEAT_INTERVAL_MS`` 30s): an owner that has not touched a run's beat file
#: for this long has abandoned it. Boot recovery waits it out once (plus
#: slack), so a run the previous gateway's processes held reads stale to KAS,
#: while one a process outside this gateway still drives keeps reading live.
OWNER_ABANDON_SECS = 135.0


BOOT_RECOVERY_SLACK_SECS = 15.0


#: Boot recovery bounds: chats reattached at once, and chats per boot.
BOOT_RECOVERY_CONCURRENCY = 2


BOOT_RECOVERY_MAX_CHATS = 16


#: How long one chat's reconnect turn may take before the next chat goes.
BOOT_RECOVERY_TURN_SECS = 120.0


#: Runs remembered in the ledger. The oldest finished runs go first.
LEDGER_MAX_RUNS = 500


class KasRuntime(Protocol):
    """The slice of :class:`~kiro_crew.acp.runtime.AcpRuntime` the router drives."""

    def is_alive(self) -> bool: ...

    async def extension_request(
        self, method: str, params: dict[str, Any], *, timeout: float = ...
    ) -> dict[str, Any]: ...

    async def send_response(self, request_id: int | str, result: dict[str, Any]) -> None: ...

    async def send_notification(self, method: str, params: dict[str, Any]) -> None: ...

    async def kill(self, *, expected: bool = False, reason: str = "") -> None: ...


RuntimeFactory = Callable[[], Awaitable[KasRuntime]]


class KasWorkflowError(RuntimeError):
    """A request the router could not complete; the message is safe to show."""


class StepNotStarted(KasWorkflowError):
    """The step has no conversation to show yet (or it is not on disk)."""


@dataclass(frozen=True)
class PendingLaunch:
    """A run the dashboard asked for, waiting for its chat's turn to start it."""

    token: str
    slot_key: str
    cwd: str
    source: str
    inputs: Mapping[str, str]
    run_label: str | None
    created_at: float
    #: Launch options: the run's default model and effort for its steps
    #: (``_kiro/workflow/new`` ``modelId`` / ``effortLevel``).
    model: str | None = None
    effort: str | None = None

    def message(self, display_name: str) -> str:
        """The user turn that starts this launch in its chat."""
        name = display_name.replace("`", "'")[:200] or "workflow"
        return f"Start workflow `{name}` in `{self.cwd}` (launch {self.token})"


@dataclass(frozen=True)
class PendingApproval:
    """One step permission request waiting for a person."""

    key: str
    request_id: int | str
    session_id: str
    workflow_id: str
    node_id: str
    title: str
    command: str | None
    options: tuple[Mapping[str, str], ...]
    plan: Mapping[str, Any] | None
    created_at: float
    runtime: "weakref.ReferenceType[Any]"
    #: The unredacted command and title, for re-deciding under a changed mode
    #: (:meth:`KasWorkflowRouter.recheck_pending`). Never sent to a client.
    raw_command: str | None = None
    raw_title: str | None = None
    #: A plan handoff: never answered by a chat's approval mode, Trust included.
    is_plan: bool = False
    #: KAS's plain-text plan (redacted, capped), shown on the card.
    plan_text: str | None = None
    #: What an "always" answer grants in Crew (:func:`_step_grant_key`): the
    #: exact command, or the tool when the request carries no command.
    grant_key: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "workflowId": self.workflow_id,
            "nodeId": self.node_id,
            "title": self.title,
            "command": self.command,
            "options": [
                {
                    **dict(o),
                    "grant": (
                        None
                        if self.is_plan
                        else _option_grant(o.get("kind"), self.command is not None)
                    ),
                }
                for o in self.options
            ],
            # A plan handoff carries KAS's text and, when KAS sent it, the steps
            # to add as a tree (``_meta.kiro.workflowPlan``), already neutralized.
            "plan": asks.plan_payload(self.plan_text, self.plan) if self.is_plan else None,
            "isPlan": self.is_plan,
            "planText": self.plan_text,
            "createdAt": self.created_at,
        }


def _offered_options(params: Mapping[str, Any]) -> tuple[Mapping[str, str], ...]:
    out: list[Mapping[str, str]] = []
    for raw in wire.as_list(params.get("options")):
        item = wire.as_mapping(raw)
        if item is None:
            continue
        option_id = wire.as_str(item.get("optionId"))
        kind = wire.as_str(item.get("kind"))
        if option_id is None or kind not in _OFFERED_KINDS:
            continue
        name = wire.as_str(item.get("name")) or option_id
        out.append({"optionId": option_id, "kind": kind, "name": name[:200]})
    return tuple(out)


def _plan_meta(params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    meta = wire.as_mapping(params.get("_meta"))
    kiro = wire.as_mapping(meta.get("kiro")) if meta else None
    return wire.as_mapping(kiro.get("workflowPlan")) if kiro else None


def _is_plan_approval(params: Mapping[str, Any]) -> bool:
    """Whether a step's request is a workflow plan handoff (``update_workflow``).

    KAS stamps such a request ``_meta.kiro.toolId: "update_workflow"`` and, when
    the plan fits, carries it as ``_meta.kiro.workflowPlan``. Either marks it.
    """
    meta = wire.as_mapping(params.get("_meta"))
    kiro = wire.as_mapping(meta.get("kiro")) if meta else None
    if kiro is None:
        return False
    return wire.as_str(kiro.get("toolId")) == wire.TOOL_UPDATE_WORKFLOW or (
        wire.as_mapping(kiro.get("workflowPlan")) is not None
    )


def _tool_call_text(tool_call: Mapping[str, Any]) -> str | None:
    """The text blocks of a request's ``toolCall.content``, joined; None when empty.

    For a plan handoff this is KAS's plain-text plan, the one copy that is
    always sent (``workflowPlan`` is dropped when the plan is too large).
    """
    parts: list[str] = []
    for raw in wire.as_list(tool_call.get("content")):
        item = wire.as_mapping(raw) or {}
        inner = wire.as_mapping(item.get("content")) or item
        if wire.as_str(inner.get("type")) == "text":
            text = wire.as_str(inner.get("text"))
            if text:
                parts.append(text)
    return "\n\n".join(parts) or None


def _reject_result(params: Mapping[str, Any]) -> dict[str, Any]:
    from kiro_crew.agent_sdk.kas_workflows_driver import reject_option_id

    option_id = reject_option_id(dict(params))
    if option_id is None:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _alive(ref: "weakref.ReferenceType[Any] | None") -> Any:
    runtime = ref() if ref is not None else None
    return runtime if runtime is not None and runtime.is_alive() else None


#: A chat's approval modes as a step sees them. ``trust`` covers the chat's own
#: Trust, a scoped grant and the process-wide override alike.
MODE_NORMAL = "normal"


MODE_TRUST = "trust"


MODE_TRUST_READS = "trust_reads"


#: ``() -> (mode, reason)``: the owning chat's mode NOW, and the SEL provenance
#: an auto-approval under it records (``trust``, ``yolo``, ``scoped``, ...).
ModeReader = Callable[[], tuple[str, str]]


def _allow_once(options: tuple[Mapping[str, str], ...]) -> str | None:
    for option in options:
        if option.get("kind") == "allow_once":
            return option.get("optionId")
    return None


#: Step grants ("Always allow" in one step) kept per step session, and sessions kept.
MAX_GRANTS_PER_STEP = 64


MAX_GRANT_SESSIONS = 512


def _option_grant(kind: str | None, has_command: bool) -> dict[str, Any]:
    """What picking an option of *kind* grants, as the card states it (gap 21).

    Crew's own Trust model, not KAS's consent scopes: an "always" allow is a
    Crew grant for THIS step instance's session (exact command, else the tool),
    never a KAS-persisted rule. A reject-always is KAS's own refusal for the
    rest of that step session.
    """
    if kind == "allow_always":
        return {"scope": "step", "match": "command" if has_command else "tool"}
    if kind == "reject_always":
        return {"scope": "step", "match": None}
    return {"scope": "once", "match": None}


def _step_grant_key(params: Mapping[str, Any], command: str | None, title: str) -> str:
    """The key an "always" grant matches on: the exact command, else the tool.

    The tool is KAS's ``_meta.kiro.toolId`` when it sent one, else the call's
    title, so a grant for one tool never covers another.
    """
    if command:
        return f"cmd:{command}"
    meta = wire.as_mapping(params.get("_meta"))
    kiro = wire.as_mapping(meta.get("kiro")) if meta else None
    tool = wire.as_str(kiro.get("toolId")) if kiro else None
    return f"tool:{tool}" if tool else f"title:{title}"


@dataclass(frozen=True)
class DashboardHooks:
    """What the router may do in the dashboard besides starting turns.

    - ``notify(key, slot_key, title, body, meta)``: publish one bell note for a
      held step ask (gap 6); ``retire(key)`` marks it read once the ask is gone.
    - ``append_marker(slot_key, content, meta) -> bool``: append one notice row
      to a chat's transcript (gap 10), True when written.

    Every hook is best effort: a missing one or one that raises costs the
    signal, never the run.
    """

    notify: Callable[[str, str, str, str, Mapping[str, Any]], None] | None = None
    retire: Callable[[str], None] | None = None
    append_marker: Callable[[str, str, Mapping[str, Any]], bool] | None = None


async def _name_grant_refusal(command: str) -> Any:
    from kiro_crew.name_grant import refusal_for_command_off_loop

    return await refusal_for_command_off_loop(command)


def _read_only(command: str) -> bool:
    from kiro_crew.security.readonly_bash import is_read_only_bash

    return is_read_only_bash(command)


def _audit_auto_approval(**fields: Any) -> None:
    from kiro_crew.sel import sel

    sel().log_tool_invocation(**fields)


class _RuntimeSink:
    """One runtime's view of the router: the reader loop's frames, tagged with it."""

    def __init__(self, router: "KasWorkflowRouter", runtime: Any) -> None:
        self._router = router
        self._runtime = weakref.ref(runtime)

    def claims_notification(self, method: str) -> bool:
        return method in wire.NOTIFICATION_METHODS

    def on_notification(self, method: str, params: dict[str, Any]) -> None:
        self._router._on_notification(self._runtime, method, params)

    def watches_notification(self, method: str) -> bool:
        return method == wire.NOTIFY_SESSION_NOTIFY

    def on_watched_notification(self, method: str, params: dict[str, Any]) -> None:
        self._router._on_watched_notification(method, params)

    def claims_step_request(self, session_id: str, method: str) -> bool:
        return self._router._claims_step(self._runtime, session_id, method)

    def on_step_request(
        self, request_id: int | str, session_id: str, method: str, params: dict[str, Any]
    ) -> None:
        self._router._on_step_request(self._runtime, request_id, session_id, params, method)


def _gate_node_actions(nodes: list[Any], methods: frozenset[str]) -> None:
    """Drop node actions the serving Kiro Agent did not advertise, in place."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node["actions"] = wire.gate_actions(list(node.get("actions") or []), methods, node=True)
        _gate_node_actions(node.get("children") or [], methods)
        for iteration in node.get("iterations") or []:
            if isinstance(iteration, dict):
                _gate_node_actions(iteration.get("children") or [], methods)


def _node_label(nodes: list[Any], node_id: str) -> str | None:
    """The display label of *node_id* in a row's ``nodes`` tree, if present."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("id") == node_id:
            return str(node.get("label") or node_id)
        found = _node_label(node.get("children") or [], node_id)
        if found:
            return found
    return None


def _parse_iso(value: Any) -> float | None:
    from datetime import datetime

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _duration_secs(started: Any, ended: Any) -> int | None:
    """Whole seconds between two ISO stamps, or None when either is unknown."""
    start, end = _parse_iso(started), _parse_iso(ended)
    if start is None or end is None or end < start:
        return None
    return int(end - start)


def _now_iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


#: Largest recipe definition read for the detail view, and the per-prompt cap.
MAX_DEFINITION_BYTES = 512 * 1024


MAX_PROMPT_CHARS = 4000


def _read_definition(path: str) -> Mapping[str, Any] | None:
    """A ``.workflow.json`` file's object, or None when unreadable, too large or not JSON."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_DEFINITION_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_DEFINITION_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#: Steps listed from one recipe definition at most.
MAX_PATHS_PER_DEFINITION = 500


def _strip_node_actions(nodes: list[Any]) -> None:
    """Empty every node's ``actions`` (and its iterations'), in place."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node["actions"] = []
        _strip_node_actions(node.get("children") or [])
        for iteration in node.get("iterations") or []:
            if isinstance(iteration, dict):
                _strip_node_actions(iteration.get("children") or [])


def _kept_plan_waits(state: Mapping[str, Any]) -> list[str]:
    """Step sessions of a loaded run that hold an open request across a restart.

    On load KAS drops every request wait a dead process left behind except the
    plan approval it keeps (``pending-handoff.json``) for a top-level step, so a
    ``permission`` wait with a ``toolCallId`` on a just-loaded run is that plan.
    """
    out: list[str] = []
    for wait in wire.parse_attention(state.get("attention")):
        if wait.kind == wire.ATTENTION_PERMISSION and wait.tool_call_id and wait.session_id:
            if wait.session_id not in out:
                out.append(wait.session_id)
    return out


def _project(cwd: str) -> str:
    """*cwd* resolved and confined to the allowed project roots, before any filesystem use."""
    from kiro_crew.kas_workflows.project_root import allowed_project_roots, confine_project_dir

    path = confine_project_dir(cwd, allowed_project_roots())
    if path is None:
        raise KasWorkflowError(
            "That project directory does not exist or is outside your home directory, "
            "the gateway's project directory and your recent projects."
        )
    return path


def session_binding(provider: Any) -> tuple[Any, str] | None:
    """The (runtime, ACP session id) a claimed chat provider runs on, if KAS.

    ``provider`` is what the chat turn claimed: an ``AcpProvider`` whose
    ``client`` is the :class:`~kiro_crew.acp.session_provider.AcpSessionProvider`
    for its shared process. Anything else -- another harness, a provider not
    started yet -- answers None.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_NATIVE_WORKFLOWS

    inner = getattr(provider, "client", None)
    runtime = getattr(inner, "runtime", None)
    session_id = getattr(inner, "session_id", None)
    if runtime is None or not isinstance(session_id, str) or not session_id:
        return None
    if getattr(runtime, "acp_backend", "") not in ACP_BACKENDS_NATIVE_WORKFLOWS:
        return None
    return runtime, session_id
