"""Qoder's per-call permission route and its fail-closed runtime witnesses.

Qoder has no resolved-permission read-back, so each call carries the route
witness: a fresh permission ask, a Crew answer that never selects allow-always,
execution only after that answer, and terminal retirement. The session-level
version and mode witnesses run before and during the turn, and history replay
stays unarmed until the first live prompt.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method:
the class binds it by name, so it reads and writes the session's own state on the
instance, and a patch of the method on the class or on an instance reaches every
caller.

Instance fields written: ``_last_substitution_model``, ``_model``,
``_permission_options``, ``_qoder_approved_ids``, ``_qoder_asked_ids``,
``_qoder_bookkeeping_overflow``, ``_qoder_denied_ids``,
``_qoder_executed_ids``, ``_qoder_request_tool``,
``_resolved_model_id``, ``model_pin_refused``.

Instance fields read: ``_mcp_notifications``, ``_permission_gate_events``,
``_qoder_default_model_id``, ``_qoder_live_prompt_seen``, ``_session_id``,
``_session_key``.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from typing import TYPE_CHECKING, Any

from kiro_crew import acp_tool_gate, model_registry
from kiro_crew import sel as sel_module
from kiro_crew.acp._dispatch import (
    _MAX_HARNESS_TOOL_ID_LEN,
    _loggable_request_id,
    parse_session_modes,
)
from kiro_crew.acp.client_permissions import _scrub_observed
from kiro_crew.acp.client_timeouts import _INIT_TIMEOUT
from kiro_crew.acp.client_tool_events import _MAX_CACHED_TOOL_PARAMS
from kiro_crew.acp.client_transport import _jsonrpc_error_code
from kiro_crew.acp.runtime_models import DEFAULT_MODEL
from kiro_crew.acp.runtime_models import advertised_model_ids as _runtime_advertised_model_ids
from kiro_crew.acp.transport_errors import AcpModelUnavailable, AcpToolGateUnroutable
from kiro_crew.acp.types import (
    ACP_BACKENDS_ADVERTISED_MODEL_EXCLUSIVE,
    QODER_REQUIRED_MODE,
    TERMINAL_TOOL_STATUSES,
    UPDATE_CURRENT_MODE,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    JsonRpcMessage,
    model_registry_namespace,
)
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

# The concrete class is injected at run time by the facade after ``AcpClient`` is defined,
# so this name is unbound until then and must be set before use. The owner must not import
# the facade at run time, and the catalog helper needs only to launch one ordinary client.

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")

# Qoder names calls with the same class of harness-authored identifiers as the
# gate-extension envelope reader. Use the shared id bound; a malformed id is
# not a witness that can authorize anything, so it is dropped rather than cut.
_QODER_TOOL_CALL_ID_MAX_CHARS = _MAX_HARNESS_TOOL_ID_LEN

#: JSON-RPC ``-32000`` is the code qodercli answers a ``session/new`` or
#: ``session/load`` with when no login exists, worded ``Authentication required: ...``.
_JSONRPC_AUTH_REQUIRED = -32000


def _qoder_config_mode(update: dict[str, Any]) -> str:
    """Qoder's mode value from a ``config_option_update``, or ``""``.

    The wire shape is the full ``configOptions`` select array, not a single
    changed pair. Requiring both the measured ``id`` and ``category`` keeps an
    unrelated ``model`` or effort update from being mistaken for a permission
    move.
    """
    options = update.get("configOptions")
    if not isinstance(options, list):
        return ""
    for option in options:
        if (
            isinstance(option, dict)
            and option.get("id") == "mode"
            and option.get("category") == "mode"
        ):
            value = option.get("currentValue")
            return value if isinstance(value, str) else ""
    return ""


def _qoder_mode_from_update(msg: JsonRpcMessage) -> str | None:
    """The permission mode a ``session/update`` names, or ``None`` when it names none.

    Qoder reports the mode in two envelopes: ``current_mode_update`` and the
    ``mode`` select of a ``config_option_update``. The open-time buffer scan and
    the live tripwire both read through here, so they cannot disagree about what
    a frame says.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") == "config_option_update":
        return _qoder_config_mode(update)
    if update.get("sessionUpdate") != UPDATE_CURRENT_MODE:
        return None
    mode_id = update.get("currentModeId")
    return mode_id if isinstance(mode_id, str) else ""


