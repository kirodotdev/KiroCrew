"""KAS "approve for me": the session option, its spawn switch, and the engine's verdict.

With approve-for-me on, KAS judges each tool call itself, shell commands and file
edits included. On a process started with approve-for-me on, Crew declares
``checksAutoApprovals`` at ``initialize`` (:func:`with_checks_auto_approvals`), so
a call KAS would approve does not run:
KAS sends ``session/request_permission`` carrying ``_meta.kiro.autoApproval``
(:func:`auto_approval_by`). The request takes Crew's usual permission path, deny
floor first, and only a call that passes every check Trust or YOLO would apply is
approved without a card (``dashboard.chat_turn.tool_approval``). Crew owns:

* **The engine switch.** ``KIRO_TRUST_CLASSIFIER_MODE`` decides whether the engine
  offers the feature at all, read once at start. Every child Crew spawns gets
  ``off`` (:func:`force_off`), whatever the gateway inherited. Only a session a
  person opted in starts with ``enforce``: its own ``extra_env`` carries it
  (:func:`session_env`), which keys it onto a process of its own, and the runtime
  spawn keeps it only from there (:func:`apply_runtime_env`).
* **The engine gate.** Only an engine known to send its verdict to the client
  instead of acting on it may hold the option on (:func:`engine_supported`). An
  engine seen approving a call on its own (``trustClassifierApproved`` on a tool
  call) is marked as not checking, the turn is cancelled and the option goes off
  (:func:`audit_classifier_approved`).
* **The session value.** Crew holds ``approveForMe`` at ``on`` or ``off``, never
  another value: :func:`settle` writes ``off`` unless the owner allows ``on``
  (:func:`register_owner_check`), when a session opens and before each turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

#: The session config option's id.
APPROVE_FOR_ME_CONFIG_ID = "approveForMe"

#: The engine's switch for the feature, read once when the engine starts.
CLASSIFIER_MODE_ENV = "KIRO_TRUST_CLASSIFIER_MODE"
#: The client capability asking the engine to send its verdict as a permission request.
CHECKS_AUTO_APPROVALS_CAPABILITY = "checksAutoApprovals"
CLASSIFIER_MODE_ON = "enforce"
CLASSIFIER_MODE_OFF = "off"

#: The ``_meta.kiro`` key on a tool call the engine approved on its own.
CLASSIFIER_APPROVED_META_KEY = "trustClassifierApproved"

#: The ``_meta.kiro`` key on a permission request the engine would have approved.
AUTO_APPROVAL_META_KEY = "autoApproval"
#: What ``autoApproval.by`` may name.
AUTO_APPROVAL_SOURCES = frozenset({"classifier", "preset"})

#: SEL ``source`` of the rows this module writes.
SEL_SOURCE = "kas_approve_for_me"

#: Tool-call ids kept per session; the oldest half is dropped past this.
_KEPT_MAX = 4096
#: Longest engine-written tool-call id kept or written to an audit row.
CALL_ID_MAX_CHARS = 256
#: Longest engine-written title / kind kept per call; the audit row uses 120 chars.
_LABEL_TITLE_MAX = 1024
_LABEL_KIND_MAX = 64

_ON = "on"
_OFF = "off"


def _deny_all(_session_key: str) -> bool:
    return False


_owner_check: Callable[[str], bool] = _deny_all

#: Whether the engine this gateway runs sends its verdict as a permission request.
#: ``None`` until read (:func:`refresh_engine_support`); read as no.
_ENGINE_SUPPORT: list[bool | None] = [None]
#: Set once an engine approved a call without asking; it then never reads as supported.
_SEEN_UNCHECKED: list[bool] = [False]


def register_owner_check(check: Callable[[str], bool]) -> None:
    """Install the answer to "may this session's approve-for-me stay on?".

    *check* gets the session key and runs on the event loop, so it must only read
    memory. Until one is installed, and whenever it raises, the answer is no.
    """
    global _owner_check
    _owner_check = check


def reset_owner_check() -> None:
    """Back to holding every session ``off``, engine support unknown. For tests."""
    register_owner_check(_deny_all)
    _ENGINE_SUPPORT[0] = None
    _SEEN_UNCHECKED[0] = False


def refresh_engine_support() -> bool:
    """Read whether the engine Crew runs checks auto-approvals with its client. Blocking.

    Only Crew's own engine bundle has a version Crew knows: kiro-cli's relay starts
    an engine of kiro-cli's choosing, so it reads as no. The bundle's version must
    reach the first release that sends the verdict
    (``kas_pin.KAS_CHECKS_AUTO_APPROVALS_MIN_ENGINE_VERSION``). Any error reads as
    no. Once an engine was seen approving on its own, the answer stays no.
    """
    if _SEEN_UNCHECKED[0]:
        _ENGINE_SUPPORT[0] = False
        return False
    try:
        from kiro_crew.acp.harness.kas import _bundle_route_selected
        from kiro_crew.acp.kas_pin import engine_checks_auto_approvals, pinned_version

        supported = _bundle_route_selected() and engine_checks_auto_approvals(pinned_version())
    except Exception:
        logger.warning("could not tell whether the engine checks auto-approvals", exc_info=True)
        supported = False
    _ENGINE_SUPPORT[0] = bool(supported)
    return bool(supported)


def engine_supported() -> bool:
    """Whether the engine is known to check auto-approvals with Crew. Memory only."""
    return _ENGINE_SUPPORT[0] is True


def may_stay_on(session_key: str) -> bool:
    """Whether *session_key* may hold approve-for-me on (fail closed)."""
    if not engine_supported():
        return False
    try:
        return bool(_owner_check(session_key))
    except Exception:
        logger.warning("approve-for-me owner check raised; holding the session off", exc_info=True)
        return False


def option_value(config_options: object) -> str | None:
    """The session's ``approveForMe`` value; ``None`` when it offers no such option."""
    if not isinstance(config_options, list):
        return None
    for opt in config_options:
        if isinstance(opt, dict) and opt.get("id") == APPROVE_FOR_ME_CONFIG_ID:
            value = opt.get("currentValue")
            return value if isinstance(value, str) else ""
    return None


