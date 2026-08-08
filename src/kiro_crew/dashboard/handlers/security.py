"""Denied-commands REST API — Settings > Security opt-out surface.

The 6 CRUD endpoints let a user disable/enable individual built-in denied
commands, disable them all at once, and add/remove their own patterns. Opt-out
state lives in the KEYSTONE file ``<config_dir>/denied_commands.json`` (on
``security._SENSITIVE_HOME_DIRS`` — the agent cannot read OR write it), NOT in
the agent-readable ``config.json``. The file root IS the opt-out object:

    {
      "disable_all": false,
      "disabled_ids": ["<builtin-rule-id>", ...],
      "user_added": [{"id": "user-xxxx", "pattern": "rm -rf /tmp/mine",
                      "enabled": true}]
    }

Mutations run under the shared config lock, write atomically (0600), and emit a
SEL audit entry (``ok`` on success, ``denied`` on reject). Governance
``commands``-scope pins force a built-in rule enabled even when the user disabled
it or set disable-all (tightest-wins): a pinned rule cannot be turned off (409)
and always counts as enabled in the snapshot.

All file I/O is offloaded to a thread executor (``build_denied_commands_snapshot_async``
+ ``_write_denied_state``) so the async handlers never block the gateway event
loop. Every endpoint (GET + all mutations) returns the full refreshed snapshot.

The module also hosts two adjacent Security-page surfaces: the read-only
governance policy viewer, and the per-app third-party trust grants
(``/api/security/trusted-apps``). Grants are the one write surface here that
targets the agent-writable ``config.json`` rather than the keystone file — see
the section comment for why that inversion is deliberate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path

from aiohttp import web

from kiro_crew.apps.execution import APP_NAME_RE, builtin_app_names, trusted_app_names
from kiro_crew.apps.manager import disable_app, get_app, list_apps
from kiro_crew.apps.registry import get_registry_app
from kiro_crew.apps.routes import app_lifecycle_lock
from kiro_crew.apps.teardown import teardown_app_runtime
from kiro_crew.config.loader import (
    KiroCrewConfig,
    _invalidate_config_cache,
    config_local_path,
    config_path,
    denied_commands_path,
    write_config_atomically,
)
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.executors import governance_executor
from kiro_crew.platform.context import current_context
from kiro_crew.platform.governance import (
    _SCOPE_ALIASES,
    CAPABILITY,
    ORDINAL,
    RULESET,
    SCOPE_CATALOG,
    SCOPEDMAP,
    CapabilityGate,
    GovernanceCeiling,
    OrdinalControl,
    ScopedMap,
    ScopedRuleset,
    _AndRuleset,
    _compose_controls,
)
from kiro_crew.platform.governance_profiles import (
    HOST_SESSION_KEY,
    bound_surfaces,
    resolve_active_scope,
)

logger = logging.getLogger(__name__)

_MAX_PATTERN_LEN = 512


def _sel():
    """Late-binding ``_sel()`` for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


def _audit(request: web.Request, *, operation: str, outcome: str, resources: str = "") -> None:
    """Best-effort SEL audit; a logging failure never breaks the request."""
    try:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


class ConfigCorruptError(Exception):
    """denied_commands.json exists but is unreadable/not-a-dict — refuse to mutate.

    A mutation that read a corrupt file as ``{}`` and wrote it back would
    silently reset the opt-out state. The write path raises this so the handler
    returns 500 instead of clobbering a populated-but-unparseable file.
    """


def _read_denied_data() -> dict:
    """Read denied_commands.json, tolerant of a missing/corrupt file (``{}``).

    For the READ/snapshot path only — a corrupt file degrades to empty state so
    GET still renders. Mutations MUST use :func:`_read_denied_strict`. The file
    root IS the opt-out object (``{disable_all, disabled_ids, user_added}``).
    """
    path = denied_commands_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.warning("denied_commands.json unreadable/corrupt; treating as empty", exc_info=True)
        return {}