def _is_qoder_signed_out_error(error: object) -> bool:
    """Whether a JSON-RPC error frame is qodercli's answer to a signed-out client.

    Both halves must hold: the code is -32000 AND the message contains
    ``Authentication required`` in any case. Either one alone is another failure,
    since that code is shared and that wording can come back under another code.
    """
    if _jsonrpc_error_code(error) != _JSONRPC_AUTH_REQUIRED or not isinstance(error, dict):
        return False
    message = error.get("message")
    return isinstance(message, str) and "authentication required" in message.casefold()


async def _resolve_qoder_model_id(self: AcpClient, model_id: str) -> str:
    """Resolve one explicit pick against Qoder's service-owned catalog.

    This is the exclusive-advertised branch of :func:`set_model`. It returns the
    id to send, or an empty string for an ``auto`` no-write, or raises.
    """
    advertised = self._advertised_model_ids()
    if model_id == DEFAULT_MODEL:
        # The response default is the only measured meaning of ``auto``.
        # Write it when a later explicit switch moved the session away;
        # if the session already serves it, the no-write is exact.
        default = self._qoder_default_model_id
        if not default or default not in advertised:
            raise AcpModelUnavailable(model_id, advertised, backend=self.backend)
        if self._resolved_model_id == default:
            self.model_pin_refused = ""
            self._last_substitution_model = None
            self._model = DEFAULT_MODEL
            self._resolved_model_id = default
            return ""
        return default
    if not advertised or model_id not in advertised:
        # The live snapshot can be stale. One bounded probe is the only
        # refresh; an empty/failing answer is not evidence that widens
        # the vocabulary, so the original refusal stands.
        refreshed = await self.refresh_available_models()
        refreshed_ids = _runtime_advertised_model_ids(refreshed)
        if model_id in refreshed_ids:
            return model_id
        _rejected_log, _ = redact_exfiltration_urls(str(model_id))
        _rejected_log, _ = redact_credentials(_rejected_log)
        raise AcpModelUnavailable(_rejected_log, advertised, backend=self.backend)
    return model_id


async def _withhold_qoder_startup_model(self: AcpClient) -> bool:
    """Withhold an inherited or unadvertised startup pin on Qoder.

    Returns True when the startup-model decision is complete. An inherited
    concrete pin is not Crew's registry choice; when the capture is empty, whether
    Qoder accepts it is unknowable, so it is not forwarded and the startup
    response decides without a second launch.
    """
    if self.backend not in ACP_BACKENDS_ADVERTISED_MODEL_EXCLUSIVE:
        return False
    advertised = self._advertised_model_ids()
    if advertised and self._model in advertised:
        return False
    _withheld_log = redact_log_via_context(str(self._model))
    logger.warning(
        "ACP model %s is not in Qoder's advertised vocabulary; staying on "
        "the served default %s (advertised: %s)",
        _withheld_log,
        self._resolved_model_id or DEFAULT_MODEL,
        ", ".join(advertised),
    )
    self.model_pin_refused = self._model
    self._model = DEFAULT_MODEL
    await self._ensure_served_default()
    return True


def _verify_qoder_version(self: AcpClient) -> None:
    """Refuse an unmeasured Qoder release before it opens a session.

    Qoder has no resolved-permission read-back, so the release is the first
    witness that argv precedence and the request frames still have the measured
    behaviour. The mode argument is the required constant only because this is
    the version half of the same fail-closed decision; the session's actual mode
    is checked immediately after session/new or restore.
    """
    if not self._is_qoder:
        return
    issue = acp_tool_gate.pinned_ask_rule_issue(
        self.backend,
        version=self.agent_version,
        observed_mode=QODER_REQUIRED_MODE,
    )
    if not issue:
        return
    remedy = acp_tool_gate.remediation_for(self.backend, reported_version=self.agent_version)
    try:
        acp_tool_gate.enforce_runtime_routing(self.backend, issue, remedy=remedy)
    except acp_tool_gate.ToolGateUnroutable as exc:
        # The user reads only the remedy; the full gate explanation goes to the log.
        logger.warning(
            "Qoder release refused before any session: %s",
            redact_log_via_context(str(exc)),
        )
        raise AcpToolGateUnroutable(remedy or str(exc)) from None


