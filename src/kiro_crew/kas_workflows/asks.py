"""What a workflow step asks a person: questions, and the plan a creator step hands off.

A QUESTION from a ``user_input`` tool reaches Crew one of two ways, and the
way decides the shape of its answer (``PendingQuestion.via``):

- Over KAS's ``_kiro/userInput`` client request (:data:`VIA_USER_INPUT`), when
  the process was started with the ``userInput`` client capability
  (``acp.types.kas_client_capabilities``, on while ``agent.kas_workflows`` is
  on). The request carries the whole question: each option's title,
  description, ``recommended`` flag and sub-option toggles, and a question with
  no options is a free-form one the person types an answer to
  (:func:`question_from_user_input`). The answer is TEXT
  (``{action: "answered", answer}``), with picked sub-options appended as
  ``"Title [a, b]"``, or ``{action: "dismissed"}``.
- Over KAS's legacy permission bridge (:data:`VIA_BRIDGE`, kiro-agent
  ``handleUserInputViaPermissionRequest``) on a process started without it: one
  question per ``session/request_permission``, its text as
  ``toolCall.title``, and one ``allow_once`` option per answer, labelled with
  the answer. A free-form question never reaches a client on that bridge: KAS
  moves the step on itself. Every tool approval KAS raises offers a reject
  option, a question never does, which is how the two are told apart
  (:func:`is_question_request`). The answer is the picked option id.

Either way a question is held for a person under EVERY approval mode: picking
an answer is not an allow, so no mode may pick one.
- A PLAN approval (``update_workflow``), whose ``_meta.kiro.workflowPlan`` is
  KAS's ``WorkflowPlanApproval``: the steps to add as a tree. Display data
  (:func:`plan_payload`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from kiro_crew.kas_workflows import wire

#: Option kinds that refuse. A request offering none of them is a question.
_REJECT_KINDS = frozenset({"reject_once", "reject_always"})

#: Longest question text and answer label kept.
_MAX_PROMPT_CHARS = 4000
_MAX_LABEL_CHARS = 500
#: Longest option / sub-option description, and longest typed answer sent back.
_MAX_DESCRIPTION_CHARS = 2000
_MAX_ANSWER_CHARS = 4000
#: Options and sub-options per option kept from a ``_kiro/userInput`` question.
_MAX_OPTIONS = 50
_MAX_SUB_OPTIONS = 50

#: How a question reached Crew (``PendingQuestion.via``): KAS's legacy
#: permission bridge, or its ``_kiro/userInput`` client request.
VIA_BRIDGE = "bridge"
VIA_USER_INPUT = "user_input"
#: Plan nodes kept, and the longest string a plan node carries.
_MAX_PLAN_NODES = 400
_MAX_PLAN_STRING = 500

#: Characters a plan string is shown without: C0/C1 controls and the
#: bidirectional overrides and isolates that could make a tree read differently
#: from what is approved (KAS leaves neutralizing them to the client).
_BIDI = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_UNSAFE_RE = re.compile(f"[\u0000-\u001f\u007f-\u009f{_BIDI}]")


def _safe(text: Any, limit: int = _MAX_PLAN_STRING) -> str | None:
    """*text* with control and bidirectional characters replaced, capped; None when empty."""
    value = wire.as_str(text)
    if value is None:
        return None
    cleaned = _UNSAFE_RE.sub(" ", unicodedata.normalize("NFC", value[: limit * 2]))
    return cleaned[:limit] or None


def _redact(text: str) -> str:
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text)


# ── Questions ──


def is_question_request(params: Mapping[str, Any]) -> bool:
    """Whether a step's permission request is a ``user_input`` question.

    A question offers answers and nothing that refuses: every option is an
    ``allow_once`` named for an answer. A plan handoff and every tool approval
    offer a reject option, so neither matches; a request with no options is
    not a question either (nothing to pick).
    """
    options = [wire.as_mapping(o) for o in wire.as_list(params.get("options"))]
    if not options or any(o is None for o in options):
        return False
    kinds = {wire.as_str(o.get("kind")) for o in options if o is not None}
    return kinds == {"allow_once"} and not (kinds & _REJECT_KINDS)


@dataclass(frozen=True)
class SubOption:
    """One toggle shown under an option (``_kiro/userInput`` only); all start on."""

    label: str
    description: str | None = None
    #: The text sent back to KAS: the title as KAS sent it (``label`` is the
    #: redacted display copy). Empty means "same as label".
    answer: str = ""

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label}
        if self.description:
            out["description"] = self.description
        return out


@dataclass(frozen=True)
class QuestionOption:
    option_id: str
    label: str
    description: str | None = None
    recommended: bool = False
    sub_options_label: str | None = None
    sub_options: tuple[SubOption, ...] = ()
    #: The text sent back to KAS for this option (see :attr:`SubOption.answer`).
    answer: str = ""

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label}
        if self.description:
            out["description"] = self.description
        if self.recommended:
            out["recommended"] = True
        if self.sub_options:
            out["subOptions"] = [s.to_payload() for s in self.sub_options]
            if self.sub_options_label:
                out["subOptionsLabel"] = self.sub_options_label
        return out

    def compose(self, picked: tuple[SubOption, ...]) -> str:
        """KAS's answer text for this option with *picked* sub-options: ``"Title [a, b]"``."""
        title = self.answer or self.label
        if not picked:
            return title
        return f"{title} [{', '.join(s.answer or s.label for s in picked)}]"