def _read_denied_strict() -> dict:
    """Read denied_commands.json for a MUTATION: raise on corrupt, ``{}`` if absent."""
    path = denied_commands_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigCorruptError(str(exc)) from exc
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigCorruptError(f"denied_commands.json is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigCorruptError("denied_commands.json top level is not a JSON object")
    return loaded


def _denied_state(data: dict) -> dict:
    """Normalize the denied_commands.json object with defaults filled.

    Defensive against a hand-edited file (mirrors ``HooksConfig.from_dict``):
    ``disabled_ids`` is filtered to non-empty strings so a malformed entry (e.g.
    ``[{}]``) can't later raise ``TypeError: unhashable type: 'dict'`` when the
    snapshot builds ``set(...)``, and ``disable_all`` goes through
    ``_coerce_bool`` so a hand-typed ``"false"`` (truthy under plain ``bool()``)
    does not silently disable everything. Fail safe: unknown junk → False.

    *data* is the file root (the opt-out object itself), not a config wrapper.
    """
    from kiro_crew.hooks import _coerce_bool

    denied = data if isinstance(data, dict) else {}
    disabled_ids = denied.get("disabled_ids", [])
    if not isinstance(disabled_ids, list):
        disabled_ids = []
    user_added = denied.get("user_added", [])
    return {
        "disable_all": _coerce_bool(denied.get("disable_all", False), default=False),
        "disabled_ids": [i for i in disabled_ids if isinstance(i, str) and i],
        "user_added": list(user_added) if isinstance(user_added, list) else [],
    }


def _user_rule_ids() -> set:
    """Set of existing user-rule ids, tolerant of malformed entries.

    A hand-edited file can hold a malformed ``user_added`` entry (e.g. ``{}``
    or one missing ``id``); those are skipped rather than raising ``KeyError`` —
    so an unknown id yields a clean 404, never a 500. Synchronous (reads the
    keystone file) — async handlers MUST use :func:`_user_rule_ids_async`.
    """
    ids = set()
    for u in _denied_state(_read_denied_data())["user_added"]:
        if isinstance(u, dict):
            uid = u.get("id")
            if isinstance(uid, str) and uid:
                ids.add(uid)
    return ids


async def _run_off_loop(fn):
    """Run a blocking (filesystem) callable in the default thread executor.

    The denied-command lookups read ``denied_commands.json`` and walk the
    governance profile store — blocking I/O that must not run on aiohttp's sole
    event loop (a slow/stalled FS would freeze every request + heartbeat).
    """
    return await asyncio.get_running_loop().run_in_executor(None, fn)


async def _user_rule_ids_async() -> set:
    """`_user_rule_ids` off the event loop (keystone file read)."""
    return await _run_off_loop(_user_rule_ids)


async def _pinned_ids_for_snapshot_async() -> set:
    """`security.pinned_builtin_command_ids_for_snapshot` off the event loop.

    Walks the governance profile store (filesystem) — offloaded so a stalled FS
    cannot block the gateway loop from the builtin-toggle 409 check.
    """
    from kiro_crew.security import pinned_builtin_command_ids_for_snapshot

    return await _run_off_loop(pinned_builtin_command_ids_for_snapshot)


def build_denied_commands_snapshot() -> dict:
    """Compute the full snapshot returned by every endpoint.

    ``enabled = pinned OR (not disable_all AND id not in disabled_ids)``;
    ``governance_locked = len(pinned_builtin_command_ids()) > 0``;
    ``effective_count = #enabled builtins + #enabled user_added``.
    """
    from kiro_crew.security import builtin_denied_rules, pinned_builtin_command_ids_for_snapshot

    state = _denied_state(_read_denied_data())
    disable_all = state["disable_all"]
    disabled_ids = set(state["disabled_ids"])
    # Snapshot is surface-agnostic → union pins across ALL profiles so a
    # profile-pinned rule renders locked (never a no-op opt-out). The ENFORCEMENT
    # gate uses the ctx-scoped pinned_builtin_command_ids + bound-profile plane,
    # so this display-only union does not widen enforcement.
    pinned = pinned_builtin_command_ids_for_snapshot()

    builtins: list[dict] = []
    for rule in builtin_denied_rules():
        rid = rule["id"]
        is_pinned = rid in pinned
        enabled = is_pinned or (not disable_all and rid not in disabled_ids)
        builtins.append(
            {
                "id": rid,
                "pattern": rule["pattern"],
                "category": rule["category"],
                "description": rule["description"],
                "enabled": enabled,
                "pinned": is_pinned,
            }
        )

    from kiro_crew.hooks import _coerce_bool

    user_added: list[dict] = []
    for entry in state["user_added"]:
        if not isinstance(entry, dict):
            continue
        user_added.append(
            {
                "id": str(entry.get("id", "")),
                "pattern": str(entry.get("pattern", "")),
                # _coerce_bool (not bool()): a hand-typed "enabled": "false" is
                # truthy under bool(); mirror from_dict so the snapshot's enabled
                # flag matches what the gate actually enforces.
                "enabled": _coerce_bool(entry.get("enabled", True), default=True),
            }
        )

    effective_count = sum(1 for b in builtins if b["enabled"]) + sum(
        1 for u in user_added if u["enabled"]
    )
    return {
        "builtins": builtins,
        "user_added": user_added,
        "disable_all": disable_all,
        "effective_count": effective_count,
        "governance_locked": bool(pinned),
    }


def count_effective_denied_commands() -> int:
    """Return the number of effectively-enabled denied commands (builtins + user).

    Synchronous — reads the keystone file + governance profiles. Async callers on
    the gateway event loop MUST offload it (see ``build_denied_commands_snapshot_async``).
    """
    snapshot = build_denied_commands_snapshot()
    return snapshot["effective_count"]


async def build_denied_commands_snapshot_async() -> dict:
    """Build the snapshot off the event loop.

    ``build_denied_commands_snapshot`` reads ``denied_commands.json`` and walks
    the governance profile store — blocking filesystem I/O. Running it inline in
    an async handler would stall aiohttp's sole event loop (and every heartbeat)
    on a slow/stalled FS, so it is offloaded to the default thread executor.
    """
    return await asyncio.get_running_loop().run_in_executor(None, build_denied_commands_snapshot)


async def _snapshot_response() -> web.Response:
    return web.json_response(await build_denied_commands_snapshot_async())


async def _write_denied_state(mutate) -> dict:
    """Read-modify-write the keystone ``denied_commands.json`` atomically.

    ``mutate(denied: dict) -> None`` edits the opt-out object (the file root) in
    place. Runs under the shared config lock. Returns the updated object so the
    caller can hot-reload the live HookManager. The file is written 0600 (owner-
    only, like other keystone secrets).

    The blocking read-modify-write (disk read, JSON (de)serialize, atomic
    replace) runs in a thread executor so it never stalls the gateway event
    loop; the async config lock still serializes concurrent mutations.
    """
    from kiro_crew.agent import _atomic_json_write
    path: Path = denied_commands_path()

    def _read_modify_write() -> dict:
        denied = _read_denied_strict()
        mutate(denied)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(path, denied)
        # Keystone file: restrict to owner (best-effort; matches other secrets).
        try:
            from kiro_crew.platform_compat import chmod_safe

            chmod_safe(path, 0o600)
        except Exception:
            logger.debug("could not chmod denied_commands.json to 0600", exc_info=True)
        return denied

    async with _get_config_lock():
        # ConfigCorruptError raised in the executor propagates through await.
        return await asyncio.get_running_loop().run_in_executor(None, _read_modify_write)


def _reload_live_hooks(request: web.Request, denied_state: dict) -> None:
    """Hot-reload the running HookManager so the opt-out takes effect NOW.

    The PreToolUse gate reads ``HookManager._config`` (built once at gateway
    boot); without this refresh a Settings>Security change would not enforce
    until the gateway restarted — a newly-added user deny would provide no
    protection, and an opt-out would stay enforced. The live manager's existing
    flat hook keys are preserved; only the denied-command opt-out fields are
    replaced from *denied_state* (the keystone file's new content). Best-effort:
    a missing context builder (e.g. in a unit test harness) is a no-op.
    """
    import dataclasses

    from kiro_crew.hooks import HooksConfig

    try:
        state = request.app["state"]
        builder = getattr(state, "context_builder", None)
        manager = getattr(builder, "hooks", None)
        if manager is None:
            return
        # Reparse ONLY the opt-out fields from the keystone state and splice them
        # onto the live config so the flat hook keys (auto_replies, transforms,
        # auto_approve_tools, …) are not lost.
        parsed = HooksConfig.from_dict({"denied_commands": denied_state})
        current = getattr(manager, "_config", None)
        if isinstance(current, HooksConfig):
            manager.reload(
                dataclasses.replace(
                    current,
                    denied_commands_disabled_ids=parsed.denied_commands_disabled_ids,
                    denied_commands_disable_all=parsed.denied_commands_disable_all,
                    denied_commands_user_added=parsed.denied_commands_user_added,
                )
            )
        else:
            manager.reload(parsed)
    except Exception:
        logger.warning(
            "failed to hot-reload HookManager after denied-commands change", exc_info=True
        )


async def _apply_mutation(request: web.Request, op: str, mutate) -> web.Response | None:
    """Run a mutation + write; on a corrupt denied_commands.json return 500.

    Returns ``None`` on success (caller then returns the snapshot), or a 500
    ``web.Response`` when the file is corrupt — so we never overwrite a
    populated-but-unparseable opt-out file. On success the live HookManager is
    hot-reloaded so the change enforces without a restart.
    """
    try:
        denied_state = await _write_denied_state(mutate)
    except ConfigCorruptError as exc:
        _audit(request, operation=op, outcome="denied", resources="config_corrupt")
        logger.error("refusing denied-commands mutation: %s", exc)
        return web.json_response(
            {"error": "denied_commands.json is corrupt; fix it before changing security settings"},
            status=500,
        )
    _reload_live_hooks(request, denied_state)
    return None


# ── GET ──


async def api_denied_commands_list(request: web.Request) -> web.Response:
    """GET /api/security/denied-commands — full snapshot (no audit; read)."""
    return await _snapshot_response()


# ── builtin toggle ──


async def api_denied_command_builtin_toggle(request: web.Request) -> web.Response:
    """PATCH /api/security/denied-commands/builtins/{id} — {enabled: bool}."""
    from kiro_crew.security import builtin_denied_rules

    op = "security.denied_commands.builtin_toggle"
    rule_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=invalid_json")
        return web.json_response({"error": "invalid JSON"}, status=400)

    # A valid-but-non-object body (e.g. `[]`) must yield a clean 400, not a 500.
    if not isinstance(body, dict):
        body = {}
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=bad_type")
        return web.json_response({"error": "enabled must be a boolean"}, status=400)

    if rule_id not in {r["id"] for r in builtin_denied_rules()}:
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=unknown")
        return web.json_response({"error": "unknown builtin rule"}, status=404)

    # Snapshot-scoped (all-profile union) to match what the UI renders locked:
    # a rule shown pinned must reject a disable with 409, not silently 200.
    # Offloaded — walks the governance profile store (FS) off the event loop.
    if not enabled and rule_id in await _pinned_ids_for_snapshot_async():
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=pinned")
        return web.json_response(
            {"error": "rule is enforced by governance policy and cannot be disabled"},
            status=409,
        )

    def _mutate(denied: dict) -> None:
        current = denied.get("disabled_ids")
        current = list(current) if isinstance(current, list) else []
        if enabled:
            current = [rid for rid in current if rid != rule_id]
        elif rule_id not in current:
            current.append(rule_id)
        denied["disabled_ids"] = current

    err = await _apply_mutation(request, op, _mutate)
    if err is not None:
        return err
    _audit(request, operation=op, outcome="ok", resources=f"{rule_id}={enabled}")
    return await _snapshot_response()


# ── disable-all ──


async def api_denied_commands_disable_all(request: web.Request) -> web.Response:
    """PATCH /api/security/denied-commands/disable-all — {value: bool}."""
    op = "security.denied_commands.disable_all"
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        body = {}
    value = body.get("value")
    if not isinstance(value, bool):
        _audit(request, operation=op, outcome="denied", resources="bad_type")
        return web.json_response({"error": "value must be a boolean"}, status=400)

    def _mutate(denied: dict) -> None:
        denied["disable_all"] = value

    err = await _apply_mutation(request, op, _mutate)
    if err is not None:
        return err
    _audit(request, operation=op, outcome="ok", resources=str(value))
    return await _snapshot_response()


# ── user add ──


async def api_denied_command_user_add(request: web.Request) -> web.Response:
    """POST /api/security/denied-commands/user — {pattern: str}."""
    op = "security.denied_commands.user_add"
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        body = {}
    pattern = body.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        _audit(request, operation=op, outcome="denied", resources="empty")
        return web.json_response({"error": "pattern must be a non-empty string"}, status=400)
    if len(pattern) > _MAX_PATTERN_LEN:
        _audit(request, operation=op, outcome="denied", resources="oversize")
        return web.json_response(
            {"error": f"pattern must be at most {_MAX_PATTERN_LEN} characters"}, status=400
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        _audit(request, operation=op, outcome="denied", resources="bad_regex")
        return web.json_response({"error": f"invalid regex: {exc}"}, status=400)

    # Reject catastrophic-backtracking (ReDoS) patterns before they can enter the
    # effective set: the gate runs user regexes synchronously on the event loop,
    # so an unsafe pattern like ``(a+)+$`` would freeze the gateway.
    from kiro_crew.security import is_safe_user_regex

    if not is_safe_user_regex(pattern):
        _audit(request, operation=op, outcome="denied", resources="redos_unsafe")
        return web.json_response(
            {"error": "pattern rejected: unsafe (catastrophic-backtracking) regex"},
            status=400,
        )

    rule_id = "user-" + uuid.uuid4().hex[:12]

    def _mutate(denied: dict) -> None:
        current = denied.get("user_added")
        current = list(current) if isinstance(current, list) else []
        current.append({"id": rule_id, "pattern": pattern, "enabled": True})
        denied["user_added"] = current

    err = await _apply_mutation(request, op, _mutate)
    if err is not None:
        return err
    _audit(request, operation=op, outcome="ok", resources=f"{rule_id}={pattern}")
    return await _snapshot_response()


# ── user toggle / delete ──


async def api_denied_command_user_toggle(request: web.Request) -> web.Response:
    """PATCH /api/security/denied-commands/user/{id} — {enabled: bool}."""
    op = "security.denied_commands.user_toggle"
    rule_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=invalid_json")
        return web.json_response({"error": "invalid JSON"}, status=400)

    # A valid-but-non-object body (e.g. `[]`) must yield a clean 400, not a 500.
    if not isinstance(body, dict):
        body = {}
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=bad_type")
        return web.json_response({"error": "enabled must be a boolean"}, status=400)

    if rule_id not in await _user_rule_ids_async():
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=unknown")
        return web.json_response({"error": "unknown user rule"}, status=404)

    def _mutate(denied: dict) -> None:
        entries = denied.get("user_added", [])
        if not isinstance(entries, list):
            return
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == rule_id:
                entry["enabled"] = enabled

    err = await _apply_mutation(request, op, _mutate)
    if err is not None:
        return err
    _audit(request, operation=op, outcome="ok", resources=f"{rule_id}={enabled}")
    return await _snapshot_response()


async def api_denied_command_user_delete(request: web.Request) -> web.Response:
    """DELETE /api/security/denied-commands/user/{id}."""
    op = "security.denied_commands.user_delete"
    rule_id = request.match_info["id"]

    if rule_id not in await _user_rule_ids_async():
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=unknown")
        return web.json_response({"error": "unknown user rule"}, status=404)

    def _mutate(denied: dict) -> None:
        current = denied.get("user_added", [])
        if not isinstance(current, list):
            current = []
        denied["user_added"] = [
            e for e in current if not (isinstance(e, dict) and e.get("id") == rule_id)
        ]

    err = await _apply_mutation(request, op, _mutate)
    if err is not None:
        return err
    _audit(request, operation=op, outcome="ok", resources=rule_id)
    return await _snapshot_response()


# ──────────────────────────────────────────────────────────────────────────
# Per-app third-party trust grants — Settings > Security opt-IN surface
# ──────────────────────────────────────────────────────────────────────────
# The narrow counterpart to ``agent.apps_allow_third_party``: instead of
# admitting every third-party app's code at once, the operator grants ONE named
# app at a time (``agent.apps_trusted``). ``apps.execution.trusted_app_names``
# is the enforcement reader; these 4 endpoints are the write surface.
#
# Unlike the denied-commands opt-out above, grants live in ``config.json`` rather
# than the keystone file. That is a deliberate call by the operator, but NOT
# because an agent needs to write them: measured, an agent can reach the grant
# through neither leg — ``is_sensitive_write_path(<home>/config.json)`` is True
# so its file-edit tool is blocked, and these routes sit in neither
# ``_STRICT_INTERNAL_API_PATHS`` nor ``_MIXED_INTERNAL_API_PATHS``, so
# ``token_auth`` default-denies an agent with ``Token required``. The reason is
# co-location: the blanket ``apps_allow_third_party`` this narrows already lives
# here, and splitting a control across two stores invites the two halves to
# disagree.
#
# Be precise about the blast radius: a grant is scoped to ONE named app as a
# DECISION, but the thing granted is not itself a bounded privilege — a trusted
# app's Python loads in-process in the gateway (``module_loader``), so it inherits
# the gateway's trust domain, including write access to the keystone files this
# module's doctrine keeps away from the agent. Adding per-app grants does not
# widen that (``apps_allow_third_party`` already reached it); it narrows WHO gets
# there. Every mutation is SEL-audited.


def _trusted_list(agent: object) -> list[str]:
    """Current ``agent.apps_trusted`` as a list of non-empty strings.

    Defensive against a hand-edited ``config.json`` (a non-list value, or a list
    holding ``{}``/``5``/``""``): junk is dropped rather than raising, so a
    malformed file yields a clean mutation instead of a 500.
    """
    raw = getattr(agent, "apps_trusted", [])
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, str) and a]


