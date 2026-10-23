"""The session's model.

The advertised model list (parsed from session responses, stored with the session
config and revalidated on the read path), and mode, model and effort selection through
the backend's own verbs, with the served-default backfill.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_available_models``, ``_available_models_captured_at``,
``_available_models_probe_confirmed``, ``_available_models_read_probe_at``,
``_config_options``, ``_kas_auto_fallback``, ``_mode_model_unjudged``, ``_model``,
``_read_refresh_task``, ``_resolved_model_id``, ``active_agent``, ``model_pin_partial``,
``model_pin_refused``.

Instance fields read: ``_runtime``, ``_session_id``, ``last_prompt_stats``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew import model_registry
from kiro_crew.acp._dispatch import set_mode_params, set_model_params
from kiro_crew.acp.client import (
    DEFAULT_MODEL,
    AcpClient,
    AcpError,
    AcpModelUnavailable,
    AcpProcessDied,
    AcpTimeoutError,
    _is_config_value_rejection,
    _push_model_via_effort_split,
    advertised_model_ids,
    catalog_row_would_drop,
    pick_served_default,
    resolve_usable_model,
)
from kiro_crew.acp.session_handle_runtime_contract import AcpRequestTimeout, AcpRuntimeError
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    MODEL_CONFIG_ID,
    effort_config_option_id,
)
from kiro_crew.agent_sdk.backends import effort_config_option_levels
from kiro_crew.agent_sdk.drivers.acp import EntitlementRevalidating
from kiro_crew.platform.context import redact_log_via_context

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


# ── Constants ──

# Read-path entitlement revalidation (see
# ``AcpSessionHandle.maybe_refresh_available_models``). These bound how eagerly
# the dashboard picker re-asks the backend what the account can run; they are a
# scheduling policy, never an entitlement decision.
#
# A session-init snapshot captured within this many seconds of the runtime's
# spawn fell inside the startup window where the degraded (free-tier default)
# answer is resolved, so it is treated as suspect and revalidated.
_READ_PATH_SPAWN_RACE_SECS = 90.0
# A session probes on the read path at most once per this interval, so a hot
# dashboard poll does not re-probe on every runtime-probe TTL expiry forever.
# The interval binds a probe-CONFIRMED snapshot and every non-auto-only
# snapshot; an UNCONFIRMED auto-only snapshot may re-probe (it always earns a
# probe), bounded by the runtime's own single-flight probe TTL — which now
# covers the failure/empty path too, so even a failing probe is re-asked at
# most once per that TTL, not on every read.
_READ_PATH_REPROBE_MIN_INTERVAL_SECS = 300.0
# The picker read path awaits the probe at most this long, then raises
# EntitlementRevalidating so the endpoint returns its degraded (503) response
# and the frontend keeps its last-good list and polls again; the shielded probe
# keeps running and the next read serves its landed result. Kept under the
# remote-hub cold-path budget (DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS in
# instances/constants.py: 5 + 10 + 3 < 20) so proxied /api/models never times
# out mid-revalidation.
_READ_PATH_PROBE_DEADLINE_SECS = 3.0

# How long AcpSessionHandle.set_model waits for a ``session/set_model`` reply.
# kiro-cli 2.28 answers in well under a second (measured: 0.03-0.04s), so this
# bounds only an adapter that never answers; that silence is read as accepted.
_SET_MODEL_REPLY_TIMEOUT = 3.0


def parse_advertised_models(resp: dict[str, Any]) -> list[dict[str, str]]:
    """The normalized advertised-model list from a ``session/new``/``session/load``
    response (``[]`` when the backend advertised nothing).

    Accepts both response shapes: a ``models`` object
    ``{availableModels: [...], currentModelId}`` and a bare list under either
    key. A falsy ``models`` value (``{}``, ``[]``, ``None``) falls through to a
    top-level ``availableModels`` — callers that gate on ``models`` themselves
    should pass a wrapped envelope (e.g. ``{"models": models}``) so the
    fallback cannot source the list from a payload their gate never saw. Normalization matches
    :meth:`AcpSessionHandle._normalize_models`, so a
    probe's answer and a session-init snapshot are directly comparable.
    """
    models = resp.get("models") or resp.get("availableModels")
    if isinstance(models, dict):
        avail = models.get("availableModels", [])
        if isinstance(avail, list):
            return _normalize_models(avail)
        return []
    if isinstance(models, list):
        return _normalize_models(models)
    return []


def _select_options(entries: object) -> list[dict[str, Any]]:
    """Flatten one level of provider GROUPS out of a select's option list.

    ACP lets a select group its options: an entry may carry no ``value`` of its own
    and hold its real choices in a nested ``options`` list instead. A filter that
    keeps only entries with a ``value`` therefore empties on a grouped select, and an
    empty list reads as "this harness advertised no models" -- which for a harness
    whose advertised list is the ONLY vocabulary its ``set_config_option`` accepts
    means no model can ever be offered or resolved. ONE level, deliberately: a group
    holding groups is not a shape any harness here serves, and recursing without
    bound would let a malformed payload spin. Both levels are narrowed against
    non-list wire data, so a malformed group is skipped rather than aborting the list.
    """
    flattened: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return flattened
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("value"):
            flattened.append(entry)
            continue
        nested = entry.get("options")
        if not isinstance(nested, list):
            continue
        flattened.extend(o for o in nested if isinstance(o, dict) and o.get("value"))
    return flattened


def models_from_config_options(resp: dict[str, Any], backend: str) -> dict[str, Any] | None:
    """A ``models`` envelope synthesized from a ``model`` select, or ``None``.

    Some hosts advertise no ``models`` object at all and put their model list in
    ``configOptions`` instead, as a ``select`` whose ``options`` carry the ids
    ``session/set_config_option`` accepts -- so a caller that reads only ``models``
    records nothing and the picker it feeds is empty. Gated on membership in
    ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` (harness-parity H6): the fold is
    only meaningful where the advertised list IS the vocabulary, and a host outside
    that set keeps whatever the static registry gave it.

    Authored once because both drivers need the same answer -- ``AcpClient`` on the
    per-session path and ``AcpSessionHandle`` on the shared-runtime one -- and a
    second copy is free to disagree about the select's shape.
    """
    if backend not in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION:
        return None
    return model_select_envelope(resp)


def model_select_envelope(resp: dict[str, Any]) -> dict[str, Any] | None:
    """The ``model`` select of a session response as a ``models`` envelope, ungated.

    The shape walk behind :func:`models_from_config_options`, without its
    membership gate, for a caller that asks the select one narrow question rather
    than adopting it as the session's advertised list.
    """
    for opt in resp.get("configOptions") or []:
        if not isinstance(opt, dict) or opt.get("id") != "model" or opt.get("type") != "select":
            continue
        options = _select_options(opt.get("options"))
        if not options:
            return None
        envelope: dict[str, Any] = {
            "availableModels": [
                {
                    "modelId": o["value"],
                    "name": o.get("name") or o["value"],
                    "description": o.get("description") or "",
                }
                for o in options
            ]
        }
        current = opt.get("currentValue")
        if isinstance(current, str) and current:
            envelope["currentModelId"] = current
        return envelope
    return None


def session_models_envelope(resp: dict[str, Any], backend: str) -> Any:
    """The ``models`` payload of a session response, with the select folded in.

    One home for "where does this host's model list live", so a reader cannot know
    about the ``models`` object and not about the ``configOptions`` select. Returns
    whatever shape the response carried when it carried one, the synthesized
    envelope when it did not and the host advertises a ``model`` select, and the
    original absent value when neither applies -- so a caller's own shape branches
    stay exactly as they were.
    """
    models = resp.get("models") or resp.get("availableModels")
    if models is None or models == {} or models == []:
        models = models_from_config_options(resp, backend) or models
    return models


def advertised_models_from_session(resp: dict[str, Any], backend: str) -> list[dict[str, str]]:
    """The normalized advertised-model list for a session response, either shape.

    What a caller wants when it needs the LIST and not the envelope -- the
    entitlement probe, which re-asks the question on a throwaway session. Reading
    ``parse_advertised_models`` alone answers ``[]`` for a host whose list is a
    ``configOptions`` select, and an empty probe result is contractually "no
    evidence", so the snapshot it exists to correct would never heal.
    """
    env = session_models_envelope(resp, backend)
    if isinstance(env, dict):
        return parse_advertised_models({"models": env})
    if isinstance(env, list):
        return parse_advertised_models({"availableModels": env})
    return []


# ── Session Configuration ──


async def set_mode(self: AcpSessionHandle, agent_name: str) -> None:
    """Activate an agent via session/set_mode.

    A stored skill-view name maps back to the agent it was built from first,
    so the projection sends that agent's CURRENT view, never the stored one.
    """
    from kiro_crew.acp.skill_projection import RetiredSkillView, resolve_source_agent

    try:
        agent_name = await resolve_source_agent(agent_name)
    except RetiredSkillView as exc:
        raise AcpRuntimeError(str(exc)) from exc
    # send_request only queues the request; it does not await a mode ACK.
    self.active_agent = ""
    take = getattr(self._runtime, "take_switched_model", None)
    if take is not None:
        # Only the agent/switched frame THIS activation sends may be judged.
        take(self._session_id)
    await self._runtime.send_request(
        METHOD_SET_MODE,
        set_mode_params(self._session_id, agent_name),
    )
    if take is not None:
        self._mode_model_unjudged = True


async def _served_default_after_set_mode(self: AcpSessionHandle) -> None:
    """Judge the model a mid-session :meth:`set_mode` put the session on.

    kiro-cli moves the session to the model the switched-to agent's spec
    pins and reports it only in ``_kiro.dev/agent/switched``, which the
    runtime's reader records for this session as it arrives. Runs before
    each turn while a switch is unjudged: a recorded model goes through
    :meth:`ensure_served_default` the way the start paths' activation does,
    so an unserved one is moved to a served model before the prompt and a
    served one is adopted with no extra write. Until the frame has arrived
    nothing is judged and the next turn looks again; a frame without a
    model records nothing, so it changes nothing. The frame is kiro-cli's
    own, so a host that never sends it never has a model to judge.
    """
    take = getattr(self._runtime, "take_switched_model", None)
    activated = take(self._session_id) if take is not None else ""
    if not activated:
        return
    self._mode_model_unjudged = False
    await self.ensure_served_default(activated_model=activated)


async def set_model(self: AcpSessionHandle, model_id: str) -> None:
    """Switch model via session/set_model.

    This is the shared-runtime SUBSTITUTE path (background one-liners, tips,
    contradiction sweep, and any caller that did not pre-guard an explicit
    user pick). ``resolve_usable_model`` maps the request to what the account
    can run: a served id is sent; a bare pin a pair-id harness serves only as
    the model half of its advertised ``<model>[<effort>]`` rows is sent as
    that bare id; ``"auto"`` is sent only when the backend
    advertises it; and anything else — ``"auto"`` on a partition that doesn't
    serve it, or an unentitled concrete id — resolves to ``""``,
    meaning **inherit the session's backend default** (the served model
    ``session/new`` assigned). So this path never puts an unserved model on
    the wire, exactly like the interactive ``_wire_model_id``
    reset-to-default. Explicit user picks raise instead, upstream in
    ``AcpSessionProvider.set_model`` / ``AcpClient.set_model``; on a harness in
    ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`` the adapter can still refuse a
    pick that passed those guards, and that refusal is recorded in
    ``model_pin_refused`` for the caller to read back
    (``chat_handlers._try_live_model_switch`` does).
    """
    # Each call describes only itself: a refusal recorded by an earlier call
    # must not survive a later one, or a read-after-call reports a pick that
    # landed (or inherited the default) as refused.
    self.model_pin_refused = ""
    # Backend-aware: on a ``<model>[<effort>]`` pair-id harness the advertised
    # list is the picker's vocabulary while the ``model`` config option's is
    # the BARE id, so a bare pin misses the list yet is exactly what the wire
    # takes. Without the backend this layer answers the provider's already
    # resolved pin with a SECOND withhold, and the pin is dropped after all.
    resolved = resolve_usable_model(
        model_id,
        self._advertised_model_ids(),
        backend=self._runtime.acp_backend,
    )
    if not resolved:
        # Inherit the backend default — nothing to send. For the ephemeral
        # _bg session the current model IS session/new's served default.
        return
    backend = self._runtime.acp_backend
    if backend in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION:
        # A harness whose adapter judges the model VALUE rather than only the
        # option: the spelling Crew stored may not be the spelling this build
        # serves, so the candidate ladder decides. Non-strict, because this
        # is the substitute path — an exhausted ladder means "stay on the
        # backend default", the same answer an unresolvable id gets above.
        applied = await self._push_model_config_option(resolved, strict=False)
        if not applied:
            self.model_pin_refused = resolved
            return
        # Record the spelling that actually went on the wire, not the one
        # asked for: the context meter looks the window up by this id, and a
        # fallback spelling can carry a different one.
        resolved = applied
    elif backend == ACP_BACKEND_KAS:
        # KAS implements no ``session/set_model``; the model is one of its
        # session config options instead. Same effect, different verb — so
        # the bookkeeping below is shared rather than duplicated.
        #
        # Deliberately NOT folded into the branch above, and KAS is
        # deliberately absent from that membership set: KAS advertises the
        # option and accepts the resolved id, so a ladder here would only add
        # retries after a failure that is already terminal, and it would turn
        # a refusal into a silent stay-on-default where today it raises.
        await self.set_config_option(MODEL_CONFIG_ID, resolved)
    else:
        # Waits for the adapter's answer so an ERROR reply is not dropped by
        # the pre-turn drain while the bookkeeping below records a model the
        # session is not serving. A refusal follows the contract of the
        # config-option branch above: the session stays on what it serves
        # and the refused id is recorded for the caller to read back, so the
        # dashboard answers the pick with a 4xx instead of a reset. Silence is
        # not a refusal: an adapter that never answers this request kept the
        # unawaited behaviour, so a timeout records the switch as before.
        req_id = await self._send_awaited(
            METHOD_SET_MODEL,
            set_model_params(self._session_id, resolved),
        )
        try:
            await self._wait_for_response(req_id, timeout=_SET_MODEL_REPLY_TIMEOUT)
        except AcpTimeoutError:
            logger.info(
                "session/set_model(%s) on %s got no reply within %.0fs; recording it",
                resolved,
                self._session_id,
                _SET_MODEL_REPLY_TIMEOUT,
            )
        except AcpProcessDied:
            raise
        except AcpError as exc:
            logger.warning(
                "session/set_model(%s) on %s was refused: %s",
                resolved,
                self._session_id,
                redact_log_via_context(str(exc)),
            )
            self.model_pin_refused = resolved
            return
    self._model = resolved
    self.model_pin_refused = ""
    if getattr(self, "_mode_model_unjudged", False):
        # The session runs this model now, not the one an earlier set_mode
        # activation put it on, so that activation's frame is not judged.
        self._mode_model_unjudged = False
        take = getattr(self._runtime, "take_switched_model", None)
        if take is not None:
            take(self._session_id)
    # Parity with AcpClient.set_model: keep _resolved_model_id in sync so
    # _backfill_context_window looks up the NEW model's window after a switch
    # (otherwise the context meter converts pct against the stale session/new
    # model until the next session refresh).
    self._resolved_model_id = resolved
    # Also rebase the meter stats themselves — the old model's window and
    # its authoritative usage_update do not describe this session
    # (mirrors AcpClient.set_model).
    win = (
        model_registry.model_window(resolved) if model_registry.has_known_window(resolved) else None
    )
    self.last_prompt_stats.rebase_to_window(win or 0)


async def _push_model_config_option(self: AcpSessionHandle, model_id: str, *, strict: bool) -> str:
    """Push the model over ``session/set_config_option``, trying each spelling.

    Returns the spelling that was accepted, or ``""`` when none was and the
    session must stay on the backend default.

    Three answers are possible when a write fails, and conflating any two of
    them breaks a session:

    * ``unknown config option`` — this adapter build has no model option at
      all, so no spelling can help. Strict re-raises; otherwise the caller
      stays on the default.
    * ``config option model`` in the message — the adapter named the option
      while refusing the VALUE, so the next spelling is worth trying.
    * a bare ``-32602`` — the request shape here is fixed and the value is
      the only thing that varies, so the code IS the refusal. Read as a
      protocol failure instead, it re-raises, the session init fails, and a
      model pin carried over from another harness kills every session at
      startup.

    Anything else is a transport or protocol fault and must keep propagating:
    swallowing it would report a model switch that never reached the process.

    ``strict=True`` (an explicit user pick) raises
    :class:`~kiro_crew.acp.client.AcpModelUnavailable` on exhaustion, because
    reporting success while running something else is worse than failing.
    ``strict=False`` (a substitute or inherited value) returns ``""``.
    """
    # Each push describes only itself; the split below sets it again.
    self.model_pin_partial = ""
    last_exc: AcpError | None = None
    # ONE home for the spelling ladder, imported rather than copied: the
    # order is a fact about how a model id is spelled on the wire, not about
    # which driver is asking, and two copies of an ORDER drift silently --
    # both still return candidates and only a cold cache on one driver shows
    # which list ran.
    for cand in AcpClient._model_config_candidates(model_id):
        try:
            await self.set_config_option(MODEL_CONFIG_ID, cand)
        except AcpError as exc:
            lowered = str(exc).lower()
            if "unknown config option" in lowered:
                if strict:
                    raise
                logger.debug(
                    "adapter exposes no %r config option; skipping model push",
                    MODEL_CONFIG_ID,
                )
                return ""
            if not _is_config_value_rejection(exc, MODEL_CONFIG_ID, self._runtime.acp_backend):
                raise
            last_exc = exc
            continue
        if cand != model_id:
            logger.info(
                "ACP model %r rejected by the adapter; applied fallback spelling %r",
                model_id,
                cand,
            )
        return cand
    # Every spelling refused as one value. A ``<model>[<effort>]`` pair --
    # the shape codex-acp advertises but its ``model`` option does not take --
    # is applied as its two halves instead (shared seam with AcpClient).
    split_applied = await _push_model_via_effort_split(self, self._runtime.acp_backend, model_id)
    if split_applied:
        return split_applied
    # Redacted through the platform context before the id reaches a log or an
    # exception message: it is caller-supplied text on a path that ends up in
    # front of a user, and this process can compose a companion redactor -- so
    # the baseline two-pass call would scan it with the weaker pass. Same call
    # this file already makes for the unserved-default warning, which is the
    # same shape of value going to the same kind of place.
    _rejected_log = redact_log_via_context(str(model_id))
    advertised_ids = self._advertised_model_ids()
    if strict:
        raise AcpModelUnavailable(
            _rejected_log,
            advertised_ids,
            backend=self._runtime.acp_backend,
            # Only a pair-id harness earns the adapter-mismatch wording: on
            # those the advertised list IS the entitlement, so refusing
            # something on it is the adapter contradicting itself. Elsewhere an
            # advertised id may simply be out of the account's reach, and the
            # entitlement wording plus the `whoami` hint is the true answer.
            advertised_but_refused=(
                model_id in advertised_ids
                and self._runtime.acp_backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
            ),
        ) from last_exc
    logger.warning(
        "ACP model %s rejected by the adapter; staying on the backend default %s "
        "(advertised: %s)",
        _rejected_log,
        self._resolved_model_id or DEFAULT_MODEL,
        ", ".join(advertised_ids) or "none",
    )
    return ""


async def set_config_option(self: AcpSessionHandle, config_id: str, value: str) -> None:
    """Set a session config option (e.g. effort level).

    Sends session/set_config_option JSON-RPC request. When the response
    carries the session's full ``configOptions`` (the ACP response shape),
    they replace the cached ones: a write can change which options exist --
    KAS serves its ``effortLevel`` select only once a concrete model is set --
    and the matching ``config_option_update`` notification sits buffered on
    the queue until the next turn drains it, so a follow-up write gated on
    :meth:`supports_config_option` would otherwise read the stale list.
    """
    req_id = await self._send_awaited(
        METHOD_SET_CONFIG_OPTION,
        {"sessionId": self._session_id, "configId": config_id, "value": value},
    )
    msg = await self._wait_for_response(req_id, timeout=10.0)
    result = getattr(msg, "result", None)
    config_options = result.get("configOptions") if isinstance(result, dict) else None
    if isinstance(config_options, list):
        self._config_options = config_options
        self._sync_effort_levels()


def _advertised_model_ids(self: AcpSessionHandle) -> list[str]:
    """Advertised model ids, for the model-rejection error path.

    Parity with ``AcpClient._advertised_model_ids``. Empty when the backend
    advertised nothing (no session yet, or a backend that omits ``models``),
    which the error path reads as "entitlement unknown" and leaves the
    transient/capacity handling alone.

    This handle is the shared-runtime path every dashboard chat takes, so
    without it the entitlement discrimination in ``_model_is_unentitled``
    would only ever fire for direct-spawn ``AcpClient`` sessions.
    """
    ids = []
    for entry in self._available_models:
        model_id = entry.get("modelId") if isinstance(entry, dict) else None
        if isinstance(model_id, str) and model_id.strip():
            ids.append(model_id)
    return ids


def supports_config_option(self: AcpSessionHandle, config_id: str) -> bool:
    """Whether the session advertised a config option with this id.

    Returns True when no config options were reported yet (lazy backend).
    """
    if not self._config_options:
        return True
    return any(isinstance(opt, dict) and opt.get("id") == config_id for opt in self._config_options)


def get_valid_effort_levels(self: AcpSessionHandle) -> list[str]:
    """Return valid effort levels from config options, preserving order.

    The option id is resolved per backend (``effort`` for most,
    ``reasoning_effort`` for codex-acp): a hard-coded spelling returns an
    empty list on a backend that spells it differently, which every caller
    reads as "this model has no effort levels".
    """
    effort_option = effort_config_option_id(self._runtime.acp_backend)
    for opt in self._config_options:
        if not isinstance(opt, dict):
            continue
        if opt.get("id") == effort_option:
            options = opt.get("options", [])
            if isinstance(options, list):
                return effort_config_option_levels(
                    self._runtime.acp_backend,
                    [o.get("value", "") for o in options if isinstance(o, dict) and o.get("value")],
                )
    return []


def _crew_agent_name(self: AcpSessionHandle, name: str) -> str:
    """*name* as Crew records it: the runtime's launch copy reads as its own agent."""
    crew_name = getattr(self._runtime, "crew_agent_name", None)
    if not callable(crew_name):
        return name
    mapped = crew_name(name)
    return mapped if isinstance(mapped, str) else name


def store_session_config(self: AcpSessionHandle, resp: dict[str, Any]) -> None:
    """Extract configOptions and available models from session/new or session/load response.

    Called after create_session() or load() to populate state.
    """
    modes = resp.get("modes")
    current_agent = modes.get("currentModeId") if isinstance(modes, dict) else None
    self.active_agent = (
        self._crew_agent_name(current_agent) if isinstance(current_agent, str) else ""
    )
    config_options = resp.get("configOptions")
    if isinstance(config_options, list):
        self._config_options = config_options
        self._sync_effort_levels()
        # Read the whole select once and keep only its answer, so a long list
        # is never truncated and never stored. Only the KAS arm of
        # ``ensure_served_default`` reads it.
        select = model_select_envelope(resp) or {}
        ids = [
            m["modelId"]
            for m in select.get("availableModels", [])
            if isinstance(m.get("modelId"), str)
        ]
        self._kas_auto_fallback = (
            pick_served_default("auto", ids) if select.get("currentModelId") == "auto" else ""
        )
    # Where this host's model list lives is asked in ONE place
    # (``session_models_envelope``): a host in
    # ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` advertises no ``models`` object
    # and puts the list in a ``configOptions`` ``model`` select, and a reader that
    # knows about one shape and not the other is how the entitlement probe came to
    # answer ``[]`` for codex while this path answered correctly. Absent stays
    # absent, so the shape branches below are untaken exactly as before.
    models = session_models_envelope(resp, self._runtime.acp_backend)
    if isinstance(models, dict):
        # Record the resolved model id (kiro-cli's currentModelId) so
        # _backfill_context_window can look up the window on pct-only
        # metadata even when the user never explicitly switched models.
        current_model_id = models.get("currentModelId")
        if isinstance(current_model_id, str) and current_model_id:
            self._resolved_model_id = current_model_id
        avail = models.get("availableModels", [])
        if isinstance(avail, list):
            # The shape walk is delegated to the canonical parser so this
            # snapshot and a probe's answer stay directly comparable.
            # The envelope is the same checked-binding discipline
            # as the client's, though here the parser's dict-or-list
            # fallback is unreachable by construction (the inner dict
            # always has a key). The isinstance check above is the
            # assignment gate: a non-list ``availableModels`` must not
            # clobber an already-stored list. A well-formed EMPTY list
            # still overwrites — that asymmetry with
            # ``AcpClient._capture_available_models`` (non-empty guard) is
            # deliberate, not an oversight.
            self._available_models = parse_advertised_models({"models": {"availableModels": avail}})
            self._mark_available_models_captured()
        # A backend may advertise its model list without echoing
        # ``currentModelId`` (it is best-effort in the ACP shape). When it
        # names exactly one model that IS the served model unambiguously, so
        # adopt it as the resolved id — otherwise ``served_model`` reports
        # ``""`` for the whole session on an unpinned run (no ``set_model``,
        # no ``currentModelId``), which is why the panel's model chip stays
        # blank until completion fills it from a different source. With two
        # or more advertised and no ``currentModelId`` the served choice is
        # genuinely unknown, so leave it empty rather than guess.
        if not self._resolved_model_id and len(self._available_models) == 1:
            self._resolved_model_id = self._available_models[0]["modelId"]
    elif isinstance(models, list):
        self._available_models = parse_advertised_models({"availableModels": models})
        self._mark_available_models_captured()