def settle_target(current: str | None, *, allowed: bool) -> str | None:
    """The value to write for a session that reads *current*, or ``None`` for none."""
    if current is None:
        return None
    target = _ON if allowed else _OFF
    return None if current == target else target


class ApproveForMeNotOff(RuntimeError):
    """The session reads ``on``, may not, and the engine refused to turn it off."""


async def settle(handle: Any) -> None:
    """Hold *handle*'s ``approveForMe`` at ``on`` or ``off``, never another value.

    A no-op for a session that offers no option. A failed write is logged; when the
    session still reads ``on`` without leave, :class:`ApproveForMeNotOff` is raised
    so the caller sends no prompt under it.
    """
    handle._kas_afm_cancel_sent = False
    current = option_value(getattr(handle, "config_options", None))
    if current is None:
        return
    await asyncio.to_thread(refresh_engine_support)
    target = settle_target(current, allowed=current is not None and may_stay_on(_key_of(handle)))
    if target is None:
        return
    try:
        await handle.set_config_option(APPROVE_FOR_ME_CONFIG_ID, target)
    except Exception as exc:
        logger.warning(
            "could not set approve-for-me to %s: %s: %s", target, type(exc).__name__, exc
        )
        if current == _ON:
            raise ApproveForMeNotOff(
                "approve-for-me is on for this session and could not be turned off"
            ) from exc
        return
    logger.info("approve-for-me set to %s for session %s", target, _key_of(handle) or "?")


def _key_of(handle: Any) -> str:
    key = getattr(handle, "_session_key", "")
    return key if isinstance(key, str) else ""


def force_off(env: MutableMapping[str, str]) -> None:
    """Turn the engine switch off in a child's *env*, overriding an inherited value."""
    env[CLASSIFIER_MODE_ENV] = CLASSIFIER_MODE_OFF


def enable(env: MutableMapping[str, str]) -> None:
    """Turn the engine switch on in *env*."""
    env[CLASSIFIER_MODE_ENV] = CLASSIFIER_MODE_ON


def session_env(session_key: str, extra_env: dict[str, str] | None) -> dict[str, str] | None:
    """*extra_env* for a session about to start: with ``enforce`` only when it may be on.

    The value is part of the session's process key, so an opted-in session never
    shares a process with one that is not. Any inherited switch value in
    *extra_env* is dropped first, so only the owner's leave can put it there.
    """
    env = {k: v for k, v in (extra_env or {}).items() if k != CLASSIFIER_MODE_ENV}
    if session_key and may_stay_on(session_key):
        enable(env)
    if env == (extra_env or {}):
        return extra_env
    return env or None