def _trusted_list_raw(agent_raw: dict) -> list[str]:
    """``apps_trusted`` from the RAW base ``agent`` dict, junk dropped.

    The raw-dict twin of :func:`_trusted_list`. Writers use this one because they
    edit the base file: reading the merged model and writing the base would drop
    base entries whenever ``config.local.json`` replaces the list.
    """
    raw = agent_raw.get("apps_trusted", [])
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, str) and a]


def build_trusted_apps_snapshot() -> dict:
    """Compute the snapshot returned by every trusted-apps endpoint.

    ``apps`` is the de-duplicated, sorted list of grants the gate ACTUALLY
    enforces; ``ineffective`` holds stored entries it ignores. The split matters
    because ``trusted_app_names`` (the enforcement reader) requires the app-name
    charset while a hand-edited ``config.json`` can hold anything: without the
    split, ``LD-App``, ``ld-app `` (trailing space), a fullwidth homoglyph, ``..``
    or ``*`` would render as granted while admitting nothing. That fails safe —
    it over-reports trust — but a security panel that shows a grant which does
    not exist is a lie in the other direction, and the user cannot tell why their
    app is still blocked.

    ``allowAll`` mirrors the blanket ``agent.apps_allow_third_party`` with the
    same strict identity check the gate uses, so a hand-typed ``"true"`` renders
    false here exactly as it denies there.

    Synchronous — reads ``config.json``. Async callers MUST offload it (see
    :func:`build_trusted_apps_snapshot_async`).
    """
    agent = KiroCrewConfig.load().agent
    stored = set(_trusted_list(agent))
    effective = trusted_app_names()
    return {
        "apps": sorted(stored & effective),
        "ineffective": sorted(stored - effective),
        "allowAll": getattr(agent, "apps_allow_third_party", False) is True,
    }


async def build_trusted_apps_snapshot_async() -> dict:
    """`build_trusted_apps_snapshot` off the event loop (config.json read)."""
    return await _run_off_loop(build_trusted_apps_snapshot)


async def _trusted_apps_response(extra: dict | None = None) -> web.Response:
    """Return the refreshed snapshot (200), optionally merged with *extra* fields."""
    snapshot = await build_trusted_apps_snapshot_async()
    if extra:
        snapshot.update(extra)
    return web.json_response(snapshot)


class TrustSettingOverlayOwned(Exception):
    """A trust setting lives in ``config.local.json``, so writing ``config.json`` is a no-op.

    ``KiroCrewConfig`` deep-merges the user-owned ``config.local.json`` overlay OVER
    ``config.json`` on every load, and ``save()`` deliberately STRIPS overlay-owned
    values from what it writes. So if ``apps_trusted`` or ``apps_allow_third_party``
    is set in the overlay, a mutation of ``config.json`` is doubly ineffective: the
    write is stripped on the way out and the overlay is re-applied on the way in.

    That makes the honest failure mode a refusal. The dangerous alternative is what
    this code did before: return 200 while a revoked grant keeps admitting the app's
    code on the next load, which is the third variant of "revocation that revokes
    nothing" this feature has had to close. Editing the overlay on the user's behalf
    is not an option either — that file is explicitly user-owned and never written by
    Kiro Crew — so the caller is told which key and which file to edit.
    """

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(
            "these trust settings are set in config.local.json, which overrides "
            f"config.json: {', '.join(keys)}"
        )