def _buffered_qoder_mode_issue(
    backend: str, version: object, notifications: list[JsonRpcMessage]
) -> tuple[str, str]:
    """The first buffered mode frame the pinned route refuses, as ``(observed, issue)``.

    ``("", "")`` means no buffered frame names a mode the route refuses. The
    observed mode is scrubbed before it leaves here, as the live tripwire scrubs it.
    """
    for msg in notifications:
        observed = _qoder_mode_from_update(msg)
        if observed is None:
            continue
        scrubbed = str(_scrub_observed(observed))
        issue = acp_tool_gate.pinned_ask_rule_issue(
            backend, version=version, observed_mode=scrubbed
        )
        if issue:
            return scrubbed, issue
    return "", ""


def _verify_qoder_routing(self: AcpClient, resp: dict) -> None:
    """Require Qoder's only session-level permission witness: the default mode.

    This does not read the resolved permission rules; none are exposed. It is a
    tripwire that an inline ask pin was ignored, a project file outranked it, or
    the release changed the response shape. Version has already been checked.

    Mode frames buffered while this response was awaited are judged here too,
    before the session opens.
    """
    if not self._is_qoder:
        return
    _ids, current_mode, advertised = parse_session_modes(resp)
    issue = acp_tool_gate.pinned_ask_rule_issue(
        self.backend,
        version=self.agent_version,
        observed_mode=current_mode if advertised else "",
    )
    if not issue:
        observed, issue = _buffered_qoder_mode_issue(
            self.backend, self.agent_version, self._mcp_notifications
        )
        if issue:
            logger.error(
                "qoder routing: a session/update buffered before the session opened "
                "named mode %r, which is not the required one; refusing the session "
                "before its first prompt [session=%s]",
                observed,
                self._session_id,
            )
    if not issue:
        return
    raise AcpToolGateUnroutable(
        "Qoder changed its permission mode, so Kiro Crew stopped it. Start a new chat."
    )


def _note_qoder_denied(self: AcpClient, request_id: str | int) -> None:
    """Remember the Qoder call whose request this client DENIED."""
    if not self._is_qoder:
        return
    tool_call_id = self._qoder_request_tool.pop(str(request_id), None)
    if tool_call_id:
        self._qoder_denied_ids.add(tool_call_id)
        self._qoder_approved_ids.discard(tool_call_id)


def _retire_qoder_request_tool(self: AcpClient, tool_call_id: str) -> None:
    """Forget request envelopes once a call has reached a terminal state.

    A late rejection still needs the approved-call envelope until execution
    ends; after that terminal, neither the request nor the verdict is live.
    """
    for request_id in [
        key for key, value in self._qoder_request_tool.items() if value == tool_call_id
    ]:
        self._qoder_request_tool.pop(request_id, None)
    if tool_call_id not in self._qoder_approved_ids and tool_call_id not in (
        self._qoder_executed_ids
    ):
        self._qoder_asked_ids.discard(tool_call_id)


def _make_room_for_qoder_request(self: AcpClient, incoming: str = "") -> None:
    """Evict only unanswered asks so an approved witness cannot be laundered.

    The request-id map exists so a late rejection can still name its call.
    Dropping an approved mapping would leave the approval armed while making
    the deny unreachable, so approved/executed entries are protected. Losing
    an unanswered ask is fail-closed: the later execution reads as unasked.
    If every slot is protected, there is no safe eviction; refuse rather
    than choose a witness to forget.
    """
    if len(self._qoder_request_tool) <= _MAX_CACHED_TOOL_PARAMS:
        while (
            len(self._qoder_asked_ids) >= _MAX_CACHED_TOOL_PARAMS
            and incoming not in self._qoder_asked_ids
        ):
            candidate = next(
                (
                    (request_id, value)
                    for request_id, value in self._qoder_request_tool.items()
                    if value not in self._qoder_approved_ids
                    and value not in self._qoder_executed_ids
                ),
                None,
            )
            if candidate is None:
                self._qoder_bookkeeping_overflow = True
                return
            request_id, value = candidate
            self._qoder_request_tool.pop(request_id, None)
            self._qoder_asked_ids.discard(value)
        return
    for request_id in [
        key
        for key, value in self._qoder_request_tool.items()
        if value not in self._qoder_approved_ids and value not in self._qoder_executed_ids
    ]:
        evicted = self._qoder_request_tool.pop(request_id, None)
        if evicted:
            self._qoder_asked_ids.discard(evicted)
        if len(self._qoder_request_tool) <= _MAX_CACHED_TOOL_PARAMS:
            return
    self._qoder_bookkeeping_overflow = True