async def ensure_served_default(self: AcpSessionHandle, *, activated_model: str = "") -> None:
    """Move an inheriting pooled session off a backend default it cannot run.

    The pooled twin of ``AcpClient._ensure_served_default``.
    ``store_session_config`` records ``session/new``'s ``currentModelId``
    as the session's resolved model without judging it against the list the
    same response advertised. A partition does not have to serve the model
    its backend defaults to — an account whose region omits ``"auto"`` can
    be handed ``"auto"`` at birth — and then every prompt on this session
    dies with "your account does not have access to model 'auto'".

    The kiro backend judges any current id: its advertised ids are exactly
    the ids ``session/set_model`` accepts, so "absent from the advertised
    list" genuinely means unusable there. KAS judges only an ``auto``
    default, against its ``model`` select (see the branch below).

    Routed through :meth:`set_model` rather than a second wire call, so the
    KAS-vs-``session/set_model`` verb choice and the window/meter rebase
    stay in one place. On kiro the id handed to it is an advertised one; on
    KAS it comes from the host's own select and passes because KAS
    advertises no list. Either way ``resolve_usable_model`` lets it through.

    ``_model`` is restored afterwards. That field is the session's INTENT
    (``""``/``"auto"`` mean "inherit"), and it is what the warm-pool
    re-apply and the slot backfill read: left as the fallback id, a fresh
    pooled session would be pinned to whichever model happened to be first
    on the list, and an unpinned slot would stop following the default.
    Only ``_resolved_model_id`` — what the session actually runs — changes.

    ``activated_model`` is the model a ``session/set_mode`` just applied.
    kiro-cli switches the session to the model the activated agent's spec
    pins, replacing whatever it ran before — including the served model an
    earlier call here moved it to. The id recorded from ``session/new`` is
    stale then, so the caller passes the model kiro-cli reported for the
    activation and it is judged instead.
    """
    if self._runtime.acp_backend == ACP_BACKEND_KIRO:
        if activated_model.strip():
            self._resolved_model_id = activated_model.strip()
        unserved = self._resolved_model_id or ""
        fallback = pick_served_default(unserved, self._advertised_model_ids())
        if not fallback:
            return
        _unserved_log = redact_log_via_context(str(unserved))
        logger.warning(
            "ACP backend default %s is not in this account's served list (advertised: %s); "
            "switching session %s to %s",
            _unserved_log,
            ", ".join(self._advertised_model_ids()),
            self._session_id,
            fallback,
        )
        intent = self._model
        try:
            await self.set_model(fallback)
        finally:
            self._model = intent
    elif self._runtime.acp_backend == ACP_BACKEND_KAS:
        # KAS defaults a new session to ``auto`` and lists its models only in
        # the ``model`` select. That list answers ONE question here: is the
        # ``auto`` this session sits on missing from it? A listed id is served,
        # so moving to one is safe even if the list is incomplete. A concrete
        # current model is never judged against it, since a partial list would
        # move a session off a model the account can run.
        fallback = self._kas_auto_fallback
        if not fallback:
            return
        logger.warning(
            "KAS session %s defaults to auto, which its model select does not list; "
            "switching to %s",
            self._session_id,
            fallback,
        )
        intent = self._model
        try:
            await self.set_model(fallback)
        except (AcpError, AcpRequestTimeout) as exc:
            # session/new already succeeded; a refused switch must not fail
            # the session start. Stay on the backend default, the substitute
            # path's own contract.
            logger.warning(
                "KAS session %s could not switch off auto: %s",
                self._session_id,
                redact_log_via_context(str(exc)),
            )
        finally:
            self._model = intent