_TRUST_SETTING_NAMES = ("apps_trusted", "apps_allow_third_party")


def _overlay_owned_trust_settings() -> list[str]:
    """Return the trust settings ``config.local.json`` currently owns (blocking I/O)."""
    path = config_local_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # Unreadable overlay: the loader ignores it, so config.json IS effective.
        # Refusing here would block a legitimate revoke on an unrelated broken file.
        return []
    agent = raw.get("agent") if isinstance(raw, dict) else None
    if not isinstance(agent, dict):
        return []
    return [name for name in _TRUST_SETTING_NAMES if name in agent]


async def _preflight_agent_config_mutable() -> None:
    """Raise if an ``agent`` trust mutation could not succeed, WITHOUT writing.

    Used by the falling-edge allow-all path, which stops app code before it
    persists the flag: without this, a mutation destined to fail (corrupt
    ``config.json``, overlay-owned setting) would tear the user's apps down and
    then refuse, leaving them stopped for nothing. Raising the same two exceptions
    the real mutation raises keeps the handler's error branches identical.

    This is a pre-flight, not a guarantee — the real check still runs inside the
    config lock during the write. It only ensures the common, already-detectable
    failures are reported before anything is stopped.
    """
    def _check() -> None:
        path = config_path()
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ConfigCorruptError(f"{path} is unreadable: {exc}") from exc
            if not isinstance(existing, dict):
                raise ConfigCorruptError(f"{path} is not a JSON object")
        owned = _overlay_owned_trust_settings()
        if owned:
            raise TrustSettingOverlayOwned(owned)

    await asyncio.get_running_loop().run_in_executor(None, _check)