@dataclass(frozen=True)
class PendingQuestion:
    """One step question waiting for a person's answer."""

    key: str
    request_id: int | str
    session_id: str
    workflow_id: str
    node_id: str
    question_id: str
    prompt: str
    options: tuple[QuestionOption, ...]
    created_at: float
    runtime: Any
    path: str | None = None
    slot: str | None = None
    #: :data:`VIA_BRIDGE` or :data:`VIA_USER_INPUT`: which request this answers,
    #: so which shape the answer takes (:meth:`result_for`).
    via: str = VIA_BRIDGE

    @property
    def is_user_input(self) -> bool:
        return self.via == VIA_USER_INPUT

    @property
    def free_form(self) -> bool:
        """A ``_kiro/userInput`` question with no options: the person types the answer."""
        return self.is_user_input and not self.options

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            # Empty for the chat's own question (no run, no step): null on the wire.
            "workflowId": self.workflow_id or None,
            "nodeId": self.node_id or None,
            "path": self.path,
            "slot": self.slot,
            "title": self.prompt,
            "createdAt": self.created_at,
            "questions": [
                {
                    "id": self.question_id,
                    "prompt": self.prompt,
                    "options": [o.to_payload() for o in self.options],
                    # KAS asks one question with one answer on both paths.
                    "multiSelect": False,
                    # Typed text only where KAS takes it: a free-form question.
                    "freeText": self.free_form,
                }
            ],
        }

    def _one_answer(self, answers: Mapping[str, Any]) -> Any:
        if set(answers) != {self.question_id}:
            raise ValueError("answer exactly the question this card asks")
        return answers[self.question_id]

    def option_for(self, answers: Mapping[str, Any]) -> str:
        """The option id the person's *answers* pick. Raises ValueError otherwise.

        One question with single-choice answers, so exactly one answer naming
        one offered option, by its label (or its option id).
        """
        raw = self._one_answer(answers)
        if isinstance(raw, list):
            if len(raw) != 1:
                raise ValueError("this question takes one answer")
            raw = raw[0]
        if not isinstance(raw, str) or not raw:
            raise ValueError("an answer must be one of the offered options")
        for option in self.options:
            if raw in (option.label, option.option_id):
                return option.option_id
        raise ValueError("that answer is not one of the offered options")

    def label_for(self, option_id: str) -> str | None:
        """The shown label of the offered option *option_id*, if it is one."""
        return next((o.label for o in self.options if o.option_id == option_id), None)

    def answer_text(self, answers: Mapping[str, Any]) -> str:
        """KAS's ``_kiro/userInput`` answer text for *answers*. Raises ValueError otherwise.

        - A free-form question takes the typed text (a string, or a one-element
          list), trimmed and non-empty.
        - Otherwise the answer names one offered option by its label: a plain
          string sends just the title; a list ``[label, *sub-option labels]``
          sends the title with the picked sub-options, in offered order, as
          ``"Title [a, b]"`` (KAS's own notation; an empty pick sends the title).
        """
        raw = self._one_answer(answers)
        if self.free_form:
            if isinstance(raw, list):
                if len(raw) != 1:
                    raise ValueError("this question takes one answer")
                raw = raw[0]
            text = raw.strip() if isinstance(raw, str) else ""
            if not text:
                raise ValueError("type an answer")
            if len(text) > _MAX_ANSWER_CHARS:
                raise ValueError(f"an answer is at most {_MAX_ANSWER_CHARS} characters")
            return text
        picked_subs: list[Any] = []
        if isinstance(raw, list):
            if not raw:
                raise ValueError("this question takes one answer")
            raw, picked_subs = raw[0], list(raw[1:])
        if not isinstance(raw, str) or not raw:
            raise ValueError("an answer must be one of the offered options")
        option = next((o for o in self.options if raw in (o.label, o.option_id)), None)
        if option is None:
            raise ValueError("that answer is not one of the offered options")
        if any(not isinstance(s, str) for s in picked_subs) or len(set(picked_subs)) != len(
            picked_subs
        ):
            raise ValueError("pick each sub-option at most once")
        offered = {s.label for s in option.sub_options}
        if not set(picked_subs) <= offered:
            raise ValueError("that sub-option is not offered for this answer")
        chosen = set(picked_subs)
        return option.compose(tuple(s for s in option.sub_options if s.label in chosen))

    def answer_value(self, answers: Mapping[str, Any]) -> str:
        """What the person's *answers* send back: an option id (bridge) or KAS's answer text.

        Raises ValueError when *answers* is not a valid answer to this question.
        """
        return self.answer_text(answers) if self.is_user_input else self.option_for(answers)

    def display_for(self, value: str) -> str:
        """The person-facing line for an :meth:`answer_value` result."""
        if self.is_user_input:
            return _redact(value)
        return self.label_for(value) or value

    def default_answer(self) -> str | None:
        """What a bare "allow" on a surface that cannot show the options answers.

        A channel offers a question only Allow / Deny. Allow on the legacy bridge
        always took the first answer; here it takes the recommended option (else
        the first) with every sub-option on, KAS's own defaults. A free-form
        question has nothing to pick, so None: the caller dismisses it.
        """
        if not self.options:
            return None
        option = next((o for o in self.options if o.recommended), self.options[0])
        if not self.is_user_input:
            return option.option_id
        return option.compose(option.sub_options)

    def result_for(self, value: str) -> dict[str, Any]:
        """The JSON-RPC result answering this question's own request with *value*."""
        if self.is_user_input:
            return user_input_answered(value)
        return {"outcome": {"outcome": "selected", "optionId": value}}

    def answer_result(self, answers: Mapping[str, Any]) -> dict[str, Any]:
        """:meth:`result_for` the person's *answers*. Raises ValueError otherwise."""
        return self.result_for(self.answer_value(answers))

    def dismiss_result(self) -> dict[str, Any]:
        """The JSON-RPC result that answers "no answer": KAS moves the step on."""
        return dict(USER_INPUT_DISMISSED) if self.is_user_input else dict(_BRIDGE_CANCELLED)


