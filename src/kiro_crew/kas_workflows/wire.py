"""The KAS workflow wire surface: names, tolerant parsers and run-row controls.

This module is the ONE place Crew spells a ``_kiro/workflow/*`` literal or reads
one of its payloads, so a wire change in KAS is a single-file edit here.

The KAS surface is pre-release and changes often, so every parser is tolerant
in the same, fail-closed way:

- A payload missing a REQUIRED field (an id, a name, a known status) parses to
  ``None`` and the caller drops it. A row Crew cannot identify is never shown.
- An unknown or malformed OPTIONAL field is dropped and the rest is kept.
- Unknown extra fields are ignored, so an additive KAS change cannot break Crew.

The control rules (:func:`run_controls`) follow kiro-desktop's run rows, so a
run reads the same way in both clients: one primary action (open to resolve,
then pause, then resume), Stop in its own slot and always confirmed, "Pausing"
while a requested pause is still pending, and nothing on a terminal run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# ── Requests (client -> agent) ──
METHOD_LIST_RECIPES = "_kiro/workflow/listRecipes"
METHOD_LIST_WATCH_HANDLERS = "_kiro/workflow/listWatchHandlers"
METHOD_NEW = "_kiro/workflow/new"
METHOD_INVOKE = "_kiro/workflow/invoke"
METHOD_LOAD = "_kiro/workflow/load"
METHOD_INSPECT = "_kiro/workflow/inspect"
METHOD_LIST = "_kiro/workflow/list"
METHOD_PAUSE = "_kiro/workflow/pause"
METHOD_RESUME = "_kiro/workflow/resume"
METHOD_CANCEL = "_kiro/workflow/cancel"
METHOD_DELETE = "_kiro/workflow/delete"
METHOD_RETRY = "_kiro/workflow/retry"
METHOD_UPDATE = "_kiro/workflow/update"
METHOD_RESUME_ALL = "_kiro/workflow/resumeAll"

REQUEST_METHODS = frozenset(
    {
        METHOD_LIST_RECIPES,
        METHOD_LIST_WATCH_HANDLERS,
        METHOD_NEW,
        METHOD_INVOKE,
        METHOD_LOAD,
        METHOD_INSPECT,
        METHOD_LIST,
        METHOD_PAUSE,
        METHOD_RESUME,
        METHOD_CANCEL,
        METHOD_DELETE,
        METHOD_RETRY,
        METHOD_UPDATE,
        METHOD_RESUME_ALL,
    }
)

# ── Notifications (agent -> client) ──
NOTIFY_RUN_START = "_kiro/workflow/run_start"
NOTIFY_NODE_START = "_kiro/workflow/node_start"
NOTIFY_NODE_COMPLETE = "_kiro/workflow/node_complete"
NOTIFY_NODE_PAUSED = "_kiro/workflow/node_paused"
NOTIFY_LOOP_ITERATION = "_kiro/workflow/loop_iteration"
NOTIFY_WATCH_POLL = "_kiro/workflow/watch_poll"
NOTIFY_PAUSED = "_kiro/workflow/paused"
NOTIFY_RUN_COMPLETE = "_kiro/workflow/run_complete"
NOTIFY_STEPS_QUEUED = "_kiro/workflow/steps_queued"
NOTIFY_RECIPES_CHANGED = "_kiro/workflow/recipes_changed"

#: Notifications that change one run's row. ``recipes_changed`` is not one: it
#: replaces the recipe catalog and names no run.
RUN_NOTIFICATIONS = frozenset(
    {
        NOTIFY_RUN_START,
        NOTIFY_NODE_START,
        NOTIFY_NODE_COMPLETE,
        NOTIFY_NODE_PAUSED,
        NOTIFY_LOOP_ITERATION,
        NOTIFY_WATCH_POLL,
        NOTIFY_PAUSED,
        NOTIFY_RUN_COMPLETE,
        NOTIFY_STEPS_QUEUED,
    }
)
NOTIFICATION_METHODS = RUN_NOTIFICATIONS | {NOTIFY_RECIPES_CHANGED}

#: A ``send_message`` delivered to a session (a step's note to the run's parent,
#: or the runtime's note on a step's behalf). It names its target ``sessionId``
#: and is routed to it as usual; the router only watches it, because a note that
#: wakes an IDLE parent starts a model turn no client asked for.
NOTIFY_SESSION_NOTIFY = "_kiro/session/notify"
#: Severities of a step's note that wake an idle parent (KAS ``admitWake``):
#: ``warning`` and ``error`` always, an explicit ``info`` to the step's own
#: parent too. ``success`` never wakes; it only completes the step.
WAKING_NOTE_SEVERITIES = frozenset({"info", "warning", "error"})

#: ``_meta.kiro.toolId`` of a step's plan handoff permission request.
TOOL_UPDATE_WORKFLOW = "update_workflow"

# ── Statuses ──
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ABORTED = "aborted"

RUN_STATUSES = frozenset(
    {STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_FAILED, STATUS_ABORTED}
)
#: ``run_complete`` is also sent with ``paused`` when a step parks for input, so
#: terminality is read from this set and never from the notification's name.
TERMINAL_RUN_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_ABORTED})

NODE_STATUS_PENDING = "pending"
NODE_STATUS_SKIPPED = "skipped"
NODE_STATUSES = frozenset(
    {
        NODE_STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_PAUSED,
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_ABORTED,
        NODE_STATUS_SKIPPED,
    }
)
#: A step in one of these does not count toward "steps left".
NODE_DONE_STATUSES = frozenset({STATUS_COMPLETED, NODE_STATUS_SKIPPED})

NODE_TYPE_STEP = "step"
NODE_TYPES = frozenset({NODE_TYPE_STEP, "sequence", "repeat", "parallel", "watch"})

ATTENTION_PERMISSION = "permission"
ATTENTION_QUESTION = "question"
ATTENTION_KINDS = frozenset({ATTENTION_PERMISSION, ATTENTION_QUESTION})

#: ACP's ordinary prompt and load: a reply to a parked step is a plain
#: ``session/prompt`` on the step's own session (KAS reroutes it into the run).
METHOD_SESSION_PROMPT = "session/prompt"
METHOD_SESSION_LOAD = "session/load"
#: ACP's turn cancel, a NOTIFICATION. On a step's session KAS stops that one
#: step: it parks ``paused`` with ``stopInitiator: 'user'`` while parallel
#: siblings keep running (kiro-agent ``beginUserStopScope``).
METHOD_SESSION_CANCEL = "session/cancel"

#: KAS's ceiling on a repeat's effective iteration budget (``MAX_REPEAT_ITERATIONS``).
MAX_REPEAT_ITERATIONS = 1000

#: ``WorkflowState.pause.kind`` of a repeat that used its whole budget.
PAUSE_KIND_MAX_ITERATIONS = "maxIterations"

#: ``WorkflowState.pauseDetail.class`` values.
PAUSE_CLASS_TRANSIENT = "transient-error"
PAUSE_CLASS_CONTINUATION = "continuation-exhausted"
PAUSE_CLASS_PARENT_AUTHORITY = "parent-authority"

#: Why a paused run is paused, as every surface reads it (``run.pause.reason``).
PAUSE_LOOP_EXHAUSTED = "loop_exhausted"
PAUSE_AWAITING_INPUT = "awaiting_input"
PAUSE_PLAN_APPROVAL = "plan_approval"
PAUSE_TRANSIENT_ERROR = "transient_error"
PAUSE_RETRIES_EXHAUSTED = "retries_exhausted"
PAUSE_USER = "user"
PAUSE_UNKNOWN = "unknown"

#: Run actions (``run.actions``) and the step actions (``node.actions``).
RUN_ACTION_PAUSE = "pause"
RUN_ACTION_RESUME = "resume"
RUN_ACTION_CANCEL = "cancel"
RUN_ACTION_DELETE = "delete"
RUN_ACTION_RETRY = "retry"
RUN_ACTION_RESTART = "restart"
NODE_ACTION_GRANT = "grant"
NODE_ACTION_COMPLETE = "complete"
NODE_ACTION_FAIL = "fail"
NODE_ACTION_CANCEL = "cancel"
NODE_ACTION_REPLY = "reply"

#: ``_kiro/workflow/update`` ``update_status`` values a person may end a step with.
STEP_END_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})
#: The longest reply a person may send a step.
MAX_STEP_REPLY_CHARS = 20000

#: Recipe sources compiled into KAS. Read-only in every editor.
BUNDLED_SOURCE_PREFIX = "bundled://"

#: Longest ``reason`` KAS accepts on a control request.
MAX_CONTROL_REASON_CHARS = 500

#: Initiator Crew stamps on every control it sends on a person's behalf.
INITIATOR_USER = "user"

#: ``node_paused.kind`` of KAS's informational transient-retry backoff: the
#: node's status stays ``running`` and KAS is still driving it. Any other value
#: (``paused``, or none) is a genuine park.
NODE_PAUSED_RETRY_WAIT = "retry-wait"

#: ``watch_poll.outcome`` values (KAS ``WatchOutcomeSchema``).
WATCH_OUTCOMES = frozenset({"idle", "new-activity", "terminal-state"})

#: ``steps_queued.resolution.outcome`` values.
PLAN_UPDATE_APPLIED = "applied"
PLAN_UPDATE_OUTCOMES = frozenset({PLAN_UPDATE_APPLIED, "rejected", "dropped"})

#: The KAS method each run / node action is sent as. An action whose method the
#: driving process did not advertise is not offered (:func:`gate_actions`).
#: ``None`` = ACP core (``session/cancel``, ``session/prompt``): always served.
RUN_ACTION_METHODS: dict[str, str | None] = {
    RUN_ACTION_PAUSE: METHOD_PAUSE,
    RUN_ACTION_RESUME: METHOD_RESUME,
    RUN_ACTION_CANCEL: METHOD_CANCEL,
    RUN_ACTION_DELETE: METHOD_DELETE,
    RUN_ACTION_RETRY: METHOD_RETRY,
    RUN_ACTION_RESTART: METHOD_INVOKE,
}
NODE_ACTION_METHODS: dict[str, str | None] = {
    NODE_ACTION_GRANT: METHOD_RESUME,
    NODE_ACTION_COMPLETE: METHOD_UPDATE,
    NODE_ACTION_FAIL: METHOD_UPDATE,
    NODE_ACTION_CANCEL: None,
    NODE_ACTION_REPLY: None,
}


def extension_methods(agent_capabilities: Any) -> frozenset[str] | None:
    """``agentCapabilities._meta.kiro.extensionMethods``, or None for a legacy agent.

    None (no list, or a malformed one) means the agent predates the list, so
    every verb is assumed served, as the IDE does (``supportsWorkflowMethod``).
    """
    caps = as_mapping(agent_capabilities)
    meta = as_mapping(caps.get("_meta")) if caps else None
    kiro = as_mapping(meta.get("kiro")) if meta else None
    methods = kiro.get("extensionMethods") if kiro else None
    if not isinstance(methods, list) or not all(isinstance(m, str) for m in methods):
        return None
    return frozenset(methods)


def method_served(method: str | None, methods: frozenset[str] | None) -> bool:
    """Whether *method* may be sent to an agent that advertised *methods*."""
    return method is None or methods is None or method in methods


def gate_actions(
    actions: list[str], methods: frozenset[str] | None, *, node: bool = False
) -> list[str]:
    """*actions* without those whose KAS method is not in *methods*."""
    table = NODE_ACTION_METHODS if node else RUN_ACTION_METHODS
    return [a for a in actions if method_served(table.get(a), methods)]


# ── Small readers ──


def as_str(value: Any) -> str | None:
    """A non-empty string, or None."""
    return value if isinstance(value, str) and value else None


def as_count(value: Any) -> int | None:
    """A non-negative int (never a bool), or None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _str_list(value: Any) -> tuple[str, ...]:
    return tuple(v for v in as_list(value) if isinstance(v, str) and v)