async def _mutate_agent_config(mutate) -> None:
    """Read-modify-write ``config.json``'s ``agent`` section off the event loop.

    ``mutate(agent_raw) -> None`` edits the RAW base ``agent`` dict in place.
    Runs under the SHARED config lock (the same lock every other config writer
    takes) so a concurrent agent/settings write cannot clobber the grant list,
    and the blocking read→write is offloaded to the default thread executor so a
    slow FS never stalls the gateway loop. The config cache is invalidated after
    the write, so the very next ``trusted_app_names()`` read sees the change —
    the grant enforces without a restart.

    **Why a targeted raw edit and not ``KiroCrewConfig.load()`` → ``save()``.**
    ``save()`` deliberately strips every value ``config.local.json`` also defines
    so overlay settings do not leak into the base file. Routing a trust mutation
    through it therefore rewrites the WHOLE base document minus all overlay-owned
    keys — so a user with base ``model: sonnet`` and overlay ``model: opus`` loses
    the base ``sonnet`` permanently the first time they grant an app trust. The
    effective config still reads ``opus`` from the overlay, so nothing looks
    wrong until the overlay is removed and the setting is simply gone. Editing
    only the key we own keeps the blast radius to that key. (The pre-existing
    overlay refusal below is a different guard, and still required: when the
    overlay owns a TRUST key, no base write can make the change effective.)

    Raises :class:`ConfigCorruptError` when ``config.json`` exists but does not
    parse as a JSON object. ``KiroCrewConfig.load()`` degrades a corrupt file to
    DEFAULTS, so a blind load→save here would write those defaults over the
    user's file and silently erase everything it holds — inline channel
    credentials, ``registries``, ``sandbox``/``jail``/``approval_mode``, the
    model. That is the same hazard ``_read_denied_strict`` guards for this
    module's sibling keystone file, and the reason its comment forbids the
    read-as-empty-and-write-back pattern. The pre-flight parse is inside the
    lock so a concurrent writer cannot slip a corrupt file in between the check
    and the load.
    """
    def _read_modify_write() -> None:
        path = config_path()
        raw: dict = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ConfigCorruptError(f"{path} is unreadable: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ConfigCorruptError(f"{path} is not a JSON object")
            raw = loaded
        # Inside the lock, so a concurrent writer cannot add an overlay between the
        # check and the write.
        owned = _overlay_owned_trust_settings()
        if owned:
            raise TrustSettingOverlayOwned(owned)
        agent_raw = raw.get("agent")
        if not isinstance(agent_raw, dict):
            agent_raw = {}
            raw["agent"] = agent_raw
        mutate(agent_raw)
        write_config_atomically(path, raw)
        # save() would have done this; a raw write must do it explicitly or the
        # grant does not take effect until a restart — and a REVOKE that does not
        # take effect is the exact failure this feature exists to prevent.
        _invalidate_config_cache()

    async with _get_config_lock():
        await asyncio.get_running_loop().run_in_executor(None, _read_modify_write)


async def api_trusted_apps_list(request: web.Request) -> web.Response:
    """GET /api/security/trusted-apps — grants + blanket flag (no audit; read)."""
    return await _trusted_apps_response()


async def api_trusted_app_grant(request: web.Request) -> web.Response:
    """POST /api/security/trusted-apps/{name} — grant one app execution trust.

    Idempotent: granting an already-granted app is a 200 with no duplicate entry.

    The name must be well-formed AND name a REAL app — one that is either already
    installed or listed in a registry the operator trusts. Both are required
    because the grant is consulted by NAME: an arbitrary name would sit in config
    and silently admit whatever app later claims it.

    Accepting a not-yet-installed registry name is what makes the feature usable
    at all. ``install_from_registry`` checks the execution gate BEFORE cloning
    (clone, build and ``onInstall`` are themselves third-party code), so an
    install-only precondition would deadlock: no grant without an install, no
    install without a grant, and the operator's only way through would be the
    blanket ``apps_allow_third_party`` this endpoint exists to avoid. A registry
    name is safe to accept because the index is curated or operator-configured,
    not caller-supplied.
    """
    op = "security.trusted_apps.grant"
    name = request.match_info["name"]

    if not APP_NAME_RE.fullmatch(name):
        _audit(request, operation=op, outcome="denied", resources=f"{name}=invalid_name")
        return web.json_response(
            {
                "error": "app name must match [a-z0-9][a-z0-9_-]*",
                "code": "invalid_app_name",
            },
            status=400,
        )

    # Validation and the write must be ONE critical section, held against the
    # same per-app lock every other lifecycle transition takes. Uninstall's
    # grant-removal precondition and this handler's existence check are both
    # read-then-act, so unserialized they interleave: the uninstall sees no grant
    # to drop and proceeds to tear the app down, and this grant lands *after* the
    # app is gone. That leaves a grant on a name nothing owns — and a name-keyed
    # grant is precisely what lets a LATER third-party app claiming that name run
    # code with no consent prompt, the same hazard the builtin check below exists
    # to prevent. Revoke already serializes this way (`api_trusted_app_revoke`),
    # so the lock order here — app lock, then the config lock inside
    # `_mutate_agent_config` — matches and introduces no new inversion.
    #
    # Safe to hold across these awaits: this handler is only ever entered from
    # the HTTP route, never from inside `install_app`, so it cannot re-enter a
    # lock its own caller holds.
    async with app_lifecycle_lock(name):
        # Offloaded: both read from disk (installed.json + app.json; the registry
        # file and its cached external-index snapshots).
        def _is_known_app() -> bool:
            return get_app(name) is not None or get_registry_app(name) is not None

        # Whether the app is INSTALLED right now, kept separate from "known". A
        # registry-only name is grantable on purpose (the install-consent flow grants
        # before the clone), so its absence from disk is expected and must not be
        # mistaken for the app vanishing. Only a name that WAS installed and is gone
        # after the write is the race below.
        was_installed = await _run_off_loop(lambda: get_app(name) is not None)

        if not await _run_off_loop(_is_known_app):
            _audit(request, operation=op, outcome="denied", resources=f"{name}=unknown")
            return web.json_response(
                {
                    "error": f"app {name!r} is neither installed nor in a known registry",
                    "code": "app_not_installed",
                },
                status=404,
            )

        # A builtin needs no grant (shipped package code is exempt at the gate), and
        # storing one is actively harmful: the entry stays in config after the
        # builtin stops owning the slot, so a LATER third-party app that claims the
        # same name inherits a grant nobody made for it. The existence check above
        # cannot catch this on its own — builtins have an installed.json too, so the
        # name passes as "known" and the grant persists inertly until the takeover
        # makes it live.
        if name in await _run_off_loop(builtin_app_names):
            _audit(request, operation=op, outcome="denied", resources=f"{name}=builtin")
            return web.json_response(
                {
                    "error": f"app {name!r} is a built-in and does not need a trust grant",
                    "code": "app_is_builtin",
                },
                status=409,
            )

        def _mutate(agent_raw: dict) -> None:
            # Reads the BASE list, not the merged view: this write lands in the
            # base file, so the merged value (which an overlay may replace
            # wholesale) is the wrong thing to append to.
            current = _trusted_list_raw(agent_raw)
            if name not in current:
                current.append(name)
            agent_raw["apps_trusted"] = current

        try:
            await _mutate_agent_config(_mutate)
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=config_corrupt")
            return web.json_response(
                {"error": str(exc), "code": "config_corrupt"}, status=409
            )
        except TrustSettingOverlayOwned as exc:
            # 409, not 200: writing config.json here changes NOTHING while the
            # overlay owns the setting, and a success response would tell the
            # operator a grant was revoked while the app stays trusted.
            _audit(request, operation=op, outcome="denied", resources=f"{name}=overlay_owned")
            return web.json_response(
                {
                    "error": str(exc),
                    "code": "trust_setting_overlay_owned",
                    "overlaySettings": exc.keys,
                },
                status=409,
            )
    # Re-check AFTER the write. `app_lifecycle_lock(name)` serializes this handler
    # against other in-process lifecycle work, but `kirocrew app uninstall` runs in a
    # DIFFERENT PROCESS and no asyncio lock reaches it — so an app that passed the
    # existence check above can be gone by the time the grant lands. Grants are keyed
    # on the name alone, so that leaves a grant over a name no app occupies, and the
    # next app installed under it would run its own code with no consent prompt: the
    # same orphan the uninstall path refuses to create and the consent modal rolls
    # back. Scoped to `was_installed` so a deliberately-granted registry name that
    # was never on disk is untouched.
    #
    # This check alone does NOT close the race — it covers only the interleaving
    # where the delete finishes first. The opposite one (this check runs and still
    # sees the app, and only then does the other process delete it) is closed by its
    # PAIR: `uninstall_app` withdraws the grant a second time AFTER its `rmtree`.
    # See the ordering argument there; the two guards are complementary and neither
    # is sufficient on its own.
    if was_installed and not await _run_off_loop(lambda: get_app(name) is not None):
        logger.warning(
            "app %r disappeared while its trust grant was being written; removing it",
            name,
        )

        def _undo(agent_raw: dict) -> None:
            agent_raw["apps_trusted"] = [
                a for a in _trusted_list_raw(agent_raw) if a != name
            ]

        try:
            await _mutate_agent_config(_undo)
        except (ConfigCorruptError, TrustSettingOverlayOwned):
            # The rollback could not be written. Report rather than claim a grant
            # that is both live and orphaned — this needs an operator, and the
            # snapshot in the response shows the entry that is still there.
            logger.warning("could not roll back the orphaned grant for %r", name)
        _audit(request, operation=op, outcome="denied", resources=f"{name}=uninstalled_mid_grant")
        return web.json_response(
            {
                "error": (
                    f"{name!r} was uninstalled while the grant was being saved, so the "
                    "grant was withdrawn — a grant over a name no app occupies would "
                    "let a different app installed under it run code without asking"
                ),
                "code": "app_uninstalled_mid_grant",
            },
            status=409,
        )

    _audit(request, operation=op, outcome="ok", resources=name)
    return await _trusted_apps_response()


async def api_trusted_app_revoke(request: web.Request) -> web.Response:
    """DELETE /api/security/trusted-apps/{name} — revoke trust, stop the code.

    Idempotent: revoking a name that holds no grant is a 200 (the postcondition
    "this app is not trusted" already holds). Deliberately NOT name-validated —
    a hand-edited ``config.json`` can hold a malformed entry that the snapshot
    surfaces, and the user must be able to remove exactly what they see.

    Revocation is EFFECTIVE, not just declarative: the grant is dropped first
    (so nothing can re-enable in the window) and then the app's code is torn
    down — shutdown hooks, route deregistration and cron cleanup via
    ``on_app_disable``, the backend PROCESS via ``stop_app_backend``, resource
    deregistration, and finally the enabled flag. ``disable_app`` alone is a
    metadata write: the backend keeps running with its app secret, its routes
    stay proxied and its crons stay armed, so a metadata-only revoke would
    report success while third-party code kept executing.

    The teardown runs ONLY for a name that actually held a grant and is not a
    builtin. Revoke's un-validated name is for removing junk config entries, and
    a side effect that powerful must not fire on a name the caller never granted
    — otherwise the endpoint becomes a way to disable any installed app,
    including a first-party one, without holding any grant over it.
    """
    op = "security.trusted_apps.revoke"
    name = request.match_info["name"]

    # Teardown runs BEFORE the grant is dropped, and the whole thing sits under the
    # app's lifecycle lock. Dropping the grant first looked safer ("nothing can
    # re-enable in the window") but the lock is what actually closes that window,
    # and grant-first made a partial failure UNRECOVERABLE: the retry would see no
    # grant, skip the teardown, and the app would keep running with trust already
    # gone. Stopping the code first means a failed teardown leaves the grant intact,
    # so the client can simply call DELETE again.

    is_builtin = name in await _run_off_loop(builtin_app_names)

    async with app_lifecycle_lock(name):
        # Config mutability is checked BEFORE anything is stopped, for the same
        # reason the blanket falling edge checks it: teardown disables the app,
        # and a mutation destined to fail (corrupt `config.json`, an
        # overlay-owned trust key) would leave the app switched off while the
        # grant it was supposed to lose is still standing. The user asked to
        # revoke trust and would get "that change did not take effect" over an
        # app that had silently stopped working. Pre-flight, not a guarantee —
        # `_mutate_agent_config` still re-checks inside the config lock, which is
        # what makes the check race-free at the moment of the write.
        try:
            await _preflight_agent_config_mutable()
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=config_corrupt")
            return web.json_response(
                {"error": str(exc), "code": "config_corrupt"}, status=409
            )
        except TrustSettingOverlayOwned as exc:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=overlay_owned")
            return web.json_response(
                {
                    "error": str(exc),
                    "code": "trust_setting_overlay_owned",
                    "overlaySettings": exc.keys,
                },
                status=409,
            )

        snapshot = await build_trusted_apps_snapshot_async()
        stored = set(snapshot["apps"]) | set(snapshot["ineffective"])
        was_granted = name in stored

        disabled = False
        teardown_warnings: list[str] = []
        # A builtin is never torn down: shipped code is exempt at the gate, so no
        # grant governs it and revoking one must not switch it off.
        if was_granted and not is_builtin:
            record = await _run_off_loop(lambda: get_app(name))
            if record:
                # Teardown runs regardless of the PERSISTED ``enabled`` flag.
                #
                # That flag is metadata, NOT evidence about the runtime.
                # ``disable_app`` is a pure metadata write, and the CLI
                # (``kirocrew app disable``) calls it from a DIFFERENT PROCESS
                # that cannot reach the gateway's backend child — so
                # ``enabled: false`` while the app's code is still executing is
                # an ordinary state, not a corner case. Gating the teardown on
                # the flag therefore skipped it for exactly the apps whose
                # recorded state was least trustworthy, and revoke answered 200
                # while third-party code kept running.
                #
                # Running it unconditionally is safe rather than merely
                # thorough: every step is idempotent (deregistration of absent
                # symlinks/manifests is a no-op), and a backend that genuinely
                # is NOT running produces no failure, because the teardown
                # reports one only when it can OBSERVE the port still listening.
                # So for an app that really was off this is a sequence of
                # no-ops, and for one whose flag lied it is the whole point.
                was_enabled = bool(record.get("enabled"))
                result = await teardown_app_runtime(name, record, withdrawing_trust=True)
                for warning in result.warnings:
                    logger.info("revoke teardown of %r: %s", name, warning)
                # Returned, not only logged. A warning here is something the
                # operator has to be able to LEARN — the leading case is the app's
                # own on_shutdown hook failing, which means state it was buffering
                # may be gone. The revocation still succeeded (its code is stopped),
                # so this rides on the 200 rather than turning into a refusal.
                teardown_warnings = list(result.warnings)
                if not result.ok:
                    # The app's code is NOT fully stopped — most importantly its
                    # crons may still fire. Reporting success here is the exact
                    # defect this endpoint was rewritten to remove, so fail loudly
                    # and leave the grant in place for a retry.
                    for failure in result.failures:
                        logger.warning("revoke teardown of %r FAILED: %s", name, failure)
                    _audit(
                        request,
                        operation=op,
                        outcome="failed",
                        resources=f"{name} teardown_incomplete",
                    )
                    return web.json_response(
                        {
                            "error": (
                                f"trust for {name!r} was NOT revoked: its code could not be "
                                "fully stopped, so the grant is left in place — retry"
                            ),
                            "code": "teardown_incomplete",
                            "failures": result.failures,
                        },
                        status=409,
                    )
                # ``disabled`` answers "did WE switch it off", so it stays keyed
                # on the PRE-teardown flag. ``disable_app`` returns ok for an app
                # that was already off, so reporting that as a disable would tell
                # the operator we changed something we did not touch.
                if was_enabled:
                    disabled = bool(await _run_off_loop(lambda: disable_app(name).ok))

        def _mutate(agent_raw: dict) -> None:
            agent_raw["apps_trusted"] = [
                a for a in _trusted_list_raw(agent_raw) if a != name
            ]

        try:
            await _mutate_agent_config(_mutate)
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=config_corrupt")
            return web.json_response(
                {"error": str(exc), "code": "config_corrupt"}, status=409
            )
        except TrustSettingOverlayOwned as exc:
            # 409, not 200: writing config.json here changes NOTHING while the
            # overlay owns the setting, and a success response would tell the
            # operator a grant was revoked while the app stays trusted.
            _audit(request, operation=op, outcome="denied", resources=f"{name}=overlay_owned")
            return web.json_response(
                {
                    "error": str(exc),
                    "code": "trust_setting_overlay_owned",
                    "overlaySettings": exc.keys,
                },
                status=409,
            )

    _audit(
        request,
        operation=op,
        outcome="ok",
        resources=f"{name} was_granted={was_granted} disabled={disabled}",
    )
    return await _trusted_apps_response(
        {"disabled": disabled, "warnings": teardown_warnings}
    )


async def api_trusted_apps_allow_all(request: web.Request) -> web.Response:
    """PUT /api/security/trusted-apps/allow-all — {value: bool} blanket flag.

    Only a JSON boolean is accepted; a truthy ``"true"``/``1`` is rejected rather
    than coerced, mirroring the strict identity check the execution gate applies
    (``third_party_execution_allowed``). Coercing here would persist a value the
    gate then reads as deny — a settings surface that lies about its own state.
    """
    op = "security.trusted_apps.allow_all"
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON", "code": "invalid_value"}, status=400)

    # A valid-but-non-object body (e.g. `[]`) must yield a clean 400, not a 500.
    if not isinstance(body, dict):
        body = {}
    value = body.get("value")
    if not isinstance(value, bool):
        _audit(request, operation=op, outcome="denied", resources="bad_type")
        return web.json_response(
            {"error": "value must be a boolean", "code": "invalid_value"}, status=400
        )

    def _mutate(agent_raw: dict) -> None:
        agent_raw["apps_allow_third_party"] = value

    # Falling edge: STOP the code first, THEN persist `false`.
    #
    # The order matters because the flag is what authorises loading an app's Python
    # at all: `load_app_module` consults `app_execution_denied`, so the moment
    # `false` is on disk a third-party app holding no grant can no longer be
    # loaded — and that includes its `on_shutdown` hook. Persisting first therefore
    # denied the very hook whose job is to flush state and release resources, so the
    # app was stopped without ever being told to clean up. Sweeping while trust
    # still stands lets the hook run, and the flag lands immediately afterwards.
    #
    # A pre-flight check comes first so a mutation that CANNOT succeed (corrupt
    # config, overlay-owned setting) does not stop the user's apps before failing.
    # The sweep's own failures do not abort the write: the flag going off is the
    # security-relevant half and must not be held hostage to a stuck teardown, so
    # they are reported as a 409 afterwards instead.
    stopped: list[str] = []
    still_running: list[str] = []
    sweep_error = ""
    if value is False:
        try:
            await _preflight_agent_config_mutable()
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="denied", resources="config_corrupt")
            return web.json_response(
                {"error": str(exc), "code": "config_corrupt"}, status=409
            )
        except TrustSettingOverlayOwned as exc:
            _audit(request, operation=op, outcome="denied", resources="overlay_owned")
            return web.json_response(
                {
                    "error": str(exc),
                    "code": "trust_setting_overlay_owned",
                    "overlaySettings": exc.keys,
                },
                status=409,
            )
        try:
            stopped, still_running = await _stop_apps_running_on_blanket_trust()
        except Exception as exc:  # noqa: BLE001 - reported below, never swallowed
            # NOT a 500 and NOT a success. An unreadable app directory or a thrown
            # teardown means apps the flag was admitting may still be executing,
            # with their crons still scheduled. Reported as a coded partial failure
            # once the flag has landed.
            logger.warning("could not stop blanket-trusted apps", exc_info=True)
            sweep_error = str(exc) or exc.__class__.__name__

    try:
        await _mutate_agent_config(_mutate)
    except ConfigCorruptError as exc:
        _audit(request, operation=op, outcome="denied", resources="config_corrupt")
        return web.json_response(
            {"error": str(exc), "code": "config_corrupt"}, status=409
        )
    except TrustSettingOverlayOwned as exc:
        # 409, not 200: writing config.json here changes NOTHING while the
        # overlay owns the setting, and a success response would tell the
        # operator a grant was revoked while the app stays trusted.
        _audit(request, operation=op, outcome="denied", resources="overlay_owned")
        return web.json_response(
            {
                "error": str(exc),
                "code": "trust_setting_overlay_owned",
                "overlaySettings": exc.keys,
            },
            status=409,
        )

    # Report BOTH lists. Withdrawing blanket trust while an app it was admitting
    # keeps running is exactly the "revocation that revokes nothing" shape this
    # feature already had to fix once; a response that only carried `stopped`
    # silently omitted the failures, so the operator could not tell that code they
    # just un-trusted is still executing. `outcome` reflects it too, so the audit
    # trail does not read as a clean success.
    if value is False:
        # SECOND sweep, after the flag is on disk, to close the enable race.
        #
        # The first sweep enumerates candidates and then tears them down, which takes
        # real time (it stops backend processes). An app enabled DURING that window is
        # never considered by it, and once `false` lands the gate stops admitting new
        # loads — so without this pass that app would keep executing indefinitely
        # under trust the operator believes they withdrew. Per-app
        # `app_lifecycle_lock` cannot prevent this: the race is against an app that
        # was not in the enumeration at all, so there was no name to lock.
        #
        # Serializing every lifecycle transition against this handler would be the
        # airtight fix, but there is no global lifecycle lock and adding one touches
        # every enable/disable path — out of proportion to this change. A second
        # sweep is bounded and provably closes the window: anything enabled in it is
        # enabled-and-ungranted, which is exactly what `_candidates` selects.
        #
        # Results merge into the SAME lists on purpose. An app caught here has had
        # its backend stopped, but its `on_shutdown` hook could not load (the flag is
        # already false), so its teardown reports a failure and it lands in
        # `still_running` — over-reporting rather than under-reporting, which is the
        # safe direction for a security control, and the 409 points the operator
        # straight at it. Retrying is idempotent and re-sweeps.
        try:
            late_stopped, late_still = await _stop_apps_running_on_blanket_trust(
                require_enabled=True
            )
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.warning("post-write blanket-trust sweep failed", exc_info=True)
            sweep_error = sweep_error or str(exc) or exc.__class__.__name__
        else:
            stopped = sorted(set(stopped) | set(late_stopped))
            still_running = sorted(set(still_running) | set(late_still))

    incomplete = bool(still_running or sweep_error)
    _audit(
        request,
        operation=op,
        outcome="partial" if incomplete else "ok",
        resources=(
            f"{value} stopped={len(stopped)} still_running={len(still_running)}"
            f"{' sweep_error' if sweep_error else ''}"
        ),
    )
    if incomplete:
        # 409 with the flag ALREADY changed, deliberately: the setting is the
        # security-relevant half and must not be rolled back, but the caller has to
        # be able to tell "trust withdrawn and nothing is running" from "trust
        # withdrawn and code it was admitting survived". A bare 200 with an extra
        # field could not be distinguished by a client that does not read the field,
        # and this is exactly the confusion the revoke path was already faulted for.
        # Retrying the same request is safe and idempotent — it re-runs the sweep.
        # 409 carrying the FRESH snapshot: the client must render the new state (the
        # flag really is off) while being told, in a machine-readable way, that
        # something it asked for did not happen.
        snapshot = await build_trusted_apps_snapshot_async()
        # Keys spelled out rather than `**snapshot`: a spread makes the body opaque
        # to the error-code contract scan, which cannot follow where `code` came
        # from and so cannot verify this response carries one.
        return web.json_response(
            {
                "apps": snapshot["apps"],
                "ineffective": snapshot["ineffective"],
                "allowAll": snapshot["allowAll"],
                "stopped": sorted(stopped),
                "stillRunning": sorted(still_running),
                "error": (
                    sweep_error
                    or "blanket trust is off, but these apps could not be stopped "
                    "and may still be executing: " + ", ".join(sorted(still_running))
                ),
                "code": "blanket_trust_sweep_incomplete",
            },
            status=409,
        )
    return await _trusted_apps_response(
        {"stopped": sorted(stopped), "stillRunning": sorted(still_running)}
    )