#: ``_kiro/userInput`` / legacy-bridge "no answer" results.
USER_INPUT_DISMISSED: dict[str, Any] = {"action": "dismissed"}
_BRIDGE_CANCELLED: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}


def user_input_answered(answer: str) -> dict[str, Any]:
    """The ``_kiro/userInput`` result carrying the person's *answer* text."""
    return {"action": "answered", "answer": answer}


def dismiss_result_for(method: str) -> dict[str, Any]:
    """The "no answer" result for a request of *method*, before any question is built."""
    from kiro_crew.agent_sdk.kas_workflows_driver import user_input_method

    return dict(USER_INPUT_DISMISSED) if method == user_input_method() else dict(_BRIDGE_CANCELLED)


def chat_question(
    *,
    request_id: int | str,
    tool_call_id: str,
    title: str,
    options: Any,
    slot: str,
    created_at: float,
    user_input: Mapping[str, Any] | None = None,
) -> PendingQuestion | None:
    """The chat's own ``user_input`` question as the step question card draws it.

    *user_input* is the ``_kiro/userInput`` request's params when the question
    came that way (``AcpEvent.user_input``): the rich question is built from it
    (:func:`question_from_user_input`). Otherwise it is the same legacy-bridge
    request a step sends (:func:`question_from_request`), read from the chat's
    permission event instead of raw params: *options* is ``AcpEvent.options``
    (``[{id, label}]``), *title* the question text. It has no run or step, so
    ``workflowId`` / ``nodeId`` are null on the wire, and its key is the
    permission request id the chat's approve route answers.
    """
    if isinstance(user_input, Mapping):
        return question_from_user_input(
            user_input,
            key=str(request_id),
            request_id=request_id,
            session_id="",
            owner=("", ""),
            created_at=created_at,
            runtime=None,
            path=None,
            slot=slot,
        )
    params = {
        "toolCall": {"toolCallId": tool_call_id or None, "title": title or None},
        "options": [
            {"optionId": o.get("id"), "name": o.get("label") or None, "kind": "allow_once"}
            for o in (wire.as_mapping(raw) for raw in wire.as_list(options))
            if o is not None
        ],
    }
    return question_from_request(
        params,
        key=str(request_id),
        request_id=request_id,
        session_id="",
        owner=("", ""),
        created_at=created_at,
        runtime=None,
        path=None,
        slot=slot,
    )