def _normalize_models(advertised: list[Any]) -> list[dict[str, str]]:
    """Normalize advertised models to ``{modelId, name, description}`` with
    guaranteed keys — the canonical normalization. Every consumer
    (``parse_advertised_models``, and through it ``store_session_config``,
    ``AcpClient._capture_available_models``, and the pooled-runtime
    entitlement probe ``AcpRuntime.probe_advertised_models``) shares this
    shape, so probe answers and session-init snapshots are directly
    comparable and the dashboard model dropdown gets a stable shape
    regardless of backend."""
    captured: list[dict[str, str]] = []
    for m in advertised:
        if not isinstance(m, dict):
            continue
        model_id = m.get("modelId") or m.get("value") or ""
        if not model_id:
            continue
        captured.append(
            {
                "modelId": str(model_id),
                "name": str(m.get("name") or model_id),
                "description": str(m.get("description") or ""),
            }
        )
    return captured


async def refresh_available_models(
    self: AcpSessionHandle, *, force: bool = False
) -> list[dict[str, str]]:
    """Re-resolve the advertised-model snapshot against the live backend.

    ``_available_models`` is otherwise written once, from this session's own
    ``session/new`` — an answer the backend resolved from the account state
    it held at that instant. When that answer was degraded (a lookup racing
    a token refresh answers with the default tier), the session refuses
    models the account actually has, for its whole life. Every consumer of
    entitlement — the explicit-pick refusal, the prompt-time pin withhold,
    the dashboard picker filter — reads through this handle's snapshot, so
    one refresh heals them all.

    The snapshot is replaced only by a NON-EMPTY probe result: a failed or
    empty probe is not evidence about entitlement, so the prior snapshot is
    kept. Returns the probe result either way, so callers can distinguish
    "revalidated" from "could not revalidate".

    ``force`` is forwarded to the runtime probe: a user action (the
    explicit-pick refusal heal, the spawn-time pin withhold) passes
    ``force=True`` so it earns a fresh probe instead of being refused on a
    recent no-evidence failure replay. The read path leaves it False.
    """
    # The floor is this snapshot's capture time, so a non-empty return is
    # always at least as new as the snapshot it replaces: a broader answer
    # cached on the shared runtime before this session captured a narrower
    # one is never replayed over it. The stored snapshot is then dated by the
    # ANSWER's own clock (the runtime's result clock, which is the arrival
    # time of a fresh answer and the original arrival time of a replayed
    # one), never by this call's time: dating a replay by the call would
    # raise this handle's floor above the data it holds, so its next refresh
    # within the TTL would open a real session/new instead of replaying, and
    # a replay captured inside the spawn-race window would be re-dated past
    # it and marked confirmed, disabling the read-path heal for this handle.
    asked_at = time.monotonic()
    fresh = await self._runtime.probe_advertised_models(
        force=force, not_before=self._available_models_captured_at
    )
    if fresh:
        served_at = self._runtime.entitlement_probe_result_at
        self._available_models = list(fresh)
        # A runtime that answered without stamping its result clock (a probe
        # seam that bypasses the store) is dated by the call instead, which
        # is still never later than the answer.
        self._available_models_captured_at = served_at if served_at > 0.0 else asked_at
        self._available_models_probe_confirmed = True
    return fresh