async def _stop_apps_running_on_blanket_trust(
    *, require_enabled: bool = False
) -> tuple[list[str], list[str]]:
    """Tear down third-party apps that hold no per-app grant.

    Called on the FALLING edge of ``agent.apps_allow_third_party``. An app in this
    set was executing solely because the blanket flag admitted it, so withdrawing
    the flag has to stop it — otherwise the setting is a label that changes nothing
    until the next gateway restart.

    ``require_enabled`` selects which of the two passes this is, and the default is
    the security-relevant one:

    * ``False`` — the pass that runs BEFORE the flag is persisted. It ignores the
      recorded ``enabled`` flag entirely, because that flag is metadata rather than
      evidence about the runtime (``disable_app`` only writes it, and the CLI writes
      it from a process that cannot stop the gateway's backend child). Trusting it
      here skipped the sweep for exactly the apps whose recorded state was least
      trustworthy.
    * ``True`` — the bounded SECOND pass that runs after the write, whose only job
      is catching an app enabled DURING the window. It must stay narrow: the flag
      is already false by then, so an app's ``on_shutdown`` hook can no longer be
      loaded, and re-tearing-down an app the first pass already handled would run
      that hook in a state where the loader denies it.

    Apps WITH their own grant are left running: their permission is independent of
    the blanket flag, and stopping them would revoke a grant the user never touched.
    Builtins are never in scope — shipped code is exempt at the gate.

    Returns ``(stopped, still_running)``. Each app is torn down under its own
    lifecycle lock via the shared teardown, and one failure never aborts the rest —
    but a failure is REPORTED rather than swallowed, because an app that is still
    executing after the operator withdrew the trust admitting it is the single most
    important thing this response can say.
    """

    def _candidates() -> list[str]:
        # `require_enabled=False` (the falling-edge pass) deliberately does NOT
        # filter on `enabled`. The persisted flag is metadata, not evidence about
        # the runtime — `disable_app` only writes it, and the CLI writes it from a
        # process that cannot stop the gateway's backend child — so an app recorded
        # as disabled may still be executing. Filtering on it excluded precisely
        # the apps whose recorded state was least trustworthy from the sweep that
        # exists to stop them.
        #
        # Builtin exemption comes from `builtin_app_names()` ALONE. The candidate
        # query used to also skip `origin == "builtin"`, but `origin` is a field of
        # the app's own `installed.json` record — writable by any app trusted to run
        # code — so a trusted app could stamp itself first-party and walk out of the
        # sweep that exists to stop it. `builtin_app_names()` cannot be forged: it
        # requires a SHIPPED `app.json` to declare the name, and its own contract is
        # that `installed.json` is consulted only to REMOVE trust, never to widen
        # it. Reading `origin` here inverted exactly that rule.
        granted = trusted_app_names()
        builtins = builtin_app_names()
        return [
            app["name"]
            for app in list_apps()
            if app.get("name")
            and (app.get("enabled") or not require_enabled)
            and app["name"] not in granted
            and app["name"] not in builtins
        ]

    stopped: list[str] = []
    still_running: list[str] = []
    for name in await _run_off_loop(_candidates):
        try:
            async with app_lifecycle_lock(name):
                record = await _run_off_loop(lambda n=name: get_app(n))
                if not record:
                    continue
                was_enabled = bool(record.get("enabled"))
                if require_enabled and not was_enabled:
                    continue
                swept = await teardown_app_runtime(
                    name, record, withdrawing_trust=True
                )
                for note in (*swept.warnings, *swept.failures):
                    logger.warning("blanket-trust teardown of %r: %s", name, note)
                if not swept.ok:
                    still_running.append(name)
                    continue
                # `stopped` is shown to the operator as the apps this action
                # stopped, so an app that was already off does not belong in it —
                # its teardown was a no-op and claiming otherwise overstates what
                # the sweep did.
                if not was_enabled:
                    continue
                if await _run_off_loop(lambda n=name: disable_app(n).ok):
                    stopped.append(name)
                else:
                    still_running.append(name)
        except Exception:  # noqa: BLE001 - one app must not block the others
            logger.warning("could not stop blanket-trusted app %r", name, exc_info=True)
            still_running.append(name)
    return stopped, still_running