#: The separator of a KAS node path (``nodePath`` and the run state keys built
#: from it). A protocol token, not a filesystem separator: it is "/" on every OS.
NODE_PATH_SEP = "/"


def node_path(value: Any) -> tuple[str, ...]:
    """KAS sends ``nodePath`` either as a list of ids or as one slash-joined string."""
    if isinstance(value, str) and value:
        return tuple(part for part in value.split(NODE_PATH_SEP) if part)
    return _str_list(value)


_node_path = node_path

#: The runtime path segment of a repeat's iteration ``n`` is ``iter-<n>``
#: (0-based), not the wrapper's own ``<repeatId>#<n>`` id.
ITERATION_SEGMENT_PREFIX = "iter-"


# ── Records ──


@dataclass(frozen=True)
class Attention:
    """One wait on a run: a permission or a question raised on a step session."""

    kind: str
    node_id: str
    session_id: str | None = None
    node_path: tuple[str, ...] = ()
    since: str | None = None
    tool_call_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "nodeId": self.node_id,
            "sessionId": self.session_id,
            "nodePath": list(self.node_path),
            "since": self.since,
            "toolCallId": self.tool_call_id,
        }


def parse_attention(value: Any) -> tuple[Attention, ...]:
    """The well-formed entries of an ``attention`` list; malformed ones are dropped."""
    out: list[Attention] = []
    for raw in as_list(value):
        item = as_mapping(raw)
        if item is None:
            continue
        kind = as_str(item.get("kind"))
        node_id = as_str(item.get("nodeId"))
        if kind not in ATTENTION_KINDS or node_id is None:
            continue
        out.append(
            Attention(
                kind=kind,
                node_id=node_id,
                session_id=as_str(item.get("sessionId")),
                node_path=_node_path(item.get("nodePath")),
                since=as_str(item.get("since")),
                tool_call_id=as_str(item.get("toolCallId")),
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class NodeDescriptor:
    """One node of a workflow's plan, as KAS describes it before or during a run."""

    id: str
    type: str
    agent: str | None = None
    label: str | None = None
    max_iterations: int | None = None
    children: tuple["NodeDescriptor", ...] = ()
    #: Step nodes: the model and effort KAS resolved (step > workflow > parent).
    model: str | None = None
    effort: str | None = None
    #: A sequence that replaced an ``include`` node: the included recipe's
    #: ``(name, path)`` (KAS ``SequenceNode.includedFrom``). Display only.
    included_from: tuple[str, str | None] | None = None
    #: Repeat nodes: what happens at ``maxIterations`` (``abort|continue|pause``).
    on_max_iterations: str | None = None
    #: Repeat nodes: the sugar-form stop condition (``stopWhen``), display only.
    stop_when: str | None = None

    def include_payload(self) -> dict[str, Any] | None:
        if self.included_from is None:
            return None
        name, path = self.included_from
        return {"recipe": name, "path": path}

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "agent": self.agent,
            "label": self.label,
            "maxIterations": self.max_iterations,
            "model": self.model,
            "effort": self.effort,
            "include": self.include_payload(),
            "onMaxIterations": self.on_max_iterations,
            "stopWhen": self.stop_when,
            "children": [child.to_payload() for child in self.children],
        }


#: Keys a descriptor's children may arrive under. KAS's own definition uses
#: ``steps`` for sequence and repeat and ``branches`` for parallel; descriptors
#: use ``children``.
_CHILD_KEYS = ("children", "steps", "branches")

#: Deepest plan Crew walks. KAS bounds its own definitions far below this; the
#: cap only stops a hostile or corrupt payload from recursing without end.
MAX_PLAN_DEPTH = 32


def parse_node_descriptor(value: Any, *, _depth: int = 0) -> NodeDescriptor | None:
    """One plan node, or None when it carries no id or an unknown type."""
    item = as_mapping(value)
    if item is None or _depth > MAX_PLAN_DEPTH:
        return None
    node_type = as_str(item.get("type"))
    # A recipe's plan names a node ``id``; KAS's run-time descriptor
    # (``WorkflowNodeDescriptor``: inspect/load ``nodePlan``, run_start
    # ``nodeTree``) names it ``nodeId``.
    node_id = as_str(item.get("id")) or as_str(item.get("nodeId"))
    if node_type not in NODE_TYPES or node_id is None:
        return None
    children: list[NodeDescriptor] = []
    for key in _CHILD_KEYS:
        if key in item:
            children = [
                child
                for child in (
                    parse_node_descriptor(raw, _depth=_depth + 1) for raw in as_list(item.get(key))
                )
                if child is not None
            ]
            break
    return NodeDescriptor(
        id=node_id,
        type=node_type,
        agent=as_str(item.get("agent")) or as_str(item.get("agentName")),
        label=as_str(item.get("label")) or as_str(item.get("name")),
        max_iterations=as_count(item.get("maxIterations")),
        model=_model_label(item.get("modelId")),
        effort=as_str(item.get("effortLevel")),
        children=tuple(children),
        included_from=parse_included_from(item.get("includedFrom")),
        on_max_iterations=as_str(item.get("onMaxIterations")),
        stop_when=as_str(item.get("stopWhen")),
    )


def parse_included_from(value: Any) -> tuple[str, str | None] | None:
    """KAS's ``includedFrom`` ``{name, path}``, or None when it names no recipe."""
    item = as_mapping(value)
    name = as_str(item.get("name")) if item else None
    if item is None or name is None:
        return None
    return name, as_str(item.get("path"))


def _model_label(value: Any) -> str | None:
    """A model id worth showing: ``auto`` / unset means "inherit", shown as nothing."""
    model = as_str(value)
    return model if model and model != "auto" else None


def parse_plan(value: Any) -> tuple[NodeDescriptor, ...]:
    """A whole plan (a list of nodes), keeping every well-formed node."""
    return tuple(
        node for node in (parse_node_descriptor(raw) for raw in as_list(value)) if node is not None
    )


def iter_steps(plan: tuple[NodeDescriptor, ...]):
    """Every ``step`` leaf in *plan*, depth-first in plan order."""
    for node in plan:
        if node.type == NODE_TYPE_STEP:
            yield node
        yield from iter_steps(node.children)


def first_repeat_max(plan: tuple[NodeDescriptor, ...]) -> tuple[str, int] | None:
    """The first ``repeat`` node's (id, maxIterations), for a recipe's meta line."""
    for node in plan:
        if node.type == "repeat" and node.max_iterations is not None:
            return node.id, node.max_iterations
        found = first_repeat_max(node.children)
        if found is not None:
            return found
    return None


@dataclass(frozen=True)
class Recipe:
    """A saved workflow definition, as ``listRecipes`` reports it.

    ``source`` is KAS's opaque launch token (an absolute file path or a
    ``bundled://`` URI) and is passed back verbatim as ``workflowPath``. A
    built-in KAS listed without one is addressed as ``bundled://<name>``; any
    other recipe listed without one is kept (so the picker can name it) but is
    not launchable.
    """

    name: str
    source: str | None
    description: str | None = None
    built_in: bool = False
    tier: str | None = None
    inputs: tuple[tuple[str, str], ...] = ()
    plan: tuple[NodeDescriptor, ...] = ()
    validation_error: str | None = None

    @property
    def read_only(self) -> bool:
        return self.built_in or (self.source or "").startswith(BUNDLED_SOURCE_PREFIX)

    @property
    def launchable(self) -> bool:
        return self.source is not None

    def scope(self, workspace_root: str | None = None) -> str:
        """``bundled``, ``workspace`` (its file is under *workspace_root*) or ``global``."""
        if self.built_in or (self.source or "").startswith(BUNDLED_SOURCE_PREFIX):
            return RECIPE_SCOPE_BUNDLED
        if self.source and workspace_root and _under(self.source, workspace_root):
            return RECIPE_SCOPE_WORKSPACE
        return RECIPE_SCOPE_GLOBAL

    def to_payload(self, workspace_root: str | None = None) -> dict[str, Any]:
        loop = first_repeat_max(self.plan)
        scope = self.scope(workspace_root)
        return {
            "name": self.name,
            "source": self.source,
            "description": self.description,
            "builtIn": self.built_in,
            "readOnly": self.read_only,
            "launchable": self.launchable,
            "scope": scope,
            "group": RECIPE_SCOPE_WORKSPACE if scope == RECIPE_SCOPE_WORKSPACE else "global",
            "tier": self.tier,
            "inputs": [{"name": name, "type": kind} for name, kind in self.inputs],
            "plan": [node.to_payload() for node in self.plan],
            "stepCount": sum(1 for _ in iter_steps(self.plan)),
            "loop": {"nodeId": loop[0], "maxIterations": loop[1]} if loop else None,
            "validationError": self.validation_error,
        }


RECIPE_SCOPE_BUNDLED = "bundled"
RECIPE_SCOPE_WORKSPACE = "workspace"
RECIPE_SCOPE_GLOBAL = "global"


def _under(path: str, root: str) -> bool:
    """Whether absolute *path* lies under directory *root* (string test, no I/O)."""
    import posixpath

    norm_root = posixpath.normpath(root.replace("\\", "/")).rstrip("/")
    norm_path = posixpath.normpath(path.replace("\\", "/"))
    return bool(norm_root) and norm_path.startswith(norm_root + "/")


def _recipe_tier(item: Mapping[str, Any]) -> str | None:
    """The settings tier a recipe came from: ``_meta.kiro.resource.source``."""
    meta = as_mapping(item.get("_meta"))
    kiro = as_mapping(meta.get("kiro")) if meta else None
    resource = as_mapping(kiro.get("resource")) if kiro else None
    return as_str(resource.get("source")) if resource else None


def _validation_error(value: Any) -> str | None:
    """KAS sends the error as text or as an object carrying a message."""
    text = as_str(value)
    if text is not None:
        return text
    item = as_mapping(value)
    if item is None:
        return None
    return as_str(item.get("message")) or as_str(item.get("detail")) or "invalid"


def parse_recipe(value: Any) -> Recipe | None:
    """One recipe, or None when it has no name.

    A built-in listed without a ``source`` gets ``bundled://<name>`` (KAS
    launches every built-in by that URI); any other recipe without one is kept
    with no source, so it can be named but not launched.
    """
    item = as_mapping(value)
    if item is None:
        return None
    name = as_str(item.get("name"))
    if name is None:
        return None
    built_in = item.get("builtIn") is True
    source = as_str(item.get("source")) or (f"{BUNDLED_SOURCE_PREFIX}{name}" if built_in else None)
    raw_inputs = as_mapping(item.get("inputs")) or {}
    inputs = tuple(
        (key, kind)
        for key, kind in ((k, as_str(v) or "string") for k, v in raw_inputs.items())
        if isinstance(key, str) and key
    )
    return Recipe(
        name=name,
        source=source,
        description=as_str(item.get("description")),
        built_in=built_in,
        tier=_recipe_tier(item),
        inputs=inputs,
        plan=parse_plan(item.get("plan")),
        validation_error=_validation_error(item.get("validationError")),
    )


def parse_recipes(response: Any) -> tuple[Recipe, ...]:
    """The recipes in a ``listRecipes`` answer or a ``recipes_changed`` payload."""
    body = as_mapping(response) or {}
    return tuple(
        recipe for recipe in (parse_recipe(raw) for raw in as_list(body.get("recipes"))) if recipe
    )


#: KAS's ``WORKFLOW_RUN_PROGRESS_KEYS`` (covenant ``workflow/types.ts``).
RUN_PROGRESS_KEYS = ("stepsCompleted", "stepsTotal", "currentNodeId", "waitingOnWatch")


@dataclass(frozen=True)
class RunProgress:
    """KAS's own "N of M steps" for a running or paused run (``WorkflowRunProgress``).

    KAS computes it at send time over the run's step and watch nodes and puts
    it on a live run's ``list`` row and on every lifecycle notification. It is
    a snapshot: when any of the four keys is present the others describe the
    same moment, so an absent ``currentNodeId`` there means "no node
    qualifies", not "unknown". An input carrying none of the keys (an older
    KAS, a finished run, a replayed event, a cached cloud row) says nothing.
    """

    steps_completed: int | None = None
    steps_total: int | None = None
    current_node_id: str | None = None
    waiting_on_watch: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "stepsCompleted": self.steps_completed,
            "stepsTotal": self.steps_total,
            "currentNodeId": self.current_node_id,
            "waitingOnWatch": self.waiting_on_watch,
        }


