"""The config PATCH route: one validated write of the editable paths a request names.

``PATCH /api/config/kirocrew`` checks each path against the editable schema and the
governance pins, writes them under the config lock, and answers with the masked
view and whether a restart is needed.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import shutil
from typing import Any

from aiohttp import web

from kiro_crew import beacon
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.superseded_defaults import ack_explicit_write
from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.core_config_models import (
    _active_provider_and_pin_backend,
    _active_provider_name,
    _revalidate_role_pin_evidence,
    _validate_role_model,
)
from kiro_crew.dashboard.handlers.core_config_schema import _editable_config_spec
from kiro_crew.dashboard.handlers.core_config_view import (
    _hot_apply_after_write,
    _masked_config_dict,
)
from kiro_crew.dashboard.handlers.core_security import _sel
from kiro_crew.metrics import provider as _metrics_provider

logger = logging.getLogger("kiro_crew.dashboard.handlers.core")


# Keys a caller may reasonably try to PATCH that have a dedicated endpoint whose
# side effects the generic config write cannot reproduce. Naming the endpoint turns
# a dead end ("field not editable") into a next step.
_MOVED_CONFIG_FIELDS: dict[str, str] = {
    "agent.apps_allow_third_party": (
        "agent.apps_allow_third_party is not editable here because turning it off "
        "must also stop the third-party app code it was admitting. Use "
        "PUT /api/security/trusted-apps/allow-all, which runs that teardown and "
        "reports anything it could not stop."
    ),
}


def _beacon_governance_pinned_off() -> bool:
    """Return whether a ceiling pins ``capabilities.telemetry`` off (blocking).

    Delegates to ``beacon.is_governance_pinned_off`` rather than re-resolving, so
    the PATCH gate and the send gate can never disagree about whether a host is
    pinned — two independent resolutions would be two things to keep in sync.

    Runs in a worker thread (see the call site): the resolution reads the
    trust-root policy file and the active profile from disk.

    ``audit_tool``: this is an ENFORCEMENT decision (it refuses the write with a
    403), so it routes through the audited seam and lands a
    ``governance_decision`` SEL record — matching the send gate and both CLI
    refusals. The name is distinct per call site so the trail says which control
    refused. The dashboard route additionally logs its own ``config.patch`` denial
    via ``_log_sel``; that records the API call, while this records the governance
    decision behind it.
    """
    return beacon.is_governance_pinned_off(audit_tool="config_patch_dashboard")


def _tailnet_governance_pinned_off() -> bool:
    """Return whether a ceiling pins ``capabilities.tailnet_origin`` off (blocking).

    The tailnet twin of :func:`_beacon_governance_pinned_off`, and delegating for
    the same reason: ``tailnet.is_governance_pinned_off`` is the one resolution, so
    the PATCH gate, the startup derivation gate and the CLI gate cannot disagree
    about whether a host is pinned.

    Runs in a worker thread (see the call site): the resolution reads the
    trust-root policy file and the active profile from disk.

    ``audit_tool``: this is an ENFORCEMENT decision (it refuses the write with a
    403), so it routes through the audited seam and lands a
    ``governance_decision`` SEL record. The name is distinct per call site so the
    trail says which control refused; the route additionally logs its own
    ``config.patch`` denial via ``_log_sel``, which records the API call while
    this records the governance decision behind it.
    """
    from kiro_crew.dashboard import tailnet  # noqa: F811 - local: keeps the import edge lazy

    return tailnet.is_governance_pinned_off(audit_tool="config_patch_dashboard_tailnet")


class _ConfigKeyAlreadySet(Exception):
    """A conditional config write found an existing leaf under the write lock."""


async def api_kirocrew_config_patch(request: web.Request) -> web.Response:
    """PATCH /api/config/kirocrew — update a single config field."""
    from kiro_crew.config.loader import (
        ConfigReadError,
        ConfigWriteRefused,
        config_path,
        update_config_locked,
    )

    caller = request.get("user")
    if not caller:
        logger.warning(
            "config.patch called without authenticated user; falling back to 'dashboard'"
        )
        caller = "dashboard"

    def _log_sel(outcome: str, resources: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="config.patch",
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )

    def _deny(msg: str, resources: str = "", status: int = 400) -> web.Response:
        _log_sel("denied", resources or msg)
        return web.json_response({"error": msg}, status=status)

    denied = await require_owner_dashboard_request(request, "config.patch")
    if denied is not None:
        return denied

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", "invalid JSON body")
    if not isinstance(body, dict):
        return _deny("invalid JSON", "invalid JSON body")

    path_key: str = body.get("path", "")
    value: Any = body.get("value")
    if_unset = body.get("if_unset", False)
    if not isinstance(if_unset, bool):
        return _deny("if_unset must be a boolean")
    clamp_notice: dict | None = None

    spec = _editable_config_spec(path_key)
    if not spec:
        # `agent.apps_allow_third_party` was deliberately REMOVED from the editable
        # set. It is not an ordinary preference: turning it off has to stop the code
        # it was admitting, which means a teardown sweep (shutdown hooks, backend
        # processes, cron deregistration) that this generic read-modify-write knows
        # nothing about. A plain PATCH here would flip the flag and leave every app
        # it admitted still executing — trust withdrawn on paper only. The dedicated
        # endpoint owns that sequencing, so point the caller at it instead of
        # silently accepting a write that cannot honour the setting's meaning.
        if path_key in _MOVED_CONFIG_FIELDS:
            return _deny(_MOVED_CONFIG_FIELDS[path_key], f"{path_key}={value}")
        return _deny(f"field not editable: {path_key}", f"{path_key}={value}")

    # Validate value
    if spec["type"] == "enum":
        # ``values_fn`` (the same hook the ``str`` branch already carries) is for an
        # enum whose membership is not knowable at import: it can widen after boot
        # when an edition registers a backend. A static ``values`` list would be
        # read before that happened.
        allowed = list(spec["values_fn"]()) if "values_fn" in spec else spec["values"]
        if value not in allowed:
            return _deny(f"invalid value, must be one of {allowed}", f"{path_key}={value}")
    elif spec["type"] == "int":
        try:
            value = body_int(value)
        except (TypeError, ValueError):
            return _deny("must be an integer", f"{path_key}={value}")
        lo, hi = spec.get("min", 0), spec.get("max", 999999)
        if value < lo or value > hi:
            return _deny(f"must be between {lo} and {hi}", f"{path_key}={value}")
    elif spec["type"] == "bool":
        if not isinstance(value, bool):
            return _deny("must be a boolean", f"{path_key}={value}")
    elif spec["type"] == "float":
        clamp_fn = spec.get("clamp_fn")
        if clamp_fn is not None and isinstance(value, bool):
            # bool is an int subclass: `true` must not save as a 1 s budget.
            return _deny("must be a number", f"{path_key}={value}")
        try:
            value = float(value)
        except (TypeError, ValueError):
            return _deny("must be a number", f"{path_key}={value}")
        if not math.isfinite(value):
            return _deny("must be a finite number", f"{path_key}={value}")
        lo, hi = spec.get("min", 0.0), spec.get("max", 999999.0)
        if clamp_fn is not None:
            stored = float(clamp_fn(value))
            if stored != value:
                clamp_notice = {
                    "path": path_key,
                    "requested": value,
                    "stored": stored,
                    "min": float(lo),
                    "max": float(hi),
                }
            value = stored
        elif value < lo or value > hi:
            return _deny(f"must be between {lo} and {hi}", f"{path_key}={value}")
    elif spec["type"] == "str":
        if not isinstance(value, str):
            return _deny("must be a string", f"{path_key}={value}")
        max_len = spec.get("max_len", 256)
        if len(value) > max_len:
            return _deny(f"must be at most {max_len} characters", f"{path_key}={value}")
        if "values" in spec and value not in spec["values"]:
            return _deny(f"invalid value, must be one of {spec['values']}", f"{path_key}={value}")
        pattern = spec.get("pattern")
        if pattern and not re.fullmatch(pattern, value):
            return _deny(f"invalid value for {path_key}", f"{path_key}={value}")
        values_fn = spec.get("values_fn")
        if values_fn and value not in values_fn():
            return _deny(f"invalid value for {path_key}", f"{path_key}={value}")
        validate_fn = spec.get("validate_fn")
        if validate_fn:
            # Resolved OFF the loop and handed down. Every validator here rejects a
            # model pin the account cannot use, and the check behind them reads the
            # configured provider from disk when nobody supplies it -- a file read and
            # a schema validation, inline in this handler, for every one of the five
            # keys that carry a hook. One hop per request, and only for a key that has
            # a validator at all. A ``validate_fn`` added later takes the provider as
            # its third argument for this reason.
            if validate_fn is _validate_role_model:
                # The validator is synchronous and judges the pin against the live
                # snapshot as it stands; revalidate that snapshot first so a
                # startup-race answer cannot deny a pin the account is entitled to.
                # Both steps are scoped to the default harness the pin runs on, so
                # the newest live session of ANOTHER harness (a member DM) is never
                # the evidence that judges it.
                provider, pin_backend = await asyncio.to_thread(_active_provider_and_pin_backend)
                pending = await _revalidate_role_pin_evidence(value, request, backend=pin_backend)
                if pending:
                    return _deny(pending, f"{path_key}={value}")
                reason = validate_fn(value, request, provider, backend=pin_backend)
            else:
                provider = await asyncio.to_thread(_active_provider_name)
                reason = validate_fn(value, request, provider)
            if reason:
                return _deny(reason, f"{path_key}={value}")
    elif spec["type"] == "str_list":
        # The audit resource is the key alone: the refused value can be up to the whole
        # request body, and a list is no shorter in the log than in config.json.
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return _deny("must be a list of strings", path_key)
        max_items, max_item_len = spec["max_items"], spec["max_item_len"]
        if len(value) > max_items:
            return _deny(f"must be at most {max_items} items", path_key)
        if any(len(item) > max_item_len for item in value):
            return _deny(f"each item must be at most {max_item_len} characters", path_key)
    elif spec["type"] == "dict":
        # One-level record written ATOMICALLY as a single value, for settings
        # where multiple scalar fields form one verdict and a partial write is
        # itself the bug (e.g. the update popup's version+snooze+skip record).
        # Strict by design: every declared key present, no undeclared keys,
        # each value validated against its scalar subspec — so this cannot
        # become a generic JSON passthrough.
        if not isinstance(value, dict):
            return _deny("must be an object", f"{path_key}={value}")
        keys_spec = spec["keys"]
        unknown = set(value) - set(keys_spec)
        if unknown:
            return _deny(f"unknown key(s): {sorted(unknown)}", f"{path_key}={value}")
        missing = set(keys_spec) - set(value)
        if missing:
            return _deny(f"missing key(s): {sorted(missing)}", f"{path_key}={value}")
        validated: dict = {}
        for sub_key, sub_spec in keys_spec.items():
            sub_val = value[sub_key]
            if sub_spec["type"] == "str":
                if not isinstance(sub_val, str):
                    return _deny(f"{sub_key} must be a string", f"{path_key}={value}")
                if len(sub_val) > sub_spec.get("max_len", 256):
                    return _deny(
                        f"{sub_key} must be at most {sub_spec.get('max_len', 256)} characters",
                        f"{path_key}={value}",
                    )
            elif sub_spec["type"] == "bool":
                if not isinstance(sub_val, bool):
                    return _deny(f"{sub_key} must be a boolean", f"{path_key}={value}")
            elif sub_spec["type"] == "float":
                # bool is an int subclass; refuse it before coercion so
                # `true` cannot silently store 1.0.
                if isinstance(sub_val, bool):
                    return _deny(f"{sub_key} must be a number", f"{path_key}={value}")
                try:
                    sub_val = float(sub_val)
                except (TypeError, ValueError):
                    return _deny(f"{sub_key} must be a number", f"{path_key}={value}")
                if not math.isfinite(sub_val):
                    return _deny(f"{sub_key} must be a finite number", f"{path_key}={value}")
                lo, hi = sub_spec.get("min", 0.0), sub_spec.get("max", 999999.0)
                if sub_val < lo or sub_val > hi:
                    return _deny(f"{sub_key} must be between {lo} and {hi}", f"{path_key}={value}")
            else:
                return _deny("unsupported config type", f"{path_key}={value}", 500)
            validated[sub_key] = sub_val
        value = validated
    else:
        return _deny("unsupported config type", f"{path_key}={value}", 500)

    # The terminal's default shell must name a program that exists — "" clears
    # the setting (restores the $SHELL / platform default). shutil.which stats
    # every PATH entry, so the probe runs off-loop (same rationale as the
    # governance reads below); the spawn path re-validates at open time and
    # falls back regardless, so this gate is a UX surface, not the safety
    # boundary — it exists to refuse a typo visibly at save time instead of
    # letting it be discovered as a silently different shell on the next
    # terminal open. The body carries a machine-readable `code` (the AGENTS
    # contract for new non-2xx JSON): the Settings field maps it to a catalog
    # key, since rendering this English sentence verbatim would ship an
    # untranslated string into a 12-language dashboard.
    if path_key == "dashboard.terminal.shell" and value.strip():
        resolved = await asyncio.to_thread(shutil.which, value.strip())
        if not resolved:
            _log_sel("denied", f"{path_key}={value}")
            return web.json_response(
                {
                    "error": (
                        "must be an executable shell (an absolute path or a "
                        "command on PATH); leave empty to use the system default"
                    ),
                    "code": "shell_not_executable",
                },
                status=400,
            )

    # ── Governance: refuse a write an enterprise ceiling has pinned ──
    # Only re-ENABLING is refused. Writing `false` is always allowed even under a
    # ceiling that already forbids the beacon: the ceiling is a floor on privacy,
    # so a narrower local choice composes with it (tightest-wins), and refusing it
    # would leave a user unable to record the stricter preference they already have
    # in effect — which would also strand them if the policy were later lifted.
    #
    # The 403 exists so a pinned host cannot be left storing `true` behind a toggle
    # that does nothing: `should_send` already blocks the egress, so without this
    # the config file and the UI would both claim "on" while nothing is ever sent.
    if path_key == "telemetry.beacon_enabled" and value is True:
        # to_thread: resolving the ceiling reads the trust-root policy file and
        # the active profile from disk, which must not block the event loop.
        pinned = await asyncio.to_thread(_beacon_governance_pinned_off)
        if pinned:
            return _deny(
                "telemetry is disabled by your administrator's security policy",
                f"{path_key}={value}",
                403,
            )

    # Local metric collection is offered as local-only ("Nothing is exported"), and
    # that promise has to hold for every state this route can reach. It would not:
    # `_build_recorder` attaches an OTLP reader for every destination the active
    # telemetry provider supplies (see metrics/provider.py), so on a host where
    # egress is configured — through `telemetry.otlp_endpoint` for the default
    # provider, or an edition's own collector — enabling collection from the
    # dashboard would start network egress
    # under a switch that says it does not. Refuse the ENABLE there and let the
    # config file — which is where the endpoint was chosen — be where that decision
    # is made. Disabling stays writable for the same reason as the beacon above: a
    # narrower local choice always composes.
    if path_key == "telemetry.enabled" and value is True:
        try:
            # to_thread: a config load is a fingerprint-cache hit in the steady
            # state, but a full read plus schema validation (~14ms) when the file
            # changed — and this handler runs on the event loop.
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            # Resolved posture, not the raw endpoint string: the DEFAULT provider
            # derives its one destination from telemetry.otlp_endpoint, but an
            # edition may supply its own collector with that key empty. Asking the
            # same resolver _build_recorder uses is what keeps this refusal and
            # the actual egress from disagreeing. It RAISES when posture cannot be
            # established, which the handler below turns into a refusal: reading a
            # transient provider error as "no egress" would permit an enable that
            # the recovered provider then turns into egress.
            egress = await asyncio.to_thread(_metrics_provider.otlp_egress_active, cfg.telemetry)
        except Exception:
            # Unreadable config, or egress posture that could not be resolved:
            # fail closed rather than enabling collection whose egress posture
            # cannot be established.
            logger.warning(
                "telemetry config or egress posture unreadable; refusing to enable",
                exc_info=True,
            )
            return _deny(
                "could not establish the telemetry egress posture",
                f"{path_key}={value}",
                409,
            )
        if egress:
            return _deny(
                "this host is configured to export metrics off the machine, so "
                "enabling collection here would also start that export. Enable it "
                "in the config file instead, where the destination is configured.",
                f"{path_key}={value}",
                409,
            )

    # Same rule, same direction, for the tailnet origin derivation. `false` stays
    # writable under a ceiling that already forbids it, for the same reason as
    # above: the ceiling is a floor, a narrower local choice composes with it, and
    # refusing the write would strand the user if the policy were later lifted.
    # The 403 exists so a pinned host cannot store `true` behind a control that
    # does nothing — `resolve_tailnet_host` already refuses to derive, so without
    # this the config file and the card would both claim "on" while no origin is
    # ever added.
    if (
        path_key in ("dashboard.tailscale.enabled", "dashboard.tailscale.trust_identity")
        and value is True
    ):
        pinned = await asyncio.to_thread(_tailnet_governance_pinned_off)
        if pinned:
            return _deny(
                "tailnet access is disabled by your administrator's security policy",
                f"{path_key}={value}",
                403,
            )

    # Read, update, write — serialized across processes via update_config_locked.
    cfg_path = config_path()
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        parts = path_key.split(".")

        def _mutate_config_patch(data: dict) -> dict | None:
            """Apply a single dotted-key assignment to the raw config dict."""
            if if_unset:
                # Raw presence, not the loaded default: a stored default is a
                # choice too. Check under the write lock so no save can race it.
                existing = data
                for part in parts[:-1]:
                    child = existing.get(part)
                    if not isinstance(child, dict):
                        break
                    existing = child
                else:
                    if parts[-1] in existing:
                        raise _ConfigKeyAlreadySet
            # Walk (creating) intermediate objects, then set the leaf. Handles
            # arbitrary depth uniformly — 1-level ("auto_update"), 2-level
            # ("agent.model"), and 3-level ("agent.role_models.background") —
            # instead of special-cases that would clobber a whole section for a
            # 3-level key.
            section = data
            for part in parts[:-1]:
                nxt = section.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise ValueError(f"config section '{part}' is not an object")
                section = nxt
            section[parts[-1]] = value
            # Saving the old default of an auto-adopting superseded-default row
            # (a 7200 turn limit) is an explicit choice. Ack it here, under the
            # config lock and before the write, so the reload this write triggers
            # does not adopt it away. A failed ack raises OSError and aborts the
            # write, rather than saving a value the next load would delete.
            ack_explicit_write(path_key, value)
            return data

        try:
            await asyncio.to_thread(update_config_locked, cfg_path, mutate=_mutate_config_patch)
        except _ConfigKeyAlreadySet:
            _log_sel("denied", f"{path_key}=already_set")
            return web.json_response(
                {"error": f"{path_key} is already set", "code": "already_set"}, status=409
            )
        except ConfigReadError:
            _log_sel("error", f"{path_key}=read_failed")
            return web.json_response({"error": "failed to read config file"}, status=500)
        except ConfigWriteRefused as exc:
            # A ``ValueError`` too, so it must be caught before the arm below: the
            # publish floor refusing a plaintext ``agent.deepseek_env`` value is the
            # operator's own input being declined (400), not a server failure, and
            # the message -- env-var keys only, never the value -- is the instruction.
            _log_sel("denied", f"{path_key}=write_refused")
            return web.json_response(
                {"error": str(exc), "code": "config_write_refused"}, status=400
            )
        except ValueError as exc:
            _log_sel("error", f"{path_key}=section_not_dict")
            return web.json_response({"error": str(exc)}, status=500)
        except OSError:
            _log_sel("error", f"{path_key}=write_failed")
            return web.json_response({"error": "failed to write config file"}, status=500)

    _log_sel("success", f"{path_key}={value}")

    # Everything a running gateway does in response to this write lives behind
    # ``config.live.subscribe`` (the provider switch and role-model rebuild are
    # registered in ``server.py``; session defaults, subagent budgets and the
    # metrics recorder by their owners), so a dashboard PATCH, ``kirocrew config
    # set`` and an ``$EDITOR`` save all apply identically. Waiting for the cycle
    # here means the masked config returned below is the one already in force.
    await _hot_apply_after_write()

    from kiro_crew.config import live

    applied = live.snapshot()
    if applied is None:
        applied = await asyncio.to_thread(KiroCrewConfig.load)
    if path_key == "agent.acp_backend":
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        prerequisite = request.app.get("kiro_prerequisite_service")
        if (
            isinstance(prerequisite, KiroPrerequisiteService)
            and not prerequisite.initial_setup_complete
        ):
            try:
                await prerequisite.record_independent_backend_setup(applied.agent.acp_backend)
            except Exception:
                logger.warning("Could not record independent backend setup", exc_info=True)
                return web.json_response(
                    {
                        "error": "Agent selection was saved, but setup completion could not be recorded. Try again.",
                        "code": "setup_marker_write_failed",
                        "config_saved": True,
                    },
                    status=503,
                )
    payload = _masked_config_dict(applied)
    if clamp_notice is not None:
        payload["clamp_notice"] = clamp_notice
    return web.json_response(payload)