# ──────────────────────────────────────────────────────────────────────────
# Governance policy viewer — READ-ONLY effective ceiling across every scope
# ──────────────────────────────────────────────────────────────────────────
# The enterprise ceiling (Level 1 ``security_policy.json``) and per-surface
# profiles (Level 2 ``profiles/*.json``) are file-authored and deliberately
# un-editable via the UI (the agent cannot even read them — they sit on the
# sensitive-path keystone). This surface lets an operator SEE the resolved
# ceiling — for every governed scope, its effective state and where it comes
# from — without exposing any write path. It mirrors the model's own
# scope-name-agnostic style: a single per-archetype serializer, driven by
# ``SCOPE_CATALOG``, so the view auto-covers any scope a future release (or the
# companion) registers with zero handler edits.


def _serialize_ruleset(value: object) -> dict:
    """Serialize a ``RulesetLike`` (ScopedRuleset or composed ``_AndRuleset``).

    POSTURE ONLY — never the rule CONTENTS. A flat ``ScopedRuleset`` renders as
    ``{mode, allow_count, deny_count}``: the set MODE and how many entries it
    holds, but NOT the entries themselves. A composed ``_AndRuleset`` renders as
    ``{mode: "intersect", components: [...]}`` so the viewer can still show
    "narrowed by both levels".

    Why counts, not contents: this endpoint is served on the dashboard, which is
    reachable by the agent's own browser tooling (Playwright extension mode
    attaches to the user's authenticated Chrome). Serializing the exact allow/deny
    globs and command patterns would hand the agent a machine-readable copy of the
    security ceiling it is fenced from reading on disk (``security_policy.json`` /
    ``profiles`` are on the ``is_sensitive_path`` keystone) — and knowing the exact
    deny patterns is precisely what lets an agent craft an evasion. The human
    operator reads the authoritative contents from the files directly (outside the
    agent sandbox); the viewer shows POSTURE (which scopes are governed, their
    shape and strictness), not the secret contents.
    """
    if isinstance(value, ScopedRuleset):
        return {
            "mode": value.mode,
            "allow_count": len(value.allow),
            "deny_count": len(value.deny),
        }
    if isinstance(value, _AndRuleset):
        return {
            "mode": "intersect",
            "components": [
                _serialize_ruleset(value.ceiling),
                _serialize_ruleset(value.profile),
            ],
        }
    return {}