#: The run payload's progress keys when KAS sent none: every one null.
UNKNOWN_RUN_PROGRESS: dict[str, Any] = {key: None for key in RUN_PROGRESS_KEYS}


def parse_run_progress(value: Any) -> RunProgress | None:
    """The progress snapshot a list row or notification carries, or None when it carries none."""
    item = as_mapping(value)
    if item is None or not any(key in item for key in RUN_PROGRESS_KEYS):
        return None
    return RunProgress(
        steps_completed=as_count(item.get("stepsCompleted")),
        steps_total=as_count(item.get("stepsTotal")),
        current_node_id=as_str(item.get("currentNodeId")),
        waiting_on_watch=item.get("waitingOnWatch") is True,
    )


@dataclass(frozen=True)
class RunSummary:
    """One row of a ``_kiro/workflow/list`` answer."""

    workflow_id: str
    status: str
    name: str
    workflow_name: str | None = None
    parent_session_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    pause_pending: bool = False
    pause_kind: str | None = None
    pause_node_id: str | None = None
    attention: tuple[Attention, ...] = field(default_factory=tuple)
    progress: RunProgress | None = None


def parse_run_summary(value: Any) -> RunSummary | None:
    """One listed run, or None when it has no id or an unknown status."""
    item = as_mapping(value)
    if item is None:
        return None
    workflow_id = as_str(item.get("workflowId"))
    status = as_str(item.get("status"))
    if workflow_id is None or status not in RUN_STATUSES:
        return None
    workflow_name = as_str(item.get("workflowName"))
    return RunSummary(
        workflow_id=workflow_id,
        status=status,
        name=as_str(item.get("name")) or workflow_name or workflow_id,
        workflow_name=workflow_name,
        parent_session_id=as_str(item.get("parentSessionId")),
        created_at=as_str(item.get("createdAt")),
        updated_at=as_str(item.get("updatedAt")),
        started_at=as_str(item.get("startedAt")),
        ended_at=as_str(item.get("endedAt")),
        pause_pending=as_mapping(item.get("pausePending")) is not None
        or item.get("pausePending") is True,
        pause_kind=as_str(item.get("pauseKind")),
        pause_node_id=as_str(item.get("pauseNodeId")),
        attention=parse_attention(item.get("attention")),
        progress=parse_run_progress(item),
    )


