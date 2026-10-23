"""Select, verify and apply an ``AcpClient`` session's model.

The advertised model list and its entitlement re-probe, the served-default correction,
judging an agent switch, ``set_model``, the config-option push ladder and the effort
levels.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_acp_config_options``, ``_activated_model``,
``_advertised_models_changed``, ``_available_mode_ids``, ``_available_models``,
``_available_models_captured_at``, ``_available_models_probe_confirmed``,
``_entitlement_probe_inflight``, ``_last_substitution_model``, ``_mode_frame_awaited``,
``_mode_frame_superseded``, ``_model``, ``_modes_advertised``, ``_resolved_model_id``,
``model_pin_partial``, ``model_pin_refused``.

Instance fields read: ``_acp_backend``, ``_agent``, ``_channel_id``,
``_derived_spec_snapshot``, ``_extra_env``, ``_mcp_gateway_overlay``,
``_mcp_gateway_socket``, ``_permission_mode``, ``_sandbox_mode``, ``_session_id``,
``_session_key``, ``_work_dir``, ``last_prompt_stats``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew import model_registry, model_scope
from kiro_crew.acp import runtime_models
from kiro_crew.acp._dispatch import build_session_new_params, parse_session_modes
from kiro_crew.acp.client_timeouts import _INIT_TIMEOUT
from kiro_crew.acp.runtime_models import DEFAULT_MODEL, model_is_unusable, pick_served_default
from kiro_crew.acp.transport_errors import AcpError, AcpModelUnavailable, AcpProcessDied
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    METHOD_INITIALIZE,
    METHOD_SESSION_NEW,
    METHOD_SET_MODEL,
    MODEL_CONFIG_ID,
    JsonRpcMessage,
    effort_config_option_id,
    effort_config_option_value,
)
from kiro_crew.agent import require_unchanged_derived_spec
from kiro_crew.agent_sdk.backends import effort_config_option_levels, model_refusal_phrase
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


#: JSON-RPC ``Invalid params``. On ``session/set_config_option`` the request shape
#: is fixed and the VALUE is the only param a caller varies, so this code means the
#: adapter refused the value it was handed — the frame a stale codex model pin
#: draws, carrying no ``data`` to match on.
_JSONRPC_INVALID_PARAMS = -32602


def _is_config_value_rejection(exc: AcpError, config_id: str, backend: str = "") -> bool:
    """Whether *exc* is the adapter refusing a config option VALUE.

    Three shapes count. claude-agent-acp names the option in its message
    (``Invalid value for config option <id>: ...``); codex-acp answers with a
    bare JSON-RPC ``-32602`` and no detail -- the request shape is fixed, so the
    code is the verdict on the value; and a harness that declares its own model
    refusal text (``agent_sdk.backends.model_refusal_phrase``) is read by that
    text, for the ``model`` option of that *backend* only. ``unknown config
    option`` is NOT a value rejection (the option itself is missing) and is left
    to the caller.

    The bare-code half rests on "the request shape is fixed, so only the value can
    be invalid", which is a per-adapter fact and not a protocol guarantee. A
    harness joining ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION`` or
    ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`` must therefore have its own -32602
    semantics checked before it is added: an adapter that also answers -32602 for a
    genuinely malformed request would have that read here as a value refusal and
    silently descend the effort ladder instead of surfacing the fault. Revisit the
    classifier -- do not widen it -- when a member does not fit.
    """
    lowered = str(exc).lower()
    phrase = model_refusal_phrase(backend) if config_id == MODEL_CONFIG_ID else ""
    return (
        f"config option {config_id}" in lowered
        or getattr(exc, "code", None) == _JSONRPC_INVALID_PARAMS
        or (bool(phrase) and phrase.lower() in lowered)
    )


async def _push_model_via_effort_split(driver: Any, backend: str, model_id: str) -> str:
    """Apply a ``<model>[<effort>]`` id as two config-option writes.

    Gated on ``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS`` membership (harness-parity
    H13): only a harness that ADVERTISES pair ids takes the split. For any other
    backend a refused bracketed id stays refused, exactly as before this seam.

    codex-acp keeps two spellings of one selection. Its ``models.availableModels``
    (what ``_capture_available_models`` advertises to the picker) is one entry per
    model x reasoning effort, ``gpt-6-astra[max]``. Its ``model`` config option --
    the channel ``set_model`` switches on -- accepts only the bare ``gpt-6-astra``
    and refuses the pair with a bare ``-32602``; the effort travels down the
    separate ``reasoning_effort`` option. Before this seam existed, every pick
    from the advertised list was refused and surfaced as "not available on your
    account", with the very list it was picked from quoted as proof.

    Shared by ``AcpClient`` and ``AcpSessionHandle``: *driver* supplies
    ``_push_model_config_option`` (the spelling ladder, run non-strict on the
    bare half), ``supports_config_option`` and ``set_config_option``.

    Returns the spelling to record: *model_id* itself when both halves landed
    (so the picker highlights the advertised row), the bare model when it landed
    but the effort was refused or unadvertised (the adapter then chose its own
    effort, which this id does not overclaim), and ``""`` when the model half was
    refused too -- the caller's exhaustion path then decides. Ids without an
    effort suffix (and ``[1m]`` window ids) return ``""`` without a write.

    The bare-model outcome is also recorded as ``driver.model_pin_partial``: the
    pin did not fully apply, but its base model runs, so a caller billing by the
    pin bills that base instead of the suffixed id.
    """
    if backend not in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS:
        return ""
    base, effort = model_registry.split_effort_suffix(model_id)
    if not effort:
        return ""
    applied_base = await driver._push_model_config_option(base, strict=False)
    if not applied_base:
        return ""
    # Through the platform context, not the baseline pair: these are log lines on a
    # process that can compose a companion redactor, so the baseline pass would be
    # the weaker scan (the gate-side census in test_security_posture pins this).
    _model_log = redact_log_via_context(str(model_id))
    _base_log = redact_log_via_context(str(applied_base))
    # The effort is a SLICE of the same caller-supplied id, so it carries whatever
    # the id carried: redacting the whole and then logging the half raw puts the
    # bracketed text straight back in the log and defeats the two lines above.
    _effort_log = redact_log_via_context(str(effort))
    # The same resolver every other effort site reads, so the split and the slot
    # override write ONE option: two spellings on one backend leave the override
    # writing an id the adapter does not know, which it reports as "no effort
    # selector" and skips -- the session then runs the suffix's effort while the
    # UI reports the slot's.
    effort_option = effort_config_option_id(backend)
    # ...and the same resolver for the VALUE, so the split writes the level in the
    # harness's own vocabulary rather than the suffix's verbatim.
    effort = effort_config_option_value(backend, effort)
    if not driver.supports_config_option(effort_option):
        logger.warning(
            "ACP model %s applied as %s; adapter exposes no %r option, effort %s not applied",
            _model_log,
            _base_log,
            effort_option,
            _effort_log,
        )
        driver.model_pin_partial = applied_base
        return applied_base
    try:
        await driver.set_config_option(effort_option, effort)
    except AcpError as exc:
        if "unknown config option" not in str(exc).lower() and not _is_config_value_rejection(
            exc, effort_option
        ):
            raise  # transport/protocol failure -- the model DID switch, but do not hide this
        logger.warning(
            "ACP model %s applied as %s; adapter refused effort %s, keeping its default",
            _model_log,
            _base_log,
            _effort_log,
        )
        driver.model_pin_partial = applied_base
        return applied_base
    return model_id


async def set_model(self: AcpClient, model_id: str) -> None:
    """Switch model on a running session (used by warm pool post-claim)."""
    if not self._session_id:
        raise AcpError("Cannot set model before session is initialized")
    # This path deliberately does not scope: an explicit pick must reach the
    # adapter because its advertised list can omit an entitlement the adapter
    # accepts, and inherited pins are already scoped by their callers.
    #
    # A refusal is never issued on the session-init snapshot alone -- the
    # same refresh-before-refuse AcpSessionProvider.set_model applies on the
    # shared runtime. That snapshot is one unconfirmed answer captured inside
    # the startup race, so a would-be refusal first revalidates against a
    # fresh backend answer and stands only if that answer ALSO lacks the
    # model. A failed probe keeps the snapshot's verdict (no evidence, no
    # entitlement granted).
    if self._is_kiro and self._model_is_unusable(model_id):
        advertised = self._advertised_model_ids()
        fresh = runtime_models.advertised_model_ids(await self.refresh_available_models())
        if model_is_unusable(model_id, fresh or advertised):
            _rejected_log, _ = redact_exfiltration_urls(str(model_id))
            _rejected_log, _ = redact_credentials(_rejected_log)
            raise AcpModelUnavailable(_rejected_log, fresh or advertised, backend=self.backend)
    if self._uses_advertised_model_selection:
        # Mirror the spawn path (_spawn): fold the requested id onto the exact
        # spelling the backend advertised, so a warm-pool claim that switches
        # model sends the versioned [1m] id claude-agent-acp serves at 1M — not
        # a bare/base spelling that resolves to the 200K window. Without this
        # the fold happened only at spawn, so a pooled runtime claimed for a
        # 4.8 chat could send the base id and silently serve 200K. Capability-
        # gated + namespace-keyed so any future member gets the same fold.
        model_id = model_registry.resolve_wire_model_id(model_id, self._model_registry_namespace)
    # An advisory belongs to the request that emitted it.  A clean explicit
    # switch must not inherit the served-model attribution from startup.
    self._last_substitution_model = None
    if self.backend in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION:
        model_id = await self._push_model_config_option(model_id, strict=True)
    else:
        await self._send_request(
            METHOD_SET_MODEL,
            {"sessionId": self._session_id, "modelId": model_id},
        )
    self._model = model_id
    self.model_pin_refused = ""
    self._resolved_model_id = self._last_substitution_model or model_id
    if self._seeds_local_settings:
        # Re-seed the per-session settings file: a pooled runtime seeded it at
        # spawn with the POOL DEFAULT model + its allowlist, and the spawn-only
        # seed left that stale file in place across the claim — so the allowlist
        # and model key could still describe the pool default (and collapse a
        # 4.8 pick to 200K). Overwrites only the file Crew authored (ownership
        # guards inside), refreshing both to the just-claimed selection.
        # Capability-gated (not ``_is_claude``) so a future settings-seeding
        # adapter re-seeds on a warm claim by joining the set.
        try:
            await asyncio.to_thread(self._write_claude_local_settings)
        except (OSError, ValueError, TypeError):
            logger.warning("re-seed of settings.local.json on set_model failed", exc_info=True)
    # The previous model's window (and its authoritative usage_update, if
    # any) do not describe this session's new model — rebase the meter stats to the
    # new model so the context meter updates without waiting for the next
    # turn's telemetry (and so _backfill_context_window is un-gated).
    # Keyed on the SERVED id, not the requested one: a config-option
    # advisory can substitute the model this switch actually got, and the
    # meter describes what is serving. Rebasing on model_id showed the
    # requested model's window against the substitute's token counts, so
    # the percentage was wrong for exactly the sessions that were
    # silently moved.
    served_model_id = self._resolved_model_id or model_id
    win = (
        model_registry.model_window(served_model_id)
        if model_registry.has_known_window(served_model_id)
        else None
    )
    self.last_prompt_stats.rebase_to_window(win or 0)


def _capture_available_models(self: AcpClient, session_resp: dict) -> None:
    """Record the model list the backend advertised in a session response.

    The ACP ``session/new`` / ``session/load`` response carries a
    ``models`` object ``{availableModels: [{modelId, name, description}],
    currentModelId}``. We keep the list so the dashboard dropdown shows the
    real backend models (e.g. the versioned Claude list from
    claude-agent-acp) instead of a hardcoded guess. Best-effort and never
    raises — a backend that omits ``models`` simply leaves the list empty.

    The shape walk is delegated to
    :func:`kiro_crew.acp.session_handle.parse_advertised_models` so this
    snapshot stays directly comparable with the pooled-runtime probe.
    This path keeps its dict-only ``models`` gate and its
    non-empty assignment guard — both are call-site policy, not parsing.

    Also records ``currentModelId`` for ``_track_metadata``'s context
    window lookup.
    """
    # Imported lazily: acp.session_handle imports this module at module
    # level, so a top-level import here would be a cycle.
    from kiro_crew.acp.session_handle import models_from_config_options

    models = session_resp.get("models")
    if not isinstance(models, dict):
        # Adapters that omit `models` still advertise via configOptions. One
        # authoring, shared with the shared-runtime driver's own capture.
        models = models_from_config_options(session_resp, self.backend)
        if models is None:
            return
    current_model_id = models.get("currentModelId")
    if isinstance(current_model_id, str) and current_model_id:
        self._resolved_model_id = current_model_id
    # Imported lazily: acp.session_handle imports this module at module
    # level, so a top-level import here would be a cycle.
    from kiro_crew.acp.session_handle import parse_advertised_models

    # Parse the gated sub-payload, not the whole response: the parser's
    # dict-or-list fallback would otherwise let an EMPTY (falsy) models
    # object fall through to a top-level ``availableModels`` key, sourcing
    # the list from a payload the dict gate above never saw.
    # NOTE: the pre-consolidation code returned early on a malformed
    # ``availableModels``; nothing may be appended after this block
    # without re-adding that malformed-payload guard.
    captured = parse_advertised_models({"models": models})
    if captured:
        self._available_models = captured
        self._available_models_captured_at = time.monotonic()
        self._available_models_probe_confirmed = False
        # Feed the discovered ids into the cross-session provider-model cache
        # so the next session's settings seed can source availableModels (and
        # the wire model id) from what this backend actually serves rather
        # than the static registry. In-memory + synchronous here (cheap, and
        # this method is sync); the disk persist is offloaded by the async
        # caller when this reports a change. Gated on capability, not on
        # ``_is_claude`` (harness-parity H6): kiro-cli reaches its models via
        # --agent and its windows via the --list-models cache, so it is not a
        # member and feeds nothing; a future adapter with the same served-vs-
        # stored spelling gap opts into the set and is fed here automatically,
        # keyed by its own registry namespace.
        if self._uses_advertised_model_selection:
            self._advertised_models_changed = model_registry.refresh_advertised_models(
                self._model_registry_namespace, self._advertised_model_ids()
            )


async def _persist_advertised_models_if_changed(self: AcpClient) -> None:
    """Offload a disk persist of the provider-model cache when it changed.

    Mirrors the kiro-window cache's split: :func:`refresh_advertised_models`
    did the cheap in-memory update synchronously in
    ``_capture_available_models`` and set ``_advertised_models_changed``;
    this offloads only the blocking write so the init path never persists on
    the event loop, and never for an unchanged cache. Best-effort — a write
    failure is swallowed by ``persist_advertised_models`` itself.
    """
    if not self._advertised_models_changed:
        return
    self._advertised_models_changed = False
    await asyncio.to_thread(model_registry.persist_advertised_models)


def available_models(self: AcpClient) -> list[dict[str, str]]:
    """Models advertised by the backend at session init (may be empty)."""
    return list(self._available_models)


def _advertised_model_ids(self: AcpClient) -> list[str]:
    """Advertised model ids, for the model-rejection error path.

    Empty when the backend advertised nothing (no session yet, or a backend
    that omits ``models``), which the error path reads as "entitlement
    unknown" and leaves the existing transient/capacity handling alone.
    """
    ids = []
    for entry in self._available_models:
        model_id = entry.get("modelId") if isinstance(entry, dict) else None
        if isinstance(model_id, str) and model_id.strip():
            ids.append(model_id)
    return ids


def _model_is_unusable(self: AcpClient, model_id: str) -> bool:
    """Whether this session's advertised set excludes *model_id*.

    Thin bind of :func:`model_is_unusable` to the set captured at
    ``session/new`` / ``session/load``, so this client and the
    ``providers.acp`` live path share one definition of entitlement.
    """
    return model_is_unusable(model_id, self._advertised_model_ids())


async def refresh_available_models(self: AcpClient) -> list[dict[str, str]]:
    """Re-resolve this session's advertised-model snapshot against the backend.

    The direct-spawn counterpart to
    :meth:`AcpSessionHandle.refresh_available_models`, called by the
    explicit-pick refusal in :meth:`set_model`, the startup pin withhold and
    the dedicated-transport picker read. This client owns one process and no
    shared probe cache, so its own snapshot IS the cache, and the runtime
    probe's freshness rules apply to it directly:

    * A snapshot a probe confirmed within ``_ENTITLEMENT_PROBE_TTL_SECS`` is
      fresh evidence and is returned without a round-trip -- a fresh list is
      never re-probed. That is the result-clock replay with the freshness
      floor at the snapshot's own capture time.
    * There is no failure replay. A user action on the shared runtime passes
      ``force=True`` to skip the attempt-clock replay, so a failed attempt is
      never replayed here either.
    * Single-flight is owned HERE, not by the callers: every caller that
      arrives while a probe is in flight awaits that same probe, so a picker
      poll and an explicit pick overlapping never start two probe processes.
      The in-flight probe is shielded, so one caller's cancellation does not
      take the answer away from the others (the probe still tears its own
      process down).

    The snapshot is replaced only by a NON-EMPTY answer, dated by the moment
    that answer ARRIVED and marked probe-confirmed. A failed or empty probe is
    not evidence about entitlement and leaves the snapshot as it was. Returns
    the probe result (``[]`` = no evidence), or the fresh snapshot on a TTL hit.
    """
    # Lazy: acp.runtime imports this module at module level.
    from kiro_crew.acp.runtime import _ENTITLEMENT_PROBE_TTL_SECS

    now = time.monotonic()
    captured_at = getattr(self, "_available_models_captured_at", 0.0)
    if (
        getattr(self, "_available_models_probe_confirmed", False)
        and self._available_models
        and captured_at > 0.0
        and now - captured_at < _ENTITLEMENT_PROBE_TTL_SECS
    ):
        return list(self._available_models)
    inflight: asyncio.Future[tuple[list[dict[str, str]], float]] | None = getattr(
        self, "_entitlement_probe_inflight", None
    )
    if inflight is None or inflight.done():
        inflight = asyncio.ensure_future(self._probe_advertised_models())
        self._entitlement_probe_inflight = inflight
    fresh, answered_at = await asyncio.shield(inflight)
    if fresh and answered_at >= getattr(self, "_available_models_captured_at", 0.0):
        self._available_models = list(fresh)
        self._available_models_captured_at = answered_at
        self._available_models_probe_confirmed = True
    return list(fresh)


def _entitlement_probe_client(self: AcpClient) -> AcpClient:
    """A throwaway client for one entitlement probe, on its OWN transport.

    Built from this client's own launch inputs (binary, env, work dir, agent
    spelling, sandbox mode, gateway overlay) through the ordinary constructor,
    so :meth:`_spawn` launches it exactly as it launched this session -- there
    is no second spelling of the spawn. It gets its own process and its own
    stdout reader and notification buffer: nothing the probe process emits
    (session updates, MCP OAuth prompts and readiness reports for whatever
    servers kiro-cli starts from the spec, substitution advisories) can reach
    this session's buffer, its OAuth dedupe set or its event stream. No audit
    source and no shared scratch: the probe never runs a turn.
    """
    return type(self)(
        work_dir=self._work_dir,
        model=None,
        agent=self._agent,
        sandbox_mode=self._sandbox_mode,
        session_key=self._session_key,
        channel_id=self._channel_id,
        extra_env=dict(self._extra_env),
        acp_backend=self._acp_backend,
        mcp_gateway_overlay=self._mcp_gateway_overlay,
        mcp_gateway_socket=self._mcp_gateway_socket,
        permission_mode=self._permission_mode,
    )


async def _probe_advertised_models(self: AcpClient) -> tuple[list[dict[str, str]], float]:
    """One throwaway session on a dedicated process, read for its model list.

    The same question :meth:`AcpRuntime.probe_advertised_models` asks on the
    shared process, asked of a short-lived kiro-cli started for the purpose
    (:meth:`_entitlement_probe_client`): ``initialize``, one minimal
    ``session/new`` (no mode activation), its model list parsed through the
    shared :func:`advertised_models_from_session` fold, the harness's own
    teardown verb, then the process is shut down. Everything else that
    process emitted dies with its client. Returns ``(list, arrival_time)``;
    ``[]`` means the probe failed or advertised nothing, never "entitled to
    nothing". kiro-only: the entitlement race it answers is kiro-cli's.
    """
    # Lazy: acp.runtime imports this module at module level.
    from kiro_crew.acp.runtime import _ENTITLEMENT_PROBE_TIMEOUT

    # kiro-only, positively: the entitlement race this answers is kiro-cli's,
    # and a session must already exist to have something to re-probe for.
    if self.backend == ACP_BACKEND_KIRO and self._session_id:
        probe: AcpClient | None = None
        try:
            probe = self._entitlement_probe_client()
            return await asyncio.wait_for(
                probe._entitlement_probe_answer(),
                timeout=_INIT_TIMEOUT + _ENTITLEMENT_PROBE_TIMEOUT,
            )
        except Exception:
            logger.debug("direct-client entitlement probe failed", exc_info=True)
            return [], 0.0
        finally:
            if probe is not None:
                try:
                    await asyncio.shield(probe.shutdown())
                except Exception:
                    logger.debug("direct-client entitlement probe shutdown failed", exc_info=True)
    return [], 0.0


async def _entitlement_probe_answer(self: AcpClient) -> tuple[list[dict[str, str]], float]:
    """Run the probe handshake on THIS (throwaway) client's own process."""
    # Lazy: acp.runtime and acp.session_handle import this module.
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp.runtime import _ENTITLEMENT_PROBE_TIMEOUT, _TERMINATE_TIMEOUT
    from kiro_crew.acp.session_handle import advertised_models_from_session

    await self._spawn()
    init_id = await self._send_request(METHOD_INITIALIZE, self._initialize_params())
    await self._wait_for_response(init_id, timeout=_INIT_TIMEOUT)
    # Close the verify->create bracket every other spawn path closes between
    # initialize and session/new: a spec revoked during probe init must not
    # create a session (which starts that spec's MCP server commands). A stale
    # snapshot raises DerivedSpecStale, which _probe_advertised_models absorbs
    # as a failed probe (empty answer -> the refusal is kept), so no session is
    # ever created on a spec nobody verified.
    await asyncio.to_thread(require_unchanged_derived_spec, self._derived_spec_snapshot)
    params = build_session_new_params(
        await self._session_work_dir(),
        # The pooled broker stubs, for cost alone: they outrank the spec entries,
        # so the probe does not start a private copy of every pooled server.
        # Isolation does not depend on it -- whatever this process starts is
        # confined to its own transport and reaped with it.
        mcp_servers=await asyncio.to_thread(self._pooled_mcp_servers),
    )
    req_id = await self._send_request(METHOD_SESSION_NEW, params)
    resp = await self._wait_for_response(
        req_id, timeout=_ENTITLEMENT_PROBE_TIMEOUT, method=METHOD_SESSION_NEW
    )
    answered_at = time.monotonic()
    fresh = advertised_models_from_session(resp, self.backend)
    probe_sid = str(resp.get("sessionId") or "")
    if probe_sid:
        policy = harness_for(self.backend).teardown
        try:
            # Every harness this probe runs on tears a session down with an
            # awaited request (``TeardownPolicy.notification`` is False for
            # kiro, KAS and codex; test_deny_bounded_write pins it), so the
            # request is the only path here.
            teardown_id = await self._send_request(policy.method, {"sessionId": probe_sid})
            await self._wait_for_response(teardown_id, timeout=_TERMINATE_TIMEOUT)
        except Exception:
            logger.debug("direct-client probe session teardown failed", exc_info=True)
    return fresh, answered_at