def apply_runtime_env(env: MutableMapping[str, str], backend: str, own_env: object) -> None:
    """Settle the engine switch in a runtime child's *env*, after every other source.

    ``enforce`` only for a backend in ``ACP_BACKENDS_APPROVE_FOR_ME`` whose runtime's
    own ``extra_env`` (*own_env*, set by :func:`session_env`) asks for it, else
    ``off`` for that backend, whatever the gateway or ``agent.env`` carried. The
    kiro-cli harness writes ``off`` itself; any other backend never reads the
    switch, so its env is left as main leaves it.
    """
    from kiro_crew.acp.types import ACP_BACKENDS_APPROVE_FOR_ME

    if backend not in ACP_BACKENDS_APPROVE_FOR_ME:
        return
    if wants_on(own_env):
        enable(env)
    else:
        force_off(env)


def wants_on(own_env: object) -> bool:
    """Whether a runtime's own ``extra_env`` (from :func:`session_env`) turns it on."""
    return isinstance(own_env, dict) and own_env.get(CLASSIFIER_MODE_ENV) == CLASSIFIER_MODE_ON


def with_checks_auto_approvals(
    capabilities: dict[str, Any], backend: str, own_env: object
) -> dict[str, Any]:
    """*capabilities* declaring ``checksAutoApprovals``, only for an opted-in process.

    Only a process :func:`apply_runtime_env` starts with ``enforce`` can make an
    auto-approval call, so every other handshake is left as the harness's constant.
    Returns a copy; the constant is never changed.
    """
    from kiro_crew.acp.types import ACP_BACKENDS_APPROVE_FOR_ME

    if backend not in ACP_BACKENDS_APPROVE_FOR_ME or not wants_on(own_env):
        return capabilities
    meta = dict(capabilities.get("_meta") or {})
    meta["kiro"] = {**(meta.get("kiro") or {}), CHECKS_AUTO_APPROVALS_CAPABILITY: True}
    return {**capabilities, "_meta": meta}


def auto_approval_by(params: object) -> str:
    """``by`` of a permission request's ``_meta.kiro.autoApproval``, or ``""``.

    Only the shape the engine documents counts: ``{"decision": "allow", "by":
    "classifier" | "preset"}``. Anything else is no verdict, so the request is an
    ordinary ask.
    """
    meta = params.get("_meta") if isinstance(params, dict) else None
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    verdict = kiro.get(AUTO_APPROVAL_META_KEY) if isinstance(kiro, dict) else None
    if not isinstance(verdict, dict) or verdict.get("decision") != "allow":
        return ""
    by = verdict.get("by")
    return by if isinstance(by, str) and by in AUTO_APPROVAL_SOURCES else ""


def classifier_approved(update: object) -> bool:
    """Whether a ``tool_call`` / ``tool_call_update`` says the engine approved it."""
    if not isinstance(update, dict):
        return False
    meta = update.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    return isinstance(kiro, dict) and kiro.get(CLASSIFIER_APPROVED_META_KEY) is True


def classifier_denied(update: object) -> bool:
    """Whether a tool update says KAS's own classifier refused the call.

    KAS refuses it with ``_meta.kiro.policyDenial`` naming an ``evaluate`` rule and
    sends no permission request, so Crew learns of it only here.
    """
    meta = update.get("_meta") if isinstance(update, dict) else None
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    denial = kiro.get("policyDenial") if isinstance(kiro, dict) else None
    rule = denial.get("matchedRule") if isinstance(denial, dict) else None
    return isinstance(rule, dict) and rule.get("effect") == "evaluate"


#: The line a call KAS refused on its own starts its tool row with.
KAS_BLOCKED_LINE = "Blocked by KAS: it refused this call itself; Crew was not asked."


def _note_classifier_denied(
    handle: Any, update: dict[str, Any], call: tuple[str, str, str]
) -> None:
    """Mark the tool row and record, once per call, that KAS refused it."""
    call_id, title, kind = call
    content = update.get("content")
    block = {"type": "content", "content": {"type": "text", "text": KAS_BLOCKED_LINE}}
    update["content"] = [block, *(content if isinstance(content, list) else ())]
    seen = _kept(handle, "_kas_denied_audited")
    if call_id in seen:
        return
    seen[call_id] = None
    session_key = _key_of(handle) or f"acp:{getattr(handle, '_session_id', '')}"
    logger.warning("KAS refused a tool call itself on session %s", session_key)
    _spawn_audit(handle, session_key, (call_id, title, kind), "denied", "approve_for_me_kas_denied")