def parse_run_list(response: Any) -> tuple[RunSummary, ...]:
    body = as_mapping(response) or {}
    return tuple(
        run for run in (parse_run_summary(raw) for raw in as_list(body.get("runs"))) if run
    )


# ── Request builders ──


def control_reason(reason: str | None) -> str | None:
    """*reason* trimmed to what KAS accepts, or None when empty."""
    if not reason:
        return None
    text = reason.strip()
    return text[:MAX_CONTROL_REASON_CHARS] or None


def new_run_params(
    *,
    source: str,
    parent_session_id: str,
    inputs: Mapping[str, str],
    run_label: str | None = None,
    model_id: str | None = None,
    effort_level: str | None = None,
) -> dict[str, Any]:
    """Params for ``_kiro/workflow/new``. KAS requires ``parentSessionId``."""
    if not source:
        raise ValueError("a workflow source is required")
    if not parent_session_id:
        raise ValueError("KAS requires a parent session for every run")
    params: dict[str, Any] = {
        "workflowPath": source,
        "parentSessionId": parent_session_id,
        "inputs": {str(k): str(v) for k, v in inputs.items()},
    }
    if run_label:
        params["runLabel"] = run_label
    if model_id:
        params["modelId"] = model_id
    if effort_level:
        params["effortLevel"] = effort_level
    return params