def _mark_available_models_captured(self: AcpSessionHandle) -> None:
    """Stamp the session-init snapshot's capture time (an unconfirmed
    answer). A snapshot written from ``session/new`` is NOT probe-confirmed:
    only :meth:`refresh_available_models` sets that flag, because only a live
    re-probe proves the answer is not the startup-race default."""
    self._available_models_captured_at = time.monotonic()
    self._available_models_probe_confirmed = False


async def maybe_refresh_available_models(
    self: AcpSessionHandle, catalog_ids: list[str]
) -> list[dict[str, str]]:
    """Revalidate the snapshot on the READ path when it would narrow the catalog.

    The dashboard picker filter narrows the ``--list-models`` catalog through
    the newest live session's ``availableModels`` snapshot. When that snapshot
    is the startup-race default (an entitlement lookup racing a token refresh
    answered with the free tier), the picker hides models the account has and
    ``auto`` chats silently inherit the degraded default — and because no
    explicit pick is ever refused, the refresh-before-refuse path never fires.
    This is the read-path counterpart: revalidate the snapshot BEFORE the
    picker trusts it to hide anything.

    ``catalog_ids`` is the full ``--list-models`` catalog (the ids the picker
    would offer unfiltered). The verdict of what to keep/drop is NOT decided
    here — it is :func:`catalog_row_would_drop`, the same per-row verdict the
    picker endpoint applies, built on ``model_is_unusable`` (the single
    spelling of "what this account can run") and ``resolve_pin_spelling``.
    This method only decides WHETHER
    the snapshot is trustworthy enough to narrow with, and reuses the existing
    :meth:`refresh_available_models` heal path when it is not (no second
    probe, no second parser).

    Staleness heuristic (a scheduling decision, never an entitlement one):
    probe only when the snapshot would actually narrow the catalog (some row
    drops and the endpoint does not fail open to the full catalog) AND one of

    * it was never probe-confirmed, or
    * it was captured within ``_READ_PATH_SPAWN_RACE_SECS`` of runtime spawn
      (the exact window the degraded answer is resolved in), or
    * it advertises only ``auto`` against a richer catalog — the strongest
      staleness signal.

    Rate limit: a probe is skipped when this session probed on the read path
    within ``_READ_PATH_REPROBE_MIN_INTERVAL_SECS`` AND the snapshot is either
    probe-confirmed or not auto-only, so a hot poll does not re-probe on every
    runtime TTL expiry forever. An UNCONFIRMED auto-only snapshot is exempt
    from the interval — it always gets to probe — while a probe-CONFIRMED
    auto-only snapshot (a genuine free-tier account really is ``auto``-only)
    honours the interval like any other rather than re-probing forever. The
    runtime's own single-flight probe TTL bounds the cost of the exempt case.

    Fast path: the probe runs as a single in-flight task per handle, shielded
    under ``_READ_PATH_PROBE_DEADLINE_SECS``. On deadline expiry this RAISES
    :class:`EntitlementRevalidating` while the task KEEPS RUNNING to
    completion — so its throwaway probe session is cleaned up and a later
    read serves the corrected list, and the endpoint returns its degraded
    response (rather than serving the un-revalidated snapshot as a live 200
    the frontend caches). A subsequent read while the same task is still in
    flight awaits it too, so it never bypasses the raise. A probe FAILURE
    (as opposed to a timeout) NEVER makes the picker worse: the current
    snapshot is returned unchanged (fail open).
    """
    snapshot = list(self._available_models)
    advertised = advertised_model_ids(snapshot)
    if not advertised:
        # No live list to narrow with — nothing to revalidate, fail open.
        return snapshot
    # Count only rows the picker would actually hide AND a fresher snapshot
    # could restore, using the endpoint's own per-row verdict
    # (``catalog_row_would_drop``): ``auto`` is always kept, an advertised or
    # ``ns::``-foldable row is kept, and an empty id drops against every
    # snapshot, so none of those can justify a probe.
    dropped = [
        cid for cid in catalog_ids if cid.strip() and catalog_row_would_drop(cid, advertised)
    ]
    survivors = [
        cid
        for cid in catalog_ids
        if cid.strip()
        and cid.strip().lower() not in ("auto", "default")
        and not catalog_row_would_drop(cid, advertised)
    ]
    advertises_auto = any(a.strip().lower() in ("auto", "default") for a in advertised)
    # The endpoint FAILS OPEN — serves the whole catalog unfiltered — when the
    # snapshot does not advertise ``auto`` and no non-``auto`` row survives
    # (a namespace mismatch rather than an entitlement answer). A snapshot in
    # that state hides nothing, so it is not narrowing either.
    fails_open = not advertises_auto and not survivors
    would_narrow = bool(dropped) and not fails_open
    if not would_narrow:
        # The snapshot keeps the whole catalog, so a stale snapshot cannot
        # currently hide anything — do not spend a probe.
        return snapshot
    auto_only = len(advertised) == 1 and advertised[0].strip().lower() == "auto"
    now = time.monotonic()
    spawn_at = self._runtime.spawn_monotonic
    within_spawn_race = (
        spawn_at is not None
        and self._available_models_captured_at > 0.0
        and (self._available_models_captured_at - spawn_at) <= _READ_PATH_SPAWN_RACE_SECS
    )
    suspect = not self._available_models_probe_confirmed or within_spawn_race or auto_only
    if not suspect:
        return snapshot
    # An in-flight probe from an earlier read (its deadline expired but the
    # shielded task kept running) MUST be awaited, not bypassed: on the
    # frontend's degraded re-poll the interval gate below would otherwise
    # return the un-revalidated snapshot as a normal answer, the endpoint
    # would serve it as a live 200, and the corrected list this very probe is
    # fetching would never reach the picker. So when a task is still running,
    # skip the interval gate and fall through to await it — the poll gets the
    # landed result or raises EntitlementRevalidating again.
    task = self._read_refresh_task
    in_flight = task is not None and not task.done()
    if not in_flight:
        recently_probed = (
            self._available_models_read_probe_at > 0.0
            and (now - self._available_models_read_probe_at) < _READ_PATH_REPROBE_MIN_INTERVAL_SECS
        )
        # The interval applies to a probe-CONFIRMED snapshot and to every
        # non-auto-only snapshot: a genuine free-tier account is legitimately
        # auto-only, so once confirmed it must not re-probe on every poll. An
        # UNCONFIRMED auto-only snapshot is exempt from the interval — it
        # always gets to probe (its docstring promise), and the runtime's own
        # single-flight probe TTL still bounds the cost of a burst.
        if recently_probed and (self._available_models_probe_confirmed or not auto_only):
            return snapshot
        self._available_models_read_probe_at = now
        task = asyncio.ensure_future(self.refresh_available_models())
        self._read_refresh_task = task
    # Non-None in both branches: in-flight reused an existing task, else one
    # was just started above.
    assert task is not None
    try:
        # Shield so a timeout leaves the task RUNNING (it finishes the probe
        # and cleans up its throwaway session); we just stop waiting on it.
        await asyncio.wait_for(asyncio.shield(task), timeout=_READ_PATH_PROBE_DEADLINE_SECS)
    except (TimeoutError, asyncio.TimeoutError):
        # The probe did not land inside the deadline and is STILL RUNNING.
        # We must not return the un-revalidated snapshot as a normal answer:
        # the picker endpoint serves that as a live HTTP 200 that the
        # frontend caches with no refetch, so the corrected list this probe
        # is fetching would never be served. Signal "revalidation in flight"
        # so the endpoint returns its degraded response and the frontend
        # keeps its last-good list and polls again; the next read (once the
        # task has landed) serves the corrected list.
        raise EntitlementRevalidating from None
    except Exception:
        # Fail open exactly as today: no evidence never worsens the picker.
        logger.debug("read-path entitlement revalidation failed", exc_info=True)
        return list(self._available_models)
    # refresh_available_models already replaced the snapshot in place on a
    # non-empty probe and kept it on an empty one; either way the live
    # snapshot is the answer to narrow with.
    return list(self._available_models)


def _sync_effort_levels(self: AcpSessionHandle) -> None:
    """Push ACP-reported effort levels to the global validation set (parity
    with AcpClient._sync_effort_levels). Without this, the unified kiro path
    never refreshes the reasoning-effort allow-list. A backend in
    ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`` advertises a per-session
    list: it joins the validation set but never replaces the global fallback
    order other slots are served. Function-level import avoids the
    chat_persistence -> dashboard -> session -> acp import cycle."""
    levels = self.get_valid_effort_levels()
    if not levels:
        return
    if self._runtime.acp_backend in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION:
        # circular import: chat_persistence -> dashboard -> session -> acp
        from kiro_crew.dashboard.chat_persistence import register_reasoning_effort_values

        register_reasoning_effort_values(levels)
        return
    # circular import: chat_persistence -> dashboard -> session -> acp
    from kiro_crew.dashboard.chat_persistence import update_reasoning_effort_values

    update_reasoning_effort_values(levels)