def question_from_request(
    params: Mapping[str, Any],
    *,
    key: str,
    request_id: int | str,
    session_id: str,
    owner: tuple[str, str],
    created_at: float,
    runtime: Any,
    path: str | None,
    slot: str | None,
) -> PendingQuestion | None:
    """A held question for a question-shaped request, or None when it has no usable option."""
    tool_call = wire.as_mapping(params.get("toolCall")) or {}
    options: list[QuestionOption] = []
    seen: set[str] = set()
    for raw in wire.as_list(params.get("options")):
        item = wire.as_mapping(raw) or {}
        option_id = wire.as_str(item.get("optionId"))
        if option_id is None or option_id in seen:
            continue
        seen.add(option_id)
        label = _redact(wire.as_str(item.get("name")) or option_id)[:_MAX_LABEL_CHARS]
        options.append(QuestionOption(option_id=option_id, label=label))
    if not options:
        return None
    prompt = _redact(wire.as_str(tool_call.get("title")) or "The step has a question.")
    return PendingQuestion(
        key=key,
        request_id=request_id,
        session_id=session_id,
        workflow_id=owner[0],
        node_id=owner[1],
        question_id=wire.as_str(tool_call.get("toolCallId")) or "question",
        prompt=prompt[:_MAX_PROMPT_CHARS],
        options=tuple(options),
        created_at=created_at,
        runtime=runtime,
        path=path,
        slot=slot,
    )


def _shown(value: Any, limit: int) -> str | None:
    """*value* as a person sees it: redacted, control/bidi characters neutralized, capped."""
    text = _safe(value, limit)
    return _redact(text)[:limit] if text else None


def question_from_user_input(
    params: Mapping[str, Any],
    *,
    key: str,
    request_id: int | str,
    session_id: str,
    owner: tuple[str, str],
    created_at: float,
    runtime: Any,
    path: str | None,
    slot: str | None,
) -> PendingQuestion:
    """A held question for KAS's ``_kiro/userInput`` request *params*.

    ``{sessionId, toolCallId, question, options: [{title, description?,
    recommended?, subOptionsLabel?, subOptions?: [{title, description?}]}]}``
    (kiro-agent ``acp-type-covenant`` ``UserInputRequest``). Never None: a
    question with no usable option is a free-form one, which this path can ask.
    Options are kept in KAS's order, deduplicated by title; option ids are
    positional (``answer-<n>``) because KAS names none and takes the title back.
    """
    options: list[QuestionOption] = []
    seen: set[str] = set()
    recommended_seen = False
    for raw in wire.as_list(params.get("options"))[:_MAX_OPTIONS]:
        item = wire.as_mapping(raw) or {}
        answer = wire.as_str(item.get("title"))
        label = _shown(answer, _MAX_LABEL_CHARS)
        if answer is None or label is None or label in seen:
            continue
        seen.add(label)
        subs: list[SubOption] = []
        sub_seen: set[str] = set()
        for raw_sub in wire.as_list(item.get("subOptions"))[:_MAX_SUB_OPTIONS]:
            sub = wire.as_mapping(raw_sub) or {}
            sub_answer = wire.as_str(sub.get("title"))
            sub_label = _shown(sub_answer, _MAX_LABEL_CHARS)
            if sub_answer is None or sub_label is None or sub_label in sub_seen:
                continue
            sub_seen.add(sub_label)
            subs.append(
                SubOption(
                    label=sub_label,
                    description=_shown(sub.get("description"), _MAX_DESCRIPTION_CHARS),
                    answer=sub_answer[:_MAX_LABEL_CHARS],
                )
            )
        # One recommendation at most: a second would make "pre-select the
        # recommended answer" ambiguous, so the first one KAS marked wins.
        recommended = item.get("recommended") is True and not recommended_seen
        recommended_seen = recommended_seen or recommended
        options.append(
            QuestionOption(
                option_id=f"answer-{len(options)}",
                label=label,
                description=_shown(item.get("description"), _MAX_DESCRIPTION_CHARS),
                recommended=recommended,
                sub_options_label=_shown(item.get("subOptionsLabel"), _MAX_LABEL_CHARS),
                sub_options=tuple(subs),
                answer=answer[:_MAX_LABEL_CHARS],
            )
        )
    prompt = _redact(wire.as_str(params.get("question")) or "The step has a question.")
    return PendingQuestion(
        key=key,
        request_id=request_id,
        session_id=session_id,
        workflow_id=owner[0],
        node_id=owner[1],
        question_id=wire.as_str(params.get("toolCallId")) or "question",
        prompt=prompt[:_MAX_PROMPT_CHARS],
        options=tuple(options),
        created_at=created_at,
        runtime=runtime,
        path=path,
        slot=slot,
        via=VIA_USER_INPUT,
    )