def user_control_params(workflow_id: str, reason: str | None = None) -> dict[str, Any]:
    """Params for a pause/resume/cancel/delete a person asked for."""
    if not workflow_id:
        raise ValueError("workflow_id is required")
    params: dict[str, Any] = {"workflowId": workflow_id, "initiator": INITIATOR_USER}
    trimmed = control_reason(reason)
    if trimmed:
        params["reason"] = trimmed
    return params


def extend_repeat_params(
    workflow_id: str, node_id: str, iterations: int, reason: str | None = None
) -> dict[str, Any]:
    """``_kiro/workflow/resume`` with ``extendRepeat``: grant an exhausted repeat more passes.

    KAS persists the new budget as the repeat's ``extendedMaxIterations``,
    re-checks the stop condition before another iteration, and refuses a grant
    past :data:`MAX_REPEAT_ITERATIONS` in total.
    """
    if not node_id:
        raise ValueError("node_id is required")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise ValueError("iterations must be a whole number of at least 1")
    if iterations > MAX_REPEAT_ITERATIONS:
        raise ValueError(f"iterations must be at most {MAX_REPEAT_ITERATIONS}")
    params = user_control_params(workflow_id, reason)
    params["extendRepeat"] = {"nodeId": node_id, "additionalIterations": iterations}
    return params


