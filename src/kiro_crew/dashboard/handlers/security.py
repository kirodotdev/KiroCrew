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
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path

from aiohttp import web

from kiro_crew.config.loader import denied_commands_path

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
    import asyncio

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
    import asyncio

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
    import asyncio

    from kiro_crew.agent import _atomic_json_write
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

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