async def _stop_qoder_on_bookkeeping_overflow(self: AcpClient, request_id: str | int) -> None:
    """Stop the harness when no safe bookkeeping eviction remains.

    The note site is synchronous, so it records the overflow without answering
    the request. This async tripwire does what every runtime tripwire does:
    audit the bypass, stop the child, and fail the session. It also releases
    the envelopes the just-built request had already recorded, because no
    answer will consume them.
    """
    if not self._is_qoder or not self._qoder_bookkeeping_overflow:
        return
    self._qoder_bookkeeping_overflow = False
    self._permission_options.pop(request_id, None)
    getattr(self, "_permission_gate_events", {}).pop(request_id, None)
    logger.error(
        "qoder gate: permission bookkeeping exceeded its bound while every "
        "request named an approved or executing tool call; the pinned ask "
        "rule can no longer be answered safely, so the harness is being "
        "stopped [session=%s]",
        self._session_id,
    )
    try:
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name=_loggable_request_id(request_id),
            tool_kind="other",
            outcome="denied",
            metadata={
                "reason": "qoder_bookkeeping_overflow",
                "backend": self.backend,
            },
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("qoder gate: audit of bookkeeping overflow failed", exc_info=True)
    await self._kill_process(force=True)
    raise AcpToolGateUnroutable(
        "Qoder permission bookkeeping exceeded its bound while every request "
        "named an approved or executing tool call; the pinned ask rule can no "
        "longer be answered safely, so the session is refused."
    )