def end_step_params(
    workflow_id: str, node_id: str, status: str, reason: str | None = None
) -> dict[str, Any]:
    """``_kiro/workflow/update`` ``update_status`` targeting *node_id*.

    KAS ends a PAUSED step with ``completed`` or ``failed``, or force-completes
    the repeat the run is paused on at ``maxIterations`` (``completed`` only:
    "end the loop and continue"). Anything else answers ``updated: false``.
    """
    if not workflow_id or not node_id:
        raise ValueError("workflow_id and node_id are required")
    if status not in STEP_END_STATUSES:
        raise ValueError("status must be completed or failed")
    params: dict[str, Any] = {
        "workflowId": workflow_id,
        "action": "update_status",
        "status": status,
        "nodeId": node_id,
        "initiator": INITIATOR_USER,
    }
    trimmed = control_reason(reason)
    if trimmed:
        params["statusReason"] = trimmed
    return params


def retry_params(workflow_id: str, node_id: str | None = None) -> dict[str, Any]:
    """``_kiro/workflow/retry``: every failed/aborted step, or one by id."""
    if not workflow_id:
        raise ValueError("workflow_id is required")
    params: dict[str, Any] = {"workflowId": workflow_id}
    if node_id:
        params["nodeId"] = node_id
    return params


# ── Pause reasons and actions ──