def _kept(handle: Any, attr: str) -> dict[str, Any]:
    kept = getattr(handle, attr, None)
    if not isinstance(kept, dict):
        kept = {}
        setattr(handle, attr, kept)
    if len(kept) > _KEPT_MAX:
        for old in list(kept)[: _KEPT_MAX // 2]:
            del kept[old]
    return kept


def _note_label(handle: Any, call_id: str, update: dict[str, Any]) -> tuple[str, str]:
    """The call's title and kind: this frame's, else the ones an earlier frame named."""
    labels = _kept(handle, "_kas_call_labels")
    title, kind = labels.get(call_id, ("", ""))
    if isinstance(update.get("title"), str) and update["title"]:
        title = update["title"][:_LABEL_TITLE_MAX]
    if isinstance(update.get("kind"), str) and update["kind"]:
        kind = update["kind"][:_LABEL_KIND_MAX]
    labels[call_id] = (title, kind)
    return title, kind


def audit_classifier_approved(handle: Any, update: dict[str, Any]) -> None:
    """Watch a session's tool-call frames for a call the engine decided on its own.

    Each frame's title and kind are kept per tool-call id, since the marked frame
    may carry neither. A call KAS's classifier refused is marked on its tool row and
    recorded once (:func:`classifier_denied`). With ``checksAutoApprovals`` declared
    the engine never runs a call on its own verdict, so a ``trustClassifierApproved``
    mark means this engine does not check auto-approvals: one SEL row names the
    call, the engine is marked as not checking (the option then reads off
    everywhere), and the running turn is cancelled once.
    """
    if not isinstance(update, dict):
        return
    raw_id = update.get("toolCallId")
    if not isinstance(raw_id, str) or not raw_id:
        return
    call_id = raw_id[:CALL_ID_MAX_CHARS]
    title, kind = _note_label(handle, call_id, update)
    if classifier_denied(update):
        _note_classifier_denied(handle, update, (call_id, title, kind))
        return
    if not classifier_approved(update):
        return
    seen = _kept(handle, "_kas_classifier_audited")
    if call_id in seen:
        return
    seen[call_id] = None
    _SEEN_UNCHECKED[0] = True
    _ENGINE_SUPPORT[0] = False
    session_key = _key_of(handle) or f"acp:{getattr(handle, '_session_id', '')}"
    logger.warning(
        "KAS approved a tool call without asking on session %s; approve-for-me is off", session_key
    )
    _spawn_audit(
        handle, session_key, (call_id, title, kind), "auto_approved", "approve_for_me_unchecked"
    )
    if not getattr(handle, "_kas_afm_cancel_sent", False) and _spawn(
        handle, lambda loop: loop.create_task(handle.cancel())
    ):
        handle._kas_afm_cancel_sent = True


def _spawn_audit(
    handle: Any, session_key: str, call: tuple[str, str, str], outcome: str, reason: str
) -> None:
    """Write one SEL row for a call KAS decided without Crew, off the event loop."""
    from kiro_crew.acp._dispatch import redact_text

    call_id, title, kind = call
    tool_name = redact_text(title)[:120] if title else "unknown"
    agent = getattr(handle, "active_agent", "") or "kirocrew"

    def _audit() -> None:
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=session_key,
                agent=agent,
                source=SEL_SOURCE,
                tool_name=tool_name,
                tool_kind=kind,
                outcome=outcome,
                request_id=call_id,
                metadata={"reason": reason, "decided_by": "kas", "crew_gate": "skipped"},
            )
        except Exception:
            logger.warning("SEL audit of a tool call KAS decided failed", exc_info=True)

    if not _spawn(handle, lambda loop: loop.create_task(asyncio.to_thread(_audit))):
        _audit()


def _spawn(handle: Any, start: Callable[[Any], Any]) -> bool:
    """Start a task on the running loop, kept on the handle; False with no loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = start(loop)
    tasks = getattr(handle, "_audit_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return True