def _note_qoder_asked(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Remember the Qoder call a permission frame is asking about.

    Qoder names the call directly, as DeepSeek does. A fresh ask is a fresh
    verdict: consuming only at execution/terminal prevents a reused id from
    inheriting an earlier answer.
    """
    if not self._is_qoder:
        return
    params = msg.params if isinstance(msg.params, dict) else {}
    tool_call = params.get("toolCall")
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    tool_call_id = tool_call.get("toolCallId")
    if (
        not isinstance(tool_call_id, str)
        or not tool_call_id
        or len(tool_call_id) > _QODER_TOOL_CALL_ID_MAX_CHARS
    ):
        return
    if (
        tool_call_id not in self._qoder_asked_ids
        and len(self._qoder_asked_ids) >= _MAX_CACHED_TOOL_PARAMS
    ):
        self._make_room_for_qoder_request(tool_call_id)
        if self._qoder_bookkeeping_overflow:
            return
    self._qoder_asked_ids.add(tool_call_id)
    self._qoder_denied_ids.discard(tool_call_id)
    self._qoder_executed_ids.discard(tool_call_id)
    self._qoder_approved_ids.discard(tool_call_id)
    if msg.id is not None:
        self._make_room_for_qoder_request(tool_call_id)
        if not self._qoder_bookkeeping_overflow:
            self._qoder_request_tool[str(msg.id)] = tool_call_id


async def _tripwire_qoder_mode(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Stop a Qoder session whose only mode witness leaves ``default``.

    The open/restore read-back cannot hold the session there. A mode update
    naming anything else means the pinned asking posture and the session's
    mode disagree, so stop before the next tool call can be auto-allowed.

    Qoder also reports selects through ``config_option_update``. Its ``mode``
    select is the same mode witness in a second envelope, so a mid-session
    move there gets exactly the same verdict.
    """
    if not self._is_qoder:
        return
    observed = _qoder_mode_from_update(msg)
    if observed is None:
        return
    issue = acp_tool_gate.pinned_ask_rule_issue(
        self.backend,
        version=self.agent_version,
        observed_mode=_scrub_observed(observed),
    )
    if not issue:
        return
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    is_config = isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update"
    await self._stop_qoder_on_mode_issue(
        issue, observed, "config option mode" if is_config else "session/set_mode"
    )


async def _stop_qoder_on_mode_issue(
    self: AcpClient, issue: str, observed: str, channel: str
) -> None:
    """Kill and convert one Qoder mode-witness refusal to the ACP error."""
    logger.error(
        "qoder routing: session reported mode %r mid-session, which is not the "
        "required one on %s; stopping the harness before it auto-allows a call "
        "[session=%s]",
        observed,
        channel,
        self._session_id,
    )
    try:
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name=channel,
            tool_kind="other",
            outcome="denied",
            metadata={
                "reason": "qoder_mode_tripwire",
                "backend": self.backend,
                "mode": _scrub_observed(observed),
                "channel": channel,
            },
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("qoder routing: audit of the mode tripwire failed", exc_info=True)
    await self._kill_process(force=True)
    raise AcpToolGateUnroutable(
        "Qoder changed its permission mode, so Kiro Crew stopped it. Start a new chat."
    )


async def _tripwire_qoder_unasked(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Stop Qoder the moment a call starts without its own permission ask.

    ``ask: ["*"]`` removes even Qoder's read-only exemption, so there is no
    safe-tool exception here. Pending frames are not execution, but they end
    any prior call's verdict. The FIRST executing frame consumes the answer
    for that one execution; later progress/terminal frames for the same
    execution are suppressed by the executed marker. A fresh ask starts a new
    verdict, but that request is not itself permission: only Crew's selected
    ALLOW answer is. A reused id can therefore never inherit an old answer.

    Restore makes one structural exception: Qoder 1.1.67 replays history,
    including terminal tool frames, before and after ``session/load`` returns.
    Those frames are not executions of THIS session, so every tool-frame
    branch -- including pending bookkeeping -- is ignored until the first
    live ``session/prompt`` request is sent.
    """
    if not self._is_qoder:
        return
    if not self._qoder_live_prompt_seen:
        return
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict):
        return
    if update.get("sessionUpdate") not in {UPDATE_TOOL_CALL, UPDATE_TOOL_CALL_UPDATE}:
        return
    tool_call_id = update.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return
    status = update.get("status")
    if status == "pending":
        # A pending frame can begin a new call that reuses an id. It does not
        # authorize that call; only a fresh permission frame can. Clear every
        # stale verdict too, including an ask left by a malformed stream that
        # never reached execution.
        self._qoder_executed_ids.discard(tool_call_id)
        self._qoder_denied_ids.discard(tool_call_id)
        self._qoder_asked_ids.discard(tool_call_id)
        self._qoder_approved_ids.discard(tool_call_id)
        return
    if status != "in_progress" and status not in TERMINAL_TOOL_STATUSES:
        return
    if status == "failed":
        # A rejected call normally ends failed and is safe; so does a call
        # Crew approved that then failed. A failed terminal with NEITHER
        # verdict still proves the gate was bypassed, because Qoder reached
        # the tool path without Crew answering its request. Do not leave an
        # executed marker: a later in_progress/completed frame with the same
        # id is a new call and must produce a fresh ask.
        already_executed = tool_call_id in self._qoder_executed_ids
        approved = tool_call_id in self._qoder_approved_ids
        denied = tool_call_id in self._qoder_denied_ids
        self._qoder_executed_ids.discard(tool_call_id)
        self._qoder_asked_ids.discard(tool_call_id)
        self._qoder_denied_ids.discard(tool_call_id)
        self._qoder_approved_ids.discard(tool_call_id)
        self._retire_qoder_request_tool(tool_call_id)
        if already_executed or approved or denied:
            return
        outcome, what = "ran_without_gate", "without asking Kiro Crew's gate"
        logger.error(
            "qoder gate: tool call %s failed %s; the session's tool calls are no "
            "longer governed by Kiro Crew's gate, so the harness is being stopped "
            "[session=%s]",
            tool_call_id,
            what,
            self._session_id,
        )
        await self._stop_qoder_unasked(tool_call_id, outcome, what)
        return
    if status in TERMINAL_TOOL_STATUSES:
        # Every terminal closes this execution. Clear the marker and verdicts
        # so a later reused id cannot hide behind the old call. The first
        # approved terminal returns; a duplicate terminal reads all-false,
        # falls through as an unasked execution, and stops the session.
        already_executed = tool_call_id in self._qoder_executed_ids
        self._qoder_executed_ids.discard(tool_call_id)
        approved = tool_call_id in self._qoder_approved_ids
        denied = tool_call_id in self._qoder_denied_ids
        self._qoder_asked_ids.discard(tool_call_id)
        self._qoder_denied_ids.discard(tool_call_id)
        self._qoder_approved_ids.discard(tool_call_id)
        self._retire_qoder_request_tool(tool_call_id)
        if already_executed or (approved and not denied):
            return
    else:
        # In-progress consumes the one-execution verdict immediately. A second
        # in-progress for the same execution is still suppressed by the marker.
        already_executed = tool_call_id in self._qoder_executed_ids
        approved = tool_call_id in self._qoder_approved_ids
        denied = tool_call_id in self._qoder_denied_ids
        self._qoder_executed_ids.add(tool_call_id)
        self._qoder_asked_ids.discard(tool_call_id)
        self._qoder_denied_ids.discard(tool_call_id)
        self._qoder_approved_ids.discard(tool_call_id)
        if already_executed or (approved and not denied):
            return
    if denied:
        outcome, what = "ran_despite_deny", "after Kiro Crew's gate DENIED it"
    elif not approved:
        outcome, what = "ran_without_gate", "without asking Kiro Crew's gate"
    logger.error(
        "qoder gate: tool call %s started %s; the session's tool calls are no "
        "longer governed by Kiro Crew's gate, so the harness is being stopped "
        "[session=%s]",
        tool_call_id,
        what,
        self._session_id,
    )
    await self._stop_qoder_unasked(tool_call_id, outcome, what)


async def _stop_qoder_unasked(self: AcpClient, tool_call_id: str, outcome: str, what: str) -> None:
    """Audit, kill and raise for one unasked or denied Qoder execution."""
    try:
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name=tool_call_id,
            tool_kind="other",
            outcome=outcome,
            metadata={"reason": "qoder_unasked_tripwire", "backend": self.backend},
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("qoder gate: audit of the unasked-call tripwire failed", exc_info=True)
    await self._kill_process(force=True)
    notice = (
        "Qoder started a tool that Kiro Crew had refused, so Kiro Crew stopped it. "
        "Start a new chat."
        if outcome == "ran_despite_deny"
        else "Qoder ran a tool without asking for approval, so Kiro Crew stopped it. Start a new chat."
    )
    raise AcpToolGateUnroutable(notice)


async def refresh_exclusive_catalog(
    backend: str, *, timeout: float | None = None
) -> list[dict[str, str]]:
    """Refresh one exclusive advertised catalog without a live session.

    The probe uses the backend's ordinary :class:`AcpClient` launch, so the
    adapter owns the pinned argv, environment scrub and credential mask. A
    throwaway workspace keeps project-owned settings out of the read; it is
    removed after the probe process is shut down on every return path. The
    answer is ingested only when non-empty, so a failed or timed-out probe can
    never replace (and therefore never widen) the last-known snapshot. This is
    best-effort by contract: callers serve the old snapshot and retry later.
    """
    from kiro_crew.acp.runtime import _ENTITLEMENT_PROBE_TIMEOUT

    if backend not in ACP_BACKENDS_ADVERTISED_MODEL_EXCLUSIVE:
        return []
    bound = _INIT_TIMEOUT + _ENTITLEMENT_PROBE_TIMEOUT if timeout is None else timeout
    client: AcpClient | None = None
    workspace = tempfile.TemporaryDirectory(prefix="kirocrew-qoder-catalog-")
    try:
        client = AcpClient(work_dir=workspace.name, model=None, acp_backend=backend)
        fresh, _answered_at = await asyncio.wait_for(
            client._entitlement_probe_answer(), timeout=bound
        )
        if not fresh:
            return []
        if model_registry.refresh_advertised_model_rows(
            model_registry_namespace(backend),
            fresh,
            observed_at=time.time_ns(),
            persist_stamp=True,
        ):
            await asyncio.to_thread(model_registry.persist_advertised_models)
        return list(fresh)
    except BaseException:
        logger.debug("exclusive advertised-model catalog refresh failed", exc_info=True)
        return []
    finally:
        if client is not None:
            try:
                await asyncio.shield(client.shutdown())
            except BaseException:
                logger.debug("exclusive advertised-model probe shutdown failed", exc_info=True)
        workspace.cleanup()