@dataclass(frozen=True)
class PauseInfo:
    """KAS's structured ``WorkflowState.pause`` (today only ``maxIterations``)."""

    kind: str
    node_id: str
    max_iterations: int | None = None
    iterations_run: int | None = None


def parse_pause_info(value: Any) -> PauseInfo | None:
    item = as_mapping(value)
    kind = as_str(item.get("kind")) if item else None
    node_id = as_str(item.get("nodeId")) if item else None
    if item is None or kind is None or node_id is None:
        return None
    return PauseInfo(
        kind=kind,
        node_id=node_id,
        max_iterations=as_count(item.get("maxIterations")),
        iterations_run=as_count(item.get("iterationsRun")),
    )


def pause_detail_class(value: Any) -> str | None:
    """``pauseDetail.class``, or None."""
    item = as_mapping(value)
    return as_str(item.get("class")) if item else None


#: The pause reason KAS stamps on a run whose owning process died mid-run
#: (kiro-agent ``workflow/run-liveness.ts``). Resume fixes it, like a transient park.
STALE_RUNNING_PAUSE_REASON = (
    "Interrupted by agent restart; the previously running step was paused for resume."
)


def classify_pause(
    status: str,
    *,
    pause: PauseInfo | None,
    detail_class: str | None,
    pause_reason: str | None,
    user_stopped: bool,
    attention: tuple[Attention, ...],
    plan_waiting: bool = False,
) -> dict[str, Any] | None:
    """Why a paused run is paused, or None for a run that is not paused.

    Most specific first: an exhausted loop (KAS ``pause``), the two classified
    parks (``pauseDetail``: a transient error Resume fixes, or a spent
    continuation budget that needs an explicit resume), a plan waiting for
    approval, a step waiting for the person's answer, a person's own pause or
    stop, then a run a restart interrupted (Resume fixes it). Anything else is
    ``unknown`` with KAS's own text.
    """
    if status != STATUS_PAUSED:
        return None
    out: dict[str, Any] = {"reason": PAUSE_UNKNOWN, "message": pause_reason}
    waits = [a for a in attention if a.kind == ATTENTION_QUESTION]
    if pause is not None and pause.kind == PAUSE_KIND_MAX_ITERATIONS:
        out["reason"] = PAUSE_LOOP_EXHAUSTED
        out["nodeId"] = pause.node_id
        if pause.max_iterations is not None:
            done = pause.iterations_run
            out["iterations"] = {
                "done": done if done is not None else pause.max_iterations,
                "max": pause.max_iterations,
            }
    elif detail_class == PAUSE_CLASS_CONTINUATION:
        out["reason"] = PAUSE_RETRIES_EXHAUSTED
    elif detail_class == PAUSE_CLASS_TRANSIENT:
        out["reason"] = PAUSE_TRANSIENT_ERROR
    elif plan_waiting:
        out["reason"] = PAUSE_PLAN_APPROVAL
    elif waits:
        out["reason"] = PAUSE_AWAITING_INPUT
        out["nodeId"] = waits[0].node_id
    elif user_stopped:
        out["reason"] = PAUSE_USER
    elif pause_reason == STALE_RUNNING_PAUSE_REASON:
        out["reason"] = PAUSE_TRANSIENT_ERROR
    if "nodeId" not in out and attention:
        out["nodeId"] = attention[0].node_id
    return out


def run_actions(
    status: str,
    *,
    pause_pending: bool,
    loop_exhausted: bool,
    has_failed_nodes: bool,
    plan_card_held: bool = False,
) -> list[str]:
    """The run-level controls KAS accepts in *status*, in display order.

    - running: pause (not while a pause is already pending), cancel.
    - paused: resume (not on an exhausted loop, where a plain resume re-parks at
      once: grant passes or end the loop instead; and not while the run is
      parked on a plan approval whose card the router holds, *plan_card_held*:
      once its step moves on KAS answers a re-asked plan ``cancelled``, so
      Resume would reject the plan the card is asking about. With no card held,
      a failed re-ask, Resume stays: it is then the only way forward), cancel.
    - finished: ``retry`` for a failed/aborted run that has a failed or aborted
      step (KAS ``retry`` refuses otherwise), ``restart`` (``invoke`` on a
      finished run starts it again from scratch), ``delete``.
    """
    if status == STATUS_RUNNING:
        return ([] if pause_pending else [RUN_ACTION_PAUSE]) + [RUN_ACTION_CANCEL]
    if status == STATUS_PAUSED:
        resumable = not loop_exhausted and not plan_card_held
        return ([RUN_ACTION_RESUME] if resumable else []) + [RUN_ACTION_CANCEL]
    if status in TERMINAL_RUN_STATUSES:
        retry = status in (STATUS_FAILED, STATUS_ABORTED) and has_failed_nodes
        return ([RUN_ACTION_RETRY] if retry else []) + [RUN_ACTION_RESTART, RUN_ACTION_DELETE]
    return []