def _model_config_candidates(model_id: str) -> list[str]:
    """Ordered fallback spellings for a config-option model push.

    The cold-cache companion to :func:`resolve_wire_model_id`'s fold: with
    an empty advertised cache there is nothing to fold against, so a
    prefixed ``[1m]`` id would otherwise reach the wire verbatim. Candidates
    are derived from the id itself — verbatim, then prefix-stripped, then
    prefix- and window-stripped — and the adapter judges each; it knows
    what it accepts.
    """
    out = [model_id]
    stripped = model_registry.strip_provider_id_prefix(model_id)
    if stripped != model_id:
        out.append(stripped)
    bare = stripped.replace("[1m]", "")
    if bare != stripped:
        out.append(bare)
    return out


async def _push_model_config_option(self: AcpClient, model_id: str, *, strict: bool) -> str:
    """Push ``model`` over ``session/set_config_option`` with cold-cache fallback.

    The value-rejection twin of ``_set_effort_config_option``'s ladder in
    ``providers.acp``: a model the adapter refuses must not bubble up as a
    generic ``AcpError`` — that failure path resets the session and drops
    the user onto the adapter default with no explanation. Each candidate
    spelling is tried in turn; the first accepted one wins and is returned
    so the caller records the spelling that actually went on the wire.

    ``strict=True`` (an explicit user pick, ``set_model``) raises
    ``AcpModelUnavailable`` when every candidate is refused — a silent
    downgrade would report success while running something else.
    ``strict=False`` (startup application of an inherited value) returns
    ``""`` so the caller stays on the backend default, mirroring the
    withhold contract in :meth:`_apply_startup_model`.

    Two shapes count as a value rejection. claude-agent-acp names the
    option in its message (``Invalid value for config option model: ...``).
    A codex session instead dies on a bare JSON-RPC ``-32602 Invalid params``
    with no detail — the same frame a malformed request would draw, but the
    request shape here is fixed and the value is the only thing that varies,
    so the code IS the rejection. Before this was read as a protocol failure
    it re-raised, the session init failed, and a stale model pin from another
    backend killed every codex session at startup.
    """
    # Each push describes only itself; the split below sets it again.
    self.model_pin_partial = ""
    last_exc: AcpError | None = None
    for cand in self._model_config_candidates(model_id):
        try:
            await self.set_config_option("model", cand)
        except AcpError as exc:
            msg = str(exc)
            lowered = msg.lower()
            if "unknown config option" in lowered:
                # No 'model' config option at all (other adapter build):
                # retrying spellings cannot help.
                if strict:
                    raise
                logger.debug("adapter exposes no 'model' config option; skipping model push")
                return ""
            if not _is_config_value_rejection(exc, MODEL_CONFIG_ID, self.backend):
                raise  # transport/protocol failure — not a value rejection
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
    # is applied as its two halves instead.
    split_applied = await _push_model_via_effort_split(self, self.backend, model_id)
    if split_applied:
        return split_applied
    _rejected_log, _ = redact_exfiltration_urls(str(model_id))
    _rejected_log, _ = redact_credentials(_rejected_log)
    advertised_ids = self._advertised_model_ids()
    if strict:
        raise AcpModelUnavailable(
            _rejected_log,
            advertised_ids,
            backend=self.backend,
            # Only a pair-id harness earns the adapter-mismatch wording: on
            # those the advertised list IS the entitlement, so refusing
            # something on it is the adapter contradicting itself. Elsewhere an
            # advertised id may simply be out of the account's reach, and the
            # entitlement wording is the true answer (the `whoami` hint belongs
            # to harnesses on the host kiro-cli identity store;
            # `host_auth.signs_in_separately` decides). "Advertised" is the
            # backend-aware resolver's verdict, so a BARE id the pairs serve
            # (``resolve_pin_spelling_on``) counts: a pair-only list never
            # contains the bare id literally.
            advertised_but_refused=(
                self.backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
                and bool(
                    runtime_models.resolve_pin_spelling_on(
                        model_id, advertised_ids, backend=self.backend
                    )
                )
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


async def _ensure_served_default(self: AcpClient) -> None:
    """Move an inheriting session off a backend default it cannot run.

    The companion to :meth:`_apply_startup_model`'s withhold: that one keeps
    an unusable PIN off the wire, this one keeps an unusable INHERITED
    default off the session. Both exits of that method leave the session on
    whatever ``session/new`` assigned, and nothing else checks that id
    against the list the same response advertised — so a partition whose
    default is not in its own served list runs a session that fails on its
    first prompt.

    The served-list check is kiro only: its advertised ids are exactly the ids
    ``session/set_model`` accepts, so "absent from the list" genuinely means
    unusable. The claude backend advertises a different namespace than the
    model it runs and announces its own substitutions instead. Its gap is a
    different one, closed by :meth:`_reassert_adapter_resolved_model`: the
    model it reports is not always the model Claude Code runs.

    ``self._model`` is deliberately left alone. ``""``/``"auto"`` there mean
    "inherit" to every reader of that field (the settings seed, the
    warm-pool re-apply), and this session IS still inheriting — the wire is
    corrected, the intent is not rewritten.
    """
    if self._is_kiro:
        advertised = self._advertised_model_ids()
        unserved = self._resolved_model_id or ""
        fallback = pick_served_default(unserved, advertised)
        if not fallback:
            return
        _unserved_log = redact_log_via_context(str(unserved))
        # The kiro gate also fixes the wire: kiro-cli takes the model via
        # ``session/set_model``, never via a session config option.
        await self._send_request(
            METHOD_SET_MODEL,
            {"sessionId": self._session_id, "modelId": fallback},
        )
        self._resolved_model_id = fallback
        logger.warning(
            "ACP backend default %s is not in this account's served list (advertised: %s); "
            "switched the session to %s",
            _unserved_log,
            ", ".join(advertised),
            fallback,
        )
    elif self._is_claude:
        await self._reassert_adapter_resolved_model()


def _note_agent_switched(self: AcpClient, params: dict[str, Any] | None) -> None:
    """Record the model a ``_kiro.dev/agent/switched`` frame names.

    kiro-cli's ``session/set_mode`` puts the session on the model the
    activated agent's spec pins and reports it only in this frame, which
    lands whenever the next read reaches it: during the start drain, or
    inside a later prompt. The record waits there for
    :meth:`_judge_activated_model`, which runs before the next prompt.

    The first frame after the start path's ``set_mode`` belongs to that
    activation. A ``session/set_model`` sent after the ``set_mode`` and
    before this frame was read is what the session runs, so that frame's
    model is ignored. A frame without a model is no evidence and records
    nothing.
    """
    superseded = getattr(self, "_mode_frame_awaited", False) and getattr(
        self, "_mode_frame_superseded", False
    )
    self._mode_frame_awaited = False
    self._mode_frame_superseded = False
    if superseded:
        return
    model = (params or {}).get("model")
    if isinstance(model, str) and model.strip():
        self._activated_model = model.strip()


async def _judge_activated_model(self: AcpClient) -> None:
    """Run the served-default check on the model an agent switch applied.

    The startup check in :meth:`_apply_startup_model` judges the
    ``session/new`` id, which an agent activation replaces. The model the
    recorded agent/switched frame named is the one the session runs now:
    a served one is adopted as the resolved model with no extra write, an
    unserved one is moved to a served model before the next prompt. kiro
    only, the one backend that sends the frame with a model.
    """
    activated = getattr(self, "_activated_model", "")
    if not activated:
        return
    self._activated_model = ""
    if self._is_kiro:
        self._resolved_model_id = activated
        await self._ensure_served_default()


async def _reassert_adapter_resolved_model(self: AcpClient) -> None:
    """Send an inheriting claude session the model its adapter reports.

    claude-agent-acp resolves an inheriting session's model from
    ``ANTHROPIC_MODEL`` or the user's ``settings.model`` and reports it as the
    ``model`` option's current value. When that value is the setting verbatim it
    does not pass it on, trusting Claude Code to have read the same setting; a
    resumed session can instead run Claude Code's built-in default while the
    adapter still reports the settings model. A custom gateway that does not
    serve that built-in default then refuses every turn. The report cannot tell
    the two cases apart, so the reported id is sent back over
    ``session/set_config_option``: the write the picker and ``/model`` make,
    which the adapter always passes on to Claude Code.

    Nothing is sent when the reported id is the head of the advertised list:
    with no model setting the adapter reports that entry (its ``default``
    pseudo-model), so there is no setting to re-apply. Nothing is sent for an
    id the list does not carry either, since the adapter would refuse it.

    Best effort: the session started without this write, so a refused value or
    a failed request leaves it on the adapter's own model and only logs. A dead
    process still propagates, because the session cannot continue.
    """
    advertised = self._advertised_model_ids()
    reported = self._resolved_model_id or ""
    if reported not in advertised or reported == advertised[0]:
        return
    _reported_log = redact_log_via_context(reported)
    # An advisory belongs to the request that emitted it (see set_model).
    self._last_substitution_model = None
    try:
        sent = await self._push_model_config_option(reported, strict=False)
    except AcpProcessDied:
        raise
    except AcpError as exc:
        logger.warning(
            "ACP model %s reported by the backend could not be re-applied: %s",
            _reported_log,
            redact_log_via_context(str(exc)),
        )
        return
    if not sent:
        return
    self._resolved_model_id = self._last_substitution_model or sent
    logger.info("ACP model: re-applied %s, resolved by the backend's settings", _reported_log)


async def _apply_startup_model(self: AcpClient) -> None:
    """Apply the configured model to a freshly initialized session.

    Split out of ``_init_session`` step 5 so the withhold decision is
    reachable without standing up a whole session.

    The model here was NOT chosen for this turn: it arrives from the agent
    spec, the config default, or a slot value persisted before the account's
    entitlements were known. So when the backend has already told us the
    account cannot run it, withholding beats failing — the user did not pick
    this model and cannot be expected to know why every turn dies. The
    session simply stays on the backend's own default, which ``session/new``
    already applied and reported as ``currentModelId``.

    Note this fixes the WIRE, not the stored setting: the persisted config /
    slot value is untouched, so a picker reading it still shows the model
    that was withheld. Healing the stored value is a separate change.

    One class of inherited pin is refused for EVERY backend rather than only
    the entitlement-checked one: a pin that a different harness's model
    catalog claims and this one does not (``model_scope``). That is a
    statement about which harness the value was picked in, which every
    backend can answer from its own namespace, unlike an entitlement question
    that only kiro can answer for its own partition. It is the case that
    motivated this withhold contract in the first place: a model chosen under
    one backend and re-sent after a switch to another. Scoped out before the
    wire, so the adapter never refuses the id and never warns about it.

    An EXPLICIT switch is handled the opposite way in :meth:`set_model`:
    there the user asked for that exact model, and quietly running another
    one would be a lie.

    The fold onto the advertised spelling happens HERE, not only at spawn: by
    this point ``session/new`` has been captured, so the advertised-model cache
    is warm even on the first-ever session -- whereas the spawn-time fold ran
    against a cold cache and was a no-op, sending a bare id that resolves to
    the base window. Same call as :meth:`set_model` uses, so an explicit switch
    and a startup application agree on one exact spelling.
    """
    if not self._model or self._model == DEFAULT_MODEL:
        logger.info(
            "ACP model: %s (from agent config; backend reports %s)",
            self._model or "auto",
            redact_log_via_context(self._resolved_model_id or "") or "none",
        )
        # Inheriting is only safe when the inherited model is served; the
        # backend can default to one this partition does not carry.
        await self._ensure_served_default()
        return
    advertised = self._advertised_model_ids()
    # Factory resolution covers every surface from the shared cache; this
    # wire site also carries the current session's fresh advertised list.
    if not model_scope.pin_applies(
        self._model,
        self._model_registry_namespace,
        advertised=advertised,
    ):
        # Another harness's model. Recorded as the default for the same
        # reason the entitlement withhold below does: the "!= DEFAULT_MODEL"
        # test above is what the warm-pool re-apply path reads, so leaving
        # the foreign id here would re-offer it on every claim.
        self._model = (
            model_scope.scoped_pin(
                self._model,
                self._model_registry_namespace,
                advertised=advertised,
                source=f"{self.backend} startup",
            )
            or DEFAULT_MODEL
        )
        await self._ensure_served_default()
        return
    if self._uses_advertised_model_selection:
        self._model = model_registry.resolve_wire_model_id(
            self._model, self._model_registry_namespace
        )
    if self._is_kiro and self._model_is_unusable(self._model):
        # A literal miss can be a stale ``<namespace>::`` qualifier on a
        # model the backend fully serves: resolve to the advertised
        # spelling and send THAT, so the session runs the model the pin
        # names instead of silently dropping to the default. Same fold the
        # display verdict uses (chat_runner._pinned_model_verdict), so the
        # chip and the wire cannot disagree about what "usable" means. A
        # pin absent under either spelling still takes the withhold below.
        _resolved = runtime_models.resolve_pin_spelling(self._model, self._advertised_model_ids())
        if not _resolved:
            # The fold found nothing, but the snapshot was captured seconds
            # ago at session/new -- inside the startup race where an
            # entitlement lookup can answer the free-tier default. Same
            # revalidate-once-before-withholding as the shared runtime's
            # spawn-time pin withhold: a pin the fresh answer serves is
            # applied, and a failed probe leaves the withhold as it was.
            fresh = runtime_models.advertised_model_ids(await self.refresh_available_models())
            if fresh and not model_is_unusable(self._model, fresh):
                _resolved = self._model
            elif fresh:
                _resolved = runtime_models.resolve_pin_spelling(self._model, fresh)
        if _resolved:
            logger.info(
                "ACP model %s resolves to advertised %s; sending the advertised spelling",
                self._model,
                _resolved,
            )
            self._model = _resolved
        else:
            _withheld_log, _ = redact_exfiltration_urls(str(self._model))
            _withheld_log, _ = redact_credentials(_withheld_log)
            logger.warning(
                "ACP model %s is not available to this account; staying on the "
                "backend default %s (advertised: %s)",
                _withheld_log,
                self._resolved_model_id or DEFAULT_MODEL,
                ", ".join(self._advertised_model_ids()),
            )
            # Record the session as running the default rather than the value we
            # declined: the "!= DEFAULT_MODEL" test above is also what the
            # warm-pool re-apply path reads (session_provider), so leaving the
            # unusable id here would re-offer it on every claim. The declined
            # pin never runs, so it is recorded where a caller billing or
            # labelling the turn by the pin looks for that.
            self.model_pin_refused = self._model
            self._model = DEFAULT_MODEL
            # Now inheriting, so the same served-default check applies: the
            # default we fall back to can itself be one the account lacks.
            await self._ensure_served_default()
            return
    # An advisory belongs to the request that emitted it, the same rule
    # set_model states: a substitution recorded before this startup
    # override — by an earlier switch on a runtime this process reset or
    # resumed — would otherwise be read below as this dispatch's own
    # served model and misattribute the session.
    self._last_substitution_model = None
    if self.backend in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION:
        sent = await self._push_model_config_option(self._model, strict=False)
        if not sent:
            # Every spelling refused: record the session as running the
            # default (the warm-pool re-apply path reads this field, so
            # leaving the refused id here would re-offer it every claim)
            # and let session/new's own model stand. Remember the refused
            # pin so a caller billing by it can tell it never ran.
            self.model_pin_refused = self._model
            self._model = DEFAULT_MODEL
            return
        self.model_pin_refused = ""
        self._model = sent
    else:
        await self._send_request(
            METHOD_SET_MODEL,
            {"sessionId": self._session_id, "modelId": self._model},
        )
    # session/new reports the backend default before an explicit override.
    # A config-option advisory may have substituted the served model.
    self._resolved_model_id = self._last_substitution_model or self._model
    logger.info("ACP model: %s", self._model)


async def set_config_option(self: AcpClient, config_id: str, value: str) -> None:
    """Set a session config option (e.g. effort level) via session/set_config_option.

    When the response carries the session's full ``configOptions`` (the ACP
    response shape), they replace the cached ones, as
    ``SessionHandle.set_config_option`` does. A write can change which options
    exist -- goose rebuilds its ``thinking_effort`` select for the model a
    ``model`` write selects -- and the matching ``config_option_update`` that
    arrives first is only buffered by ``_wait_for_response``, which nothing
    drains after init. Without this, ``supports_effort`` after a live model
    switch reads the previous model's option.
    """
    if not self._session_id:
        raise AcpError("Cannot set config option before session is initialized")
    req_id = await self._send_request(
        "session/set_config_option",
        {"sessionId": self._session_id, "configId": config_id, "value": value},
    )
    result = await self._wait_for_response(req_id, timeout=10.0)
    config_options = result.get("configOptions") if isinstance(result, dict) else None
    if isinstance(config_options, list):
        self._acp_config_options = config_options
        self._sync_effort_levels()


def _store_session_config(self: AcpClient, resp: dict) -> None:
    """Extract effort configOptions from a session/new or session/load response.

    Model lists are captured separately by ``_capture_available_models``,
    which parses the real dict-shaped ``models`` payload
    (``{availableModels: [...]}``); only the ``configOptions`` effort
    selector is consumed here.
    """
    logger.debug("_store_session_config keys: %s", list(resp.keys()))
    config_options = resp.get("configOptions")
    if isinstance(config_options, list):
        self._acp_config_options = config_options
        logger.debug("ACP config options loaded: %d entries", len(config_options))
        self._sync_effort_levels()
    # Capture advertised mode ids + whether a modes list was advertised at
    # all, so step 4's set_mode can fail closed against a requested agent the
    # backend never loaded (would fault with "Mode '<agent>' not found").
    # Assigned unconditionally so a re-init that omits `modes` clears any
    # stale state rather than guarding on it.
    self._available_mode_ids, _current_mode, self._modes_advertised = parse_session_modes(resp)


def _handle_config_option_update(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Process a config_option_update session notification.

    ACP emits this when config changes (e.g. model switch rebuilds effort options).
    The payload is a full configOptions array that replaces the previous one.
    """
    params = msg.params or {}
    update = params.get("update", {})
    if not isinstance(update, dict):
        return
    config_options = update.get("configOptions")
    if isinstance(config_options, list):
        self._acp_config_options = config_options
        logger.debug("ACP config options updated: %d entries", len(config_options))
        self._sync_effort_levels()


def _sync_effort_levels(self: AcpClient) -> None:
    """Push ACP-reported effort levels to the global validation set.

    A backend in ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`` advertises a
    list that belongs to this session alone (pi adds ``minimal``, deepseek
    omits ``medium`` and ``xhigh``, goose offers ``off`` on a model that does
    not think). Its levels join the validation set so the API and the picker
    accept them, but they never replace the global fallback ORDER, which a
    slot whose provider advertises no effort option of its own (kiro-cli)
    is served. Every other backend's list still seeds that fallback.
    """
    levels = self.get_valid_effort_levels()
    if not levels:
        return
    if self.backend in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION:
        # circular import: chat_persistence → dashboard → session → acp.client
        from kiro_crew.dashboard.chat_persistence import register_reasoning_effort_values

        register_reasoning_effort_values(levels)
        return
    # circular import: chat_persistence → dashboard → session → acp.client
    from kiro_crew.dashboard.chat_persistence import update_reasoning_effort_values

    update_reasoning_effort_values(levels)


def supports_config_option(self: AcpClient, config_id: str) -> bool:
    """Whether the session advertised a config option with this id.

    Older claude-agent-acp builds do not expose an ``effort`` selector at
    all; pushing ``session/set_config_option`` for it then fails with
    ``Unknown config option`` (a -32603 Internal error, distinct from a
    value-level rejection). Callers gate on this so an adapter that lacks
    the option is a silent no-op rather than a noisy error + session reset.

    Returns True when no config options were reported yet, so that a
    backend which advertises options lazily (after the first turn) is not
    permanently treated as unsupported.
    """
    if not self._acp_config_options:
        return True
    return any(
        isinstance(opt, dict) and opt.get("id") == config_id for opt in self._acp_config_options
    )


def get_valid_effort_levels(self: AcpClient) -> list[str]:
    """Return valid effort levels from ACP config, preserving ACP order.

    Parses configOptions for the entry whose id is this backend's effort
    option -- ``effort`` for most, ``reasoning_effort`` for codex-acp -- and
    extracts its ``options[].value`` list in the order ACP reported them.
    Resolving the id here is what fills the dropdown on a backend that spells
    it differently; a hard-coded spelling returns an empty list there, which
    every caller reads as "this model has no effort levels". A value the
    backend uses for "no override" (opencode's ``default``) is not a level and
    is dropped.
    """
    effort_option = effort_config_option_id(self.backend)
    for opt in self._acp_config_options:
        if not isinstance(opt, dict):
            continue
        if opt.get("id") == effort_option:
            options = opt.get("options", [])
            if isinstance(options, list):
                return effort_config_option_levels(
                    self.backend,
                    [o["value"] for o in options if isinstance(o, dict) and "value" in o],
                )
    return []
