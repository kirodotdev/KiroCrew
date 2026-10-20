"""The config document the dashboard reads and replaces, and the masked view it is shown as.

``GET``/``PUT /api/config/kirocrew`` answer with the config masked for a browser:
schema-sensitive values and agent-writable free text are replaced by a sentinel.
A write runs one config-watcher cycle before the route answers, so every hot
applier has seen it.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Iterable

from aiohttp import web

import kiro_crew.config.resolution as _resolution
from kiro_crew.config.loader import (
    MAX_SUBAGENTS_FIXED_FLOOR,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    KiroCrewConfig,
)
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.core_security import _sel
from kiro_crew.external_text import redact_external_text as _redact_external

logger = logging.getLogger("kiro_crew.dashboard.handlers.core")


# Sentinel returned in place of sensitive config values in API responses. Kept
# distinct from "" so the UI can render a "set (hidden)" placeholder.
_SENSITIVE_MASK = "••••••••"


# Agent-record fields that carry agent- or package-writable FREE TEXT. An agent
# can edit ``config.json`` directly, and agent sync copies ``description``
# straight off a discovered agent spec, so a third-party package controls these
# strings. They are not schema-``sensitive`` (they are not secrets the OWNER
# stored), so the schema-driven walk in ``_masked_config_dict`` never touches
# them — this named list is what closes that gap on the config endpoint.
#
# Scope: every ``str`` field of ``KiroCrewAgentConfig`` whose LOAD path does not
# pin its shape. ``reasoning_effort`` (``coerce_effort`` collapses anything but
# a known level to ``""``) and ``session_color`` (``_safe_color`` pins to
# ``#rrggbb``) are excluded because their guards already refuse redactable
# content; everything else — including fields with only an isinstance-str
# guard, which constrains type but not content — is in. A test enumerates the
# dataclass's ``str`` fields against this tuple plus that exception set, so a
# newly added free-text field fails loudly instead of shipping unmasked.
#
# The record KEY (the agent name) is handled separately in the pass below:
# a suspicious-keyed record is REMOVED from the browser-facing view (masking a
# key would collide two suspicious records into one entry), and the
# name-reference fields that could still spell it are masked. The create route
# does not refuse a credential-shaped name, so this view cannot assume one never
# arrives.
_AGENT_UNTRUSTED_TEXT_FIELDS = (
    "member_id",
    "display_name",
    "description",
    "triggers",
    "kiro_agent",
    "workspace",
    "memory_store",
    "model",
    "worker_model",
    "source",
    "telegram_account",
)


def _mask_agent_free_text(value: object) -> object:
    """Render ONE agent-record free-text value for the config response.

    Same rule the roster endpoint's rows need: a value the
    redactors would alter — credential- or exfiltration-URL-shaped text — is
    replaced WHOLESALE by ``_SENSITIVE_MASK``; a non-string is masked too (it
    is not renderable content; the loader pins every declared-``str`` agent
    field except ``memory_store`` and ``member_id`` to its default, so for the
    rest this branch is defence in depth, not the only guard). Benign content passes through
    byte-identical, so an ordinary stored value renders exactly as written.
    ``GET /api/agents`` ships these fields verbatim — that half of the class is
    not this endpoint's.

    A fixed sentinel rather than an in-place scrub: a scrubbed view is a
    FUNCTION of the stored value, so any future write-side "treat the mask as
    unchanged" rule would have to recompute the transform and breaks under
    redaction-chain drift or a stale view; the sentinel is recognizable
    regardless of either. Named cost: a value containing one credential-shaped
    token is masked entirely, the same trade ``_masked_config_dict`` already
    makes for schema-sensitive values.

    Keyed on ``_redact_external`` itself rather than a second detector so this
    rule and the roster's cannot drift apart.
    """
    if not isinstance(value, str):
        return _SENSITIVE_MASK
    # No falsy pre-check on purpose: ``_redact_external`` returns falsy input
    # unchanged, so ``""`` compares equal and passes through — a ``value and``
    # guard here would only look like the fail-open bug class without being it.
    if _redact_external(value) != value:
        return _SENSITIVE_MASK
    return value


def _masked_config_dict(cfg: KiroCrewConfig) -> dict:
    """Return ``cfg.to_dict()`` with sensitive string values masked.

    Applied ONLY to the GET /api/config/kirocrew response — never to the value
    ``cfg.to_dict()`` / ``cfg.save()`` serialize, since masking there would
    persist the sentinel and destroy the real secret (e.g. ``telegram.bot_token``).
    Safe here because no config write endpoint accepts sensitive fields; if one
    is ever added it MUST treat ``_SENSITIVE_MASK`` as "unchanged" and keep the
    stored value. Sensitivity is schema-driven (``sensitive=True`` field
    metadata), so newly added sensitive fields are masked automatically.

    Two masking passes. The schema walk covers owner-stored secrets
    (``sensitive=True``). A second pass covers agent-record free text
    (``_AGENT_UNTRUSTED_TEXT_FIELDS``): those values are agent- and
    package-writable, so a credential- or exfiltration-URL-shaped one is
    masked wholesale (``_mask_agent_free_text``) instead of shipping to the
    browser verbatim. The write-side note above holds for this pass too:
    neither branch of this endpoint can echo the mask into storage — the
    PATCH allowlist (``_EDITABLE_CONFIG``) names no ``agents.*`` path, and
    the PUT branch reads only the singular ``agent`` section against a
    hardcoded key list. The agents CRUD route is the write path for these
    fields; its read pair is ``GET /api/agents``, which ships them verbatim —
    that half of the class needs the same mask plus a mask-means-unchanged
    write rule this endpoint does not need. Named cost of the wider field set: the overview's config tab renders
    ``kiro_agent``/``workspace``/``memory_store`` and cross-references the
    latter two against the workspace and store lists, so a masked value breaks
    that "used by" row — but only for a record whose value is already
    credential-shaped, and therefore already meaningless as a reference.
    """
    from kiro_crew.config.schema import JSON_SCHEMA
    from kiro_crew.config.validation import _is_sensitive_path

    masked = copy.deepcopy(cfg.to_dict())

    # Drop unknown/edition-contributed top-level sections (KiroCrewConfig.
    # _extra_sections) from the API response entirely. They exist ONLY for the
    # save() round-trip; the core does not model them, so they are absent from
    # the schema and the sensitivity walk below (which is schema-driven) cannot
    # know which of their values are secrets. Returning them verbatim to the
    # dashboard would leak any credential an edition stored in its own section.
    # to_dict()/save() still carry them — only this browser-facing view omits
    # them. (An edition that needs to surface its config in the dashboard does
    # so through its own masked route, not this core endpoint.)
    for _extra_key in getattr(cfg, "_extra_sections", {}):
        masked.pop(_extra_key, None)

    # Same reasoning one level down (KiroCrewConfig._extra_keys): an unknown key
    # captured INSIDE a modelled section — or inside a named agents/workspaces/
    # memory_stores record — is absent from the schema too, so the sensitivity
    # walk below cannot recognize it either; a credential a previous build stored
    # under a since-renamed key (`slack.legacy_bot_token`) would ship verbatim.
    # Preserving it for save() is the point of the capture; showing it to the
    # browser is not.
    _resolution.drop_extra_section_keys(masked, getattr(cfg, "_extra_keys", {}))

    def _walk(node: object, prefix: str) -> None:
        if isinstance(node, dict):
            for key, val in list(node.items()):
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(val, dict):
                    _walk(val, path)
                elif isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            _walk(item, path)
                elif isinstance(val, str) and val and _is_sensitive_path(JSON_SCHEMA, path):
                    node[key] = _SENSITIVE_MASK

    _walk(masked, "")

    # Second pass: agent-record free text. These fields are absent from the
    # schema's sensitive set by design (they are not owner secrets), so the walk
    # above cannot cover them; see _AGENT_UNTRUSTED_TEXT_FIELDS. Both response
    # sites of this endpoint (the GET body and the PATCH echo) funnel through
    # this one function, so this pass gives the redaction rule surface coverage
    # here rather than point coverage.
    #
    # The record KEY (the agent name) is handled by REMOVAL, not masking: agent
    # sync stores a discovered agent's name as this dict's key, so a
    # credential-shaped package name would ship verbatim as a key — and masking
    # a key would collide two suspicious records into one entry. Dropping the
    # record from this browser-facing view (save() still carries it) leaks
    # nothing and collides nothing; the name-reference fields that could still
    # spell the removed name (``default_agent``, ``session.pool_agent``) are
    # masked when they match. Named cost: a suspicious-keyed record is invisible
    # in the config tab — the same trade the roster's project rows make, and the
    # name was never renderable content.
    agents = masked.get("agents")
    if isinstance(agents, dict):
        removed: set[object] = set()
        for name in list(agents.keys()):
            if not isinstance(name, str) or _mask_agent_free_text(name) != name:
                agents.pop(name)
                removed.add(name)
                continue
            record = agents[name]
            if not isinstance(record, dict):
                continue
            for field_name in _AGENT_UNTRUSTED_TEXT_FIELDS:
                if field_name in record:
                    record[field_name] = _mask_agent_free_text(record[field_name])
        if removed:
            if masked.get("default_agent") in removed:
                masked["default_agent"] = _SENSITIVE_MASK
            session_section = masked.get("session")
            if isinstance(session_section, dict) and session_section.get("pool_agent") in removed:
                session_section["pool_agent"] = _SENSITIVE_MASK
    return masked


# ── Kiro Crew Config API ──
# The security-relevant ceilings (SUBAGENT_AUTO_MAX_CEILING,
# SUBAGENT_MAX_TURNS_CEILING) are imported from ``config.loader`` — the single
# source of truth shared by this API-write gate and the loader's load-time
# clamp, so the two cannot drift apart. subagent_auto_max is the security cap
# that bounds max_subagents, so it needs its own hard upper bound to stop a
# caller raising it arbitrarily (e.g. {"subagent_auto_max": 9999}) to bypass
# the concurrency limit.


def _changed_paths_need_restart(changed: Iterable[str]) -> bool:
    """Whether any of the dotted *changed* paths is declared ``restart=True``.

    The schema metadata is the ONE statement of which fields a running gateway
    cannot adopt; every other field is hot-applied by the config watcher, so a
    handler never keeps its own list of boot-only keys. ``changed`` must hold
    only paths whose value actually moved -- the dashboard sends every setting on
    each save, so "was applied" is not "was changed".
    """
    from kiro_crew.config.schema import requires_restart

    return any(requires_restart(p) for p in changed)


async def _hot_apply_after_write() -> None:
    """Run one watcher cycle so the handler answers after the cycle has dispatched.

    With the watcher started this is the same path a CLI or ``$EDITOR`` write
    takes, only synchronous. Every applier the cycle awaits has run when this
    returns; the two that deliberately run off the cycle -- a channel reconnect
    and a provider switch -- are scheduled by it and may still be in flight when
    the handler answers. Before boot arms the watcher (or in a test that never
    did) the loader's cache drop already makes the next ``load()`` see the
    write, so there is nothing further to do.
    """
    from kiro_crew.config import live

    w = live.watch()
    if not w.started:
        return
    try:
        await w.refresh_now()
    except Exception:
        logger.exception("config hot-apply after write failed; next poll retries")


async def api_kirocrew_config(request: web.Request) -> web.Response:
    """GET/PUT /api/config/kirocrew — read or update Kiro Crew config."""
    # Re-imported at call time (not reused from the module-level binding) so a
    # test that redirects ``kiro_crew.config.loader.config_path`` at a temp path
    # is observed by this handler.
    from kiro_crew.config.loader import config_path  # noqa: F811

    if request.method == "PUT":
        denied = await require_owner_dashboard_request(request, "config.update")
        if denied is not None:
            return denied

        caller = request.get("user", "dashboard")

        def _deny(error: str, status: int = 400, *, code: str | None = None) -> web.Response:
            _sel().log_api_access(
                caller=caller,
                operation="config.update",
                outcome="denied",
                error=error,
            )
            payload: dict[str, str] = {"error": error}
            if code is not None:
                payload["code"] = code
            return web.json_response(payload, status=status)

        try:
            body = await request.json()
        except Exception:
            return _deny("invalid JSON")
        agent_settings = body.get("agent")
        if not isinstance(agent_settings, dict):
            return _deny("agent must be an object")
        cfg_path = config_path()
        # Validate-only (CPU-bound) before acquiring the lock — fail fast on
        # obviously-bad input so the lock hold is as short as possible.
        # The actual read-modify-write is serialised under _get_config_lock and
        # offloaded to a thread so it neither races concurrent writers (lost-write
        # bug) nor blocks the event loop (event-loop-stall bug).  This mirrors the
        # pattern used by the sibling PATCH handler (``core_config_patch.py``).
        from kiro_crew.config.loader import (  # noqa: F811
            ConfigReadError,
            ConfigWriteRefused,
            update_config_locked,
        )
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        # Carry the validation error and result out of the mutate callback.
        # Validation that depends on the *persisted* ceiling (max_subagents bound)
        # runs inside the callback where it can read the current config; the
        # callback also emits the "no recognized settings provided" 400, so no
        # pre-lock key-recognition check is needed.
        _validation_error: list[tuple[str, int]] = []
        _result: dict[str, object] = {}

        def _mutate_config_put(data: dict) -> dict | None:
            if not isinstance(data.get("agent"), dict):
                data["agent"] = {}
            agent = data["agent"]
            # Snapshot BEFORE mutation for the restart-hint truthfulness guard.
            # The dashboard sends all settings on every save so "was applied" !=
            # "was changed" — see the no-op-save comments in messaging.py.
            before = dict(agent)

            limits = {"subagent_max_turns": SUBAGENT_MAX_TURNS_CEILING}
            applied: list[str] = []
            for key, upper in limits.items():
                if key in agent_settings:
                    val = agent_settings[key]
                    if isinstance(val, bool) or not isinstance(val, int) or val < 1 or val > upper:
                        _validation_error.append(
                            (f"{key} must be an integer between 1 and {upper}", 400)
                        )
                        return None
                    agent[key] = val
                    applied.append(key)

            # Capture the hard cap from the *persisted* config BEFORE applying any
            # subagent_auto_max from this request — deny-by-default prevents a
            # same-request ceiling-raise+spend.
            persisted_hard_cap = agent.get("subagent_auto_max", 16)
            if (
                not isinstance(persisted_hard_cap, int)
                or isinstance(persisted_hard_cap, bool)
                or persisted_hard_cap < 3
            ):
                persisted_hard_cap = 16
            persisted_hard_cap = min(persisted_hard_cap, SUBAGENT_AUTO_MAX_CEILING)

            if "subagent_auto_max" in agent_settings:
                val = agent_settings["subagent_auto_max"]
                if (
                    isinstance(val, bool)
                    or not isinstance(val, int)
                    or val < 3
                    or val > SUBAGENT_AUTO_MAX_CEILING
                ):
                    _validation_error.append(
                        (
                            "subagent_auto_max must be an integer between 3 and "
                            f"{SUBAGENT_AUTO_MAX_CEILING}",
                            400,
                        )
                    )
                    return None
                agent["subagent_auto_max"] = val
                applied.append("subagent_auto_max")

            if "max_subagents" in agent_settings:
                val = agent_settings["max_subagents"]
                hard_cap = persisted_hard_cap
                if (
                    isinstance(val, bool)
                    or not isinstance(val, int)
                    or (val != 0 and not (MAX_SUBAGENTS_FIXED_FLOOR <= val <= hard_cap))
                ):
                    _validation_error.append(
                        (
                            f"max_subagents must be 0 (auto) or an integer between "
                            f"{MAX_SUBAGENTS_FIXED_FLOOR} and {hard_cap}",
                            400,
                        )
                    )
                    return None
                agent["max_subagents"] = val
                applied.append("max_subagents")

            if not applied:
                _validation_error.append(("no recognized settings provided", 400))
                return None

            restart_required = _changed_paths_need_restart(
                f"agent.{key}" for key in applied if agent.get(key) != before.get(key)
            )
            _result["applied"] = applied
            _result["restart_required"] = restart_required
            return data

        try:
            async with _get_config_lock():
                try:
                    await asyncio.to_thread(
                        update_config_locked, cfg_path, mutate=_mutate_config_put
                    )
                except ConfigReadError:
                    _sel().log_api_access(
                        caller=caller,
                        operation="config.update",
                        outcome="error",
                        error="config.json is corrupt",
                    )
                    return web.json_response(
                        {"error": "config.json is corrupt", "code": "config_corrupt"},
                        status=500,
                    )
                except ConfigWriteRefused as exc:
                    # The publish floor refused the document this write would land
                    # (a plaintext ``agent.deepseek_env`` value the write introduces
                    # or keeps while changing that mapping). The message names
                    # env-var keys only, never the value, and is the same
                    # instruction the CLI prints; nothing reached disk.
                    return _deny(str(exc), 400, code="config_write_refused")

                if _validation_error:
                    msg, status = _validation_error[0]
                    return _deny(msg, status)

                applied: list[str] = _result["applied"]  # type: ignore[assignment]
                _sel().log_api_access(
                    caller=caller,
                    operation="config.update",
                    outcome="ok",
                    resources=",".join(applied),
                )
        except OSError:
            _sel().log_api_access(
                caller=caller,
                operation="config.update",
                outcome="error",
                error="config.json write failed",
            )
            return web.json_response(
                {"error": "failed to write config file", "code": "config_write_failed"},
                status=500,
            )

        restart_required: bool = _result["restart_required"]  # type: ignore[assignment]
        await _hot_apply_after_write()
        return web.json_response({"ok": True, "restart_required": restart_required})

    cfg = KiroCrewConfig.load()
    return web.json_response(_masked_config_dict(cfg))