# ── Plans ──


def _plan_node(value: Any, depth: int, budget: list[int]) -> dict[str, Any] | None:
    """One ``WorkflowPlanNode`` as a surface draws it, or None past the caps."""
    item = wire.as_mapping(value)
    if item is None or depth > wire.MAX_PLAN_DEPTH or budget[0] <= 0:
        return None
    node_id = _safe(item.get("nodeId")) or _safe(item.get("id"))
    node_type = wire.as_str(item.get("type"))
    if node_id is None or node_type not in wire.NODE_TYPES:
        return None
    budget[0] -= 1
    children: list[dict[str, Any]] = []
    for key in ("steps", "branches"):
        for raw in wire.as_list(item.get(key)):
            child = _plan_node(raw, depth + 1, budget)
            if child is not None:
                children.append(child)
    included = wire.parse_included_from(item.get("includedFrom"))
    task = wire.as_mapping(item.get("task"))
    task_text = _safe(task.get("text")) if task else None
    model = wire.as_str(item.get("modelId"))
    return {
        "id": node_id,
        "type": node_type,
        "agent": _safe(item.get("agentName")) or _safe(item.get("agent")),
        "label": node_id,
        "model": _safe(model) if model and model != "auto" else None,
        "modelSource": wire.as_str(item.get("modelSource")),
        "effort": _safe(item.get("effortLevel")),
        "effortSource": wire.as_str(item.get("effortSource")),
        "maxIterations": wire.as_count(item.get("maxIterations")),
        "joinPolicy": wire.as_str(item.get("joinPolicy")),
        "include": (
            {"recipe": _safe(included[0]), "path": _safe(included[1])} if included else None
        ),
        "task": (
            {"text": _redact(task_text), "truncated": task.get("truncated") is True}
            if task and task_text
            else None
        ),
        "children": children,
    }


def plan_nodes(workflow_plan: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
    """The steps a plan approval adds, as a tree; None when KAS sent no tree."""
    if workflow_plan is None:
        return None
    steps = wire.as_list(workflow_plan.get("steps"))
    if not steps:
        return None
    budget = [_MAX_PLAN_NODES]
    nodes = [n for n in (_plan_node(raw, 0, budget) for raw in steps) if n is not None]
    return nodes or None


def plan_payload(text: str | None, workflow_plan: Mapping[str, Any] | None) -> dict[str, Any]:
    """A plan approval's ``plan``: KAS's text (always sent) and, when sent, the tree.

    ``nodes`` is absent when KAS dropped the tree to stay under its size cap
    (``omitted: "steps"``) or sent no ``workflowPlan``; the text is the fallback.
    """
    out: dict[str, Any] = {"text": text or ""}
    nodes = plan_nodes(workflow_plan)
    if nodes is not None:
        out["nodes"] = nodes
    if workflow_plan is not None:
        kind = wire.as_str(workflow_plan.get("kind"))
        if kind:
            out["kind"] = kind
        kept = [s for s in (_safe(k) for k in wire.as_list(workflow_plan.get("keptStepIds"))) if s]
        if kept:
            out["keptStepIds"] = kept
        omitted = wire.as_str(workflow_plan.get("omitted"))
        if omitted:
            out["omitted"] = omitted
    return out


__all__ = [
    "USER_INPUT_DISMISSED",
    "VIA_BRIDGE",
    "VIA_USER_INPUT",
    "PendingQuestion",
    "QuestionOption",
    "SubOption",
    "chat_question",
    "dismiss_result_for",
    "is_question_request",
    "plan_nodes",
    "plan_payload",
    "question_from_request",
    "question_from_user_input",
    "user_input_answered",
]