# ── Run-row controls ──

PRIMARY_OPEN = "open"
PRIMARY_PAUSE = "pause"
PRIMARY_RESUME = "resume"

DISPLAY_RUNNING = "running"
DISPLAY_PAUSING = "pausing"
DISPLAY_PAUSED = "paused"
DISPLAY_COMPLETED = "completed"
DISPLAY_FAILED = "failed"
DISPLAY_STOPPED = "stopped"

_TERMINAL_DISPLAY = {
    STATUS_COMPLETED: DISPLAY_COMPLETED,
    STATUS_FAILED: DISPLAY_FAILED,
    STATUS_ABORTED: DISPLAY_STOPPED,
}


#: Why a live run needs a person (``controls.attentionKind``).
ATTENTION_KIND_APPROVAL = "approval"
ATTENTION_KIND_INPUT = "input"
ATTENTION_KIND_PARKED = "parked"


@dataclass(frozen=True)
class RunControls:
    """What a run row offers, computed once here so every surface agrees.

    ``display`` is the state a person reads: a run waiting on a question, or
    paused, reads ``paused``; a RUNNING run whose only waits are tool or plan
    approvals keeps reading ``running`` (it is still running: other steps go
    on, and Pause still applies); a run whose pause was accepted but not yet
    applied reads ``pausing``; an aborted run reads ``stopped`` (grey, not an
    error). ``needs_attention`` is the IDE's one "Needs you" rule.
    """

    display: str
    primary: str | None
    can_stop: bool
    can_retry: bool
    can_delete: bool
    needs_attention: bool
    attention_kind: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "display": self.display,
            "primary": self.primary,
            "canStop": self.can_stop,
            "canRetry": self.can_retry,
            "canDelete": self.can_delete,
            "needsAttention": self.needs_attention,
            "attentionKind": self.attention_kind,
        }


def attention_kind(
    status: str, attention: tuple[Attention, ...], pause_reason: str | None
) -> str | None:
    """Why a live run needs a person, or None when it does not (the IDE's rule).

    A step waiting on an approval or on an answer always does. So does a run
    paused for any reason other than the person's own pause or stop: an
    exhausted loop, a transient error, spent retries, a restart, an unknown
    park. *pause_reason* is :func:`classify_pause`'s ``reason``.
    """
    if status in TERMINAL_RUN_STATUSES:
        return None
    if any(item.kind == ATTENTION_QUESTION for item in attention):
        return ATTENTION_KIND_INPUT
    if attention:
        return ATTENTION_KIND_APPROVAL
    if status == STATUS_PAUSED and pause_reason != PAUSE_USER:
        if pause_reason == PAUSE_PLAN_APPROVAL:
            return ATTENTION_KIND_APPROVAL
        if pause_reason == PAUSE_AWAITING_INPUT:
            return ATTENTION_KIND_INPUT
        return ATTENTION_KIND_PARKED
    return None


def run_controls(
    status: str,
    *,
    pause_pending: bool,
    attention: tuple[Attention, ...],
    pause_reason: str | None = None,
) -> RunControls:
    """The controls a run in *status* offers.

    The primary slot holds at most one action, in this order: nothing on a
    terminal run; "pause" on a running run whose waits are only approvals (the
    run is still running; the approval is answered on its card); "open" when a
    question wait, or any wait of a paused run, has a step session to open
    (resolving it is the way forward, so Resume is never offered instead);
    "pause" while running; "resume" while paused. While a pause is pending the
    slot stays empty and the row reads ``pausing``, so the button just pressed
    does not simply vanish. Stop is separate, available on every non-terminal
    run, and the dashboard always confirms it.

    *pause_reason* (:func:`classify_pause`'s ``reason``) decides whether a
    paused run with no wait still needs someone: everything but ``user`` does.
    """
    if status in TERMINAL_RUN_STATUSES:
        return RunControls(
            display=_TERMINAL_DISPLAY[status],
            primary=None,
            can_stop=False,
            can_retry=status in (STATUS_FAILED, STATUS_ABORTED),
            can_delete=True,
            needs_attention=False,
        )
    kind = attention_kind(status, attention, pause_reason)
    questions = [item for item in attention if item.kind == ATTENTION_QUESTION]
    openable = any(
        item.session_id for item in (attention if status == STATUS_PAUSED else questions)
    )
    if pause_pending and status == STATUS_RUNNING:
        display, primary = DISPLAY_PAUSING, None
    elif openable:
        display, primary = DISPLAY_PAUSED, PRIMARY_OPEN
    elif status == STATUS_RUNNING:
        display = DISPLAY_PAUSED if questions else DISPLAY_RUNNING
        primary = PRIMARY_PAUSE
    else:
        display, primary = DISPLAY_PAUSED, PRIMARY_RESUME
    return RunControls(
        display=display,
        primary=primary,
        can_stop=True,
        can_retry=False,
        can_delete=False,
        needs_attention=kind is not None,
        attention_kind=kind,
    )