def _serialize_control(archetype: str, value: object) -> dict:
    """Serialize one effective archetype value to a UI-friendly dict.

    Dispatch is by ARCHETYPE (``spec.kind``), never by scope name — the same
    decoupling the evaluator uses — so a newly registered scope serializes with
    no edit here as long as it reuses one of the four archetypes.
    """
    if value is None:
        return {}
    if archetype == RULESET:
        return _serialize_ruleset(value)
    if archetype == ORDINAL and isinstance(value, OrdinalControl):
        return {"scale": value.scale, "floor": value.value}
    if archetype == CAPABILITY and isinstance(value, CapabilityGate):
        return {
            "enabled": value.enabled,
            "inner": {name: _serialize_ruleset(rs) for name, rs in value.scopes.items()},
        }
    if archetype == SCOPEDMAP and isinstance(value, ScopedMap):
        return {
            "members": _serialize_ruleset(value.members),
            "posture": {
                member: {leaf: _serialize_ruleset(rs) for leaf, rs in leaves.items()}
                for member, leaves in value.posture.items()
            },
        }
    return {}


def _surface_scope_note(source: str, policy_control: object, archetype: str) -> str:
    """Machine-readable caveat naming WHOSE ceiling a row describes.

    Returns one of ``""`` / ``"host_profile"`` / ``"policy_wide"``:

    * ``host_profile`` — the value is the HOST surface's posture alone. This is
      what stops the viewer mis-reporting a host-only pin as an install-wide
      "off": the shipped host profile disables ``cron``/``messaging``/``spawn``
      because the host process performs none of them, while the cron and messaging
      surfaces enable them under their own profiles.
    * ``policy_wide`` — a Level-1 ceiling produces this value, and policy binds
      every surface, so the row IS install-wide.
    * ``""`` — ungoverned; there is nothing to caveat.

    A composed ``policy+profile`` row needs the policy control itself, not just the
    source label, to classify correctly. For a CAPABILITY, ``enabled`` composes by
    AND across levels, so when the POLICY half is already off the result is off on
    every surface however permissive the profile is — reporting that as
    host-scoped would UNDER-claim a Level-1 ceiling, the same mislabel class this
    function exists to prevent, merely inverted. Only the profile-decides case is
    host-scoped.

    A string enum rather than a rendered sentence: this is contract data the
    frontend maps to a translated string, so it never ships English into a
    JSON body (per the i18n gate) and stays stable across languages.
    """
    if source == "policy":
        return "policy_wide"
    if source == "policy+profile":
        # A policy capability gate that is already OFF binds every surface.
        if (
            archetype == CAPABILITY
            and isinstance(policy_control, CapabilityGate)
            and not policy_control.enabled
        ):
            return "policy_wide"
        return "host_profile"
    if source == "profile":
        return "host_profile"
    return ""


def _other_bound_surfaces() -> list[str]:
    """Surface ids (excluding ``host``) that carry their OWN bound profile.

    Names only, sorted — never a control, count, or rule from those profiles. The
    viewer needs this to answer the question a host-only row provokes: "is cron
    really off, or is that just the host's ceiling?" Listing which surfaces are
    separately governed makes the host row's scope legible without widening the
    endpoint's POSTURE-only contract (see :func:`_serialize_ruleset`).

    Reads the same profile store ``resolve_active_scope`` uses, via the store's own
    ``bound_surfaces()`` accessor. Best-effort: on any store error it returns
    ``[]``, so the caveat degrades to absent rather than breaking the snapshot.
    """
    try:
        return [s for s in bound_surfaces() if s != "host"]
    except Exception:
        logger.warning("could not enumerate bound surfaces", exc_info=True)
        return []


def build_governance_policy_snapshot() -> dict:
    """Compute the effective governance ceiling across ALL scopes (host surface).

    Iterates ``SCOPE_CATALOG`` (so the list stays complete and auto-extends when
    a scope is registered) and, for each scope, intersects the boot-frozen
    POLICY control with the host-surface PROFILE control using the model's OWN
    composition algebra (``_compose_controls`` — the same helper the evaluator's
    ``compose_profiles`` path uses); it does not re-implement ``policy ∩
    profile``. A scope governed by neither level is reported ``ungoverned`` (it
    permits — the standalone default), so with NO policy and NO profile every
    scope is ``ungoverned`` and the response is byte-identical to a standalone
    host.

    **The reported ceiling is the HOST surface's, and only the host's.** The host
    profile governs in-process actions that no user-facing surface drives (app
    activation, workspace admission), so it legitimately pins capabilities like
    ``cron`` and ``messaging`` OFF — the host process itself never schedules a job
    or sends a message. Those same capabilities are typically ENABLED for the
    surfaces that do use them, under their own narrower profiles. A row therefore
    describes one surface's posture, never the whole install's: ``scope_note``
    carries that caveat per row (see :func:`_surface_scope_note`), so a viewer
    cannot render "Disabled by policy" as though the feature were off everywhere.

    Synchronous — reading the ceiling is in-memory, but ``resolve_active_scope``
    may read profile files, so async callers MUST offload it (see
    :func:`build_governance_policy_snapshot_async`). Fail-SAFE for DISPLAY: any
    unexpected governance error yields a well-formed ``unavailable`` response
    rather than raising, so a resolution glitch never breaks the Security page
    (this endpoint enforces nothing).
    """
    try:
        ceiling = getattr(current_context(), "governance", None)
        if ceiling is not None and not isinstance(ceiling, GovernanceCeiling):
            ceiling = None
        # Host-surface profile (bind: {type: surface, id: host}); usually None.
        profile = resolve_active_scope(HOST_SESSION_KEY)

        scopes: list[dict] = []
        for scope, spec in SCOPE_CATALOG.items():
            # Skip the folders.* aliases: they normalize to filesystem.* at parse
            # time, so a control is never stored under the alias key — emitting it
            # would be a permanently-ungoverned duplicate row.
            if scope in _SCOPE_ALIASES:
                continue
            policy_control = ceiling.get(scope) if ceiling is not None else None
            profile_control = profile.get(scope) if profile is not None else None

            if policy_control is not None and profile_control is not None:
                source = "policy+profile"
                effective = _compose_controls(policy_control, profile_control)
            elif policy_control is not None:
                source = "policy"
                effective = policy_control
            elif profile_control is not None:
                source = "profile"
                effective = profile_control
            else:
                source = "ungoverned"
                effective = None

            scopes.append(
                {
                    "scope": scope,
                    "archetype": spec.kind,
                    "governed": effective is not None,
                    "source": source,
                    "scope_note": _surface_scope_note(source, policy_control, spec.kind),
                    "detail": _serialize_control(spec.kind, effective),
                }
            )

        return {
            "version": ceiling.version if ceiling is not None else None,
            "has_policy": ceiling is not None,
            "profile": profile.name if profile is not None else None,
            # The snapshot resolves the HOST-surface profile only; narrower
            # per-surface/app/task profiles can tighten a scope further at runtime.
            # The field makes that scope explicit so the viewer never overclaims to
            # be the whole effective ceiling for every surface.
            "surface": "host",
            # Surfaces OTHER than host that carry their own profile. Names only.
            # The host profile disables cron/messaging/spawn because the host
            # process performs none of them; those surfaces enable what they need
            # under these profiles. Naming them is what lets the viewer show a
            # host row as one surface's posture instead of an install-wide "off".
            "other_bound_surfaces": _other_bound_surfaces(),
            "unavailable": False,
            "scopes": scopes,
        }
    except Exception:
        # Display must never 500 the Security page on a governance glitch.
        logger.warning("governance policy snapshot unavailable", exc_info=True)
        return {
            "version": None,
            "has_policy": False,
            "profile": None,
            "surface": "host",
            "other_bound_surfaces": [],
            "unavailable": True,
            "scopes": [],
        }


async def build_governance_policy_snapshot_async() -> dict:
    """Build the governance-policy snapshot off the event loop.

    ``build_governance_policy_snapshot`` may walk the profile store (filesystem)
    via ``resolve_active_scope``, so it is offloaded to the dedicated
    ``governance_executor`` (``mc-gov``) — NOT the shared default pool — since this
    GET is browser-triggerable and profile-store I/O on a slow FS would otherwise
    pin the default-pool workers the event loop shares for DNS.
    """
    return await asyncio.get_running_loop().run_in_executor(
        governance_executor(), build_governance_policy_snapshot
    )


async def api_governance_policy(request: web.Request) -> web.Response:
    """GET /api/governance/policy — effective ceiling across all scopes (read)."""
    return web.json_response(await build_governance_policy_snapshot_async())
