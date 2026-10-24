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
                      "enabled": true, "note": "use a scoped path instead"}]
    }

``note`` is optional operator prose surfaced in the refusal when the rule fires,
so the caller reads remediation instead of a raw regex. It is metadata only: it
never participates in matching. Create-only, mirroring ``pattern`` — neither has
an edit endpoint; you delete the rule and re-add it.

Mutations run under the shared config lock, write atomically (owner-only
before the payload is published), and emit a SEL audit entry (``ok`` on
success, ``denied`` on reject). Governance
``commands``-scope pins force a built-in rule enabled even when the user disabled
it or set disable-all (tightest-wins): a pinned rule cannot be turned off (409)
and always counts as enabled in the snapshot.

All file I/O is offloaded to a thread executor (``build_denied_commands_snapshot_async``
+ ``_write_denied_state``) so the async handlers never block the gateway event
loop. Every endpoint (GET + all mutations) returns the full refreshed snapshot.

The module is also the import path of three adjacent Security-page surfaces,
which live in sibling owners: the read-only governance policy viewer, the
per-app third-party trust grants (``/api/security/trusted-apps``) and the
registry owner-trust grants. App grants are the one write surface that targets
the agent-writable ``config.json`` rather than a keystone file — see the
section comment in ``security_trusted_apps`` for why that inversion is deliberate.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import re
import sys as _sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.config.loader import (
    denied_commands_path,
)
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError, _audit, _run_off_loop
from kiro_crew.security import DENY_REASON_MATCH_PREFIX, edition_denied_rules

logger = logging.getLogger("kiro_crew.dashboard.handlers.security")

_MAX_PATTERN_LEN = 512
# Notes are prose, not regexes, and land in a refusal the agent reads inline, so
# they get their own (tighter) cap. A note is a one-line remediation hint; a
# paragraph would bury the pattern it annotates.
_MAX_NOTE_LEN = 200


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
        # This function REBUILDS the object rather than passing it through, so an
        # unlisted key is invisible to every consumer downstream. A new key must
        # be added here or it silently does nothing.
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

    ``enabled = pinned OR floor-enforced OR (not disable_all AND id not in
    disabled_ids)``; ``governance_locked = len(pinned_builtin_command_ids()) > 0``;
    ``effective_count = #enabled builtins + #enabled user_added``.

    Each builtin carries an additive ``lock_reason`` discriminator so the UI can
    say WHY a rule is locked: ``"floor"`` (enforced by an always-on floor,
    never opt-out-able), ``"policy"`` (governance-pinned), or ``None``
    (freely toggleable). ``pinned`` keeps its existing governance-only meaning
    for current consumers.
    """
    from kiro_crew.security import (
        builtin_denied_rules,
        floor_enforced_builtin_command_ids,
        pinned_builtin_command_ids_for_snapshot,
    )

    state = _denied_state(_read_denied_data())
    disable_all = state["disable_all"]
    disabled_ids = set(state["disabled_ids"])
    # Snapshot is surface-agnostic → union pins across ALL profiles so a
    # profile-pinned rule renders locked (never a no-op opt-out). The ENFORCEMENT
    # gate uses the ctx-scoped pinned_builtin_command_ids + bound-profile plane,
    # so this display-only union does not widen enforcement.
    pinned = pinned_builtin_command_ids_for_snapshot()
    floor_ids = floor_enforced_builtin_command_ids()

    builtins: list[dict] = []
    # Edition-contributed rules are listed in the SAME array so the panel's
    # category grouping, counts and toggles work with no frontend change. They
    # carry source="edition" so a consumer can tell them apart, and they are
    # never pinned or floor-enforced: a governance pin resolves a pattern to a
    # rule id against the static catalog only, so a pin cannot name one.
    catalog: list[tuple[dict, bool]] = [(r, False) for r in builtin_denied_rules()]
    catalog += [
        (
            {
                "id": r.id,
                "pattern": r.pattern,
                "category": r.category,
                "description": r.description,
            },
            True,
        )
        for r in edition_denied_rules()
    ]
    for rule, from_edition in catalog:
        rid = rule["id"]
        is_pinned = (not from_edition) and rid in pinned
        is_floor = (not from_edition) and rid in floor_ids
        # Floor-enforced rules render forced-on even when the id somehow sits in
        # disabled_ids (state persisted before the toggle rejected it): the floor
        # consults no opt-out state, so honesty requires enabled=true.
        enabled = is_pinned or is_floor or (not disable_all and rid not in disabled_ids)
        # "floor" wins over "policy": the floor holds even if every governance
        # pin were removed, so it is the stronger (and always-true) reason.
        lock_reason: str | None
        if is_floor:
            lock_reason = "floor"
        elif is_pinned:
            lock_reason = "policy"
        else:
            lock_reason = None
        builtins.append(
            {
                "id": rid,
                "pattern": rule["pattern"],
                "category": rule["category"],
                "description": rule["description"],
                "enabled": enabled,
                "pinned": is_pinned,
                "lock_reason": lock_reason,
                "source": "edition" if from_edition else "builtin",
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
                # This serializer is an ALLOWLIST rebuild, not a passthrough — a
                # key absent here is stored on disk but invisible to every
                # endpoint, so the Settings row could never render the note.
                "note": str(entry.get("note", "") or ""),
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
    caller can hot-reload the live HookManager.     The file is locked down to the
    owner only on every write: 0600 on POSIX and an owner-only DACL on Windows.
    The lockdown is applied to the temp file before any content reaches it, so
    the keystone never exists in a world-readable file.

    The blocking read-modify-write (disk read, JSON (de)serialize, atomic
    replace) runs in a thread executor so it never stalls the gateway event
    loop; the async config lock still serializes concurrent mutations.
    """
    from kiro_crew.atomic_write import atomic_write

    path: Path = denied_commands_path()

    def _read_modify_write() -> dict:
        denied = _read_denied_strict()
        mutate(denied)
        # atomic_write(..., restrict_to_owner=True) refuses a linked parent
        # before it mkdir's, then locks the temp. A mkdir here would walk
        # through a planted link and create dirs under its target first.
        atomic_write(
            path,
            json.dumps(denied, indent=2) + "\n",
            restrict_to_owner=True,
        )
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
    from kiro_crew.hooks import HooksConfig, splice_denied_commands

    try:
        state = request.app["state"]
        builder = getattr(state, "context_builder", None)
        manager = getattr(builder, "hooks", None)
        if manager is None:
            return
        # Splice ONLY the opt-out fields from the keystone state onto the live config
        # so the flat hook keys (auto_replies, transforms, auto_approve_tools, …) are
        # not lost. Same helper the config.json hooks reload uses from the other
        # side, so neither write reverts the other's half.
        current = getattr(manager, "_config", None)
        base = current if isinstance(current, HooksConfig) else HooksConfig()
        manager.reload(splice_denied_commands(base, denied_state))
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
    from kiro_crew.security import (
        builtin_denied_rules,
        edition_denied_rules,
        floor_enforced_builtin_command_ids,
    )

    op = "security.denied_commands.builtin_toggle"

    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
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

    # Union the edition-contributed ids: a rule listed in the panel must be
    # toggleable there, or the UI offers a switch the API 404s.
    known_ids = {r["id"] for r in builtin_denied_rules()} | {r.id for r in edition_denied_rules()}
    if rule_id not in known_ids:
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=unknown")
        return web.json_response({"error": "unknown builtin rule"}, status=404)

    # Floor-enforced rules (always-on git-publish floor) are never opt-out-able:
    # a disable must be rejected here, before any state write, or disabled_ids
    # records an id nothing ever reads (silent no-op opt-out). Re-enabling stays
    # a no-op success below. Pure set lookup — no FS, safe on the event loop.
    if not enabled and rule_id in floor_enforced_builtin_command_ids():
        _audit(request, operation=op, outcome="denied", resources=f"{rule_id}=floor_enforced")
        return web.json_response(
            {
                "error": "rule is enforced by an always-on security floor and cannot be disabled",
                "code": "floor_enforced",
            },
            status=409,
        )

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

    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
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
    """POST /api/security/denied-commands/user — {pattern: str, note?: str}."""
    op = "security.denied_commands.user_add"

    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
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
    from kiro_crew.security import (
        _DANGEROUS_AWS_FLAG_RUN,
        _LINEARIZED_AWS_FLAG_RUN,
        is_safe_user_regex,
    )

    if not is_safe_user_regex(pattern):
        error = "pattern rejected: unsafe (catastrophic-backtracking) regex"
        # A user who copies a built-in pattern and tweaks it embeds the dangerous
        # flag-run fragment verbatim and hits a dead end: the fragment is exempt
        # from the backtracking check only as part of a COMPLETE built-in pattern
        # (the scrub in ``is_safe_user_regex`` is builtin-gated), and a complete
        # built-in never reaches this branch, so any pattern here is
        # user-authored. Name the fragment so the trigger is self-explanatory --
        # but only when it really is the trigger: the hint is emitted only if
        # the pattern with both fragments scrubbed would pass, mirroring the
        # builtin scrub (the linearized form is checked too for symmetry with
        # it). ``is_safe_user_regex`` returns False on a non-compiling residue,
        # so a misleading hint fails safe to the generic message.
        embedded = next(
            (
                frag
                for frag in (_DANGEROUS_AWS_FLAG_RUN, _LINEARIZED_AWS_FLAG_RUN)
                if frag in pattern
            ),
            None,
        )
        if embedded is not None:
            scrubbed = pattern.replace(_DANGEROUS_AWS_FLAG_RUN, "").replace(
                _LINEARIZED_AWS_FLAG_RUN, ""
            )
            if is_safe_user_regex(scrubbed):
                error += (
                    f"; the embedded built-in flag-run fragment '{embedded}' is"
                    " the trigger. It is exempt only inside a complete built-in"
                    " pattern, so remove or rewrite that fragment"
                )
        _audit(request, operation=op, outcome="denied", resources="redos_unsafe")
        return web.json_response({"error": error}, status=400)

    # Optional operator note. Absent is the norm (and the pre-existing shape), so
    # a missing key is NOT an error — but a present-and-wrong-typed one is, to
    # match how ``pattern`` is handled rather than silently dropping input the
    # caller believes it supplied.
    note_raw = body.get("note", "")
    if not isinstance(note_raw, str):
        _audit(request, operation=op, outcome="denied", resources="note_bad_type")
        return web.json_response(
            {"error": "note must be a string", "code": "note_not_a_string"}, status=400
        )
    # Newlines would forge extra lines in the refusal, where the first line is a
    # parsed contract (RecoveryCard's per-line POLICY_RE). Collapse rather than
    # reject: the intent is unambiguous and a 400 over whitespace is hostile.
    note = " ".join(note_raw.split())
    if len(note) > _MAX_NOTE_LEN:
        _audit(request, operation=op, outcome="denied", resources="note_oversize")
        return web.json_response(
            {
                "error": f"note must be at most {_MAX_NOTE_LEN} characters",
                "code": "note_too_long",
            },
            status=400,
        )
    # The note is emitted on its own line of the refusal, and RecoveryCard parses
    # refusals with a GLOBAL per-line regex — so a note carrying the refusal
    # prefix would be read as a second, fabricated deny pattern. Pasting a
    # refusal you just saw into this field is a natural thing to do, so reject it
    # with a real error rather than silently mangling the text. Guard on the
    # COLON-terminated form: the regex makes the space after the colon optional,
    # so "…policy:forged" parses as a refusal line without matching the emitted
    # prefix.
    if DENY_REASON_MATCH_PREFIX in note:
        _audit(request, operation=op, outcome="denied", resources="note_forges_reason")
        return web.json_response(
            {
                "error": f"note must not contain {DENY_REASON_MATCH_PREFIX!r}",
                "code": "note_forges_reason",
            },
            status=400,
        )

    rule_id = "user-" + uuid.uuid4().hex[:12]

    def _mutate(denied: dict) -> None:
        current = denied.get("user_added")
        current = list(current) if isinstance(current, list) else []
        current.append({"id": rule_id, "pattern": pattern, "enabled": True, "note": note})
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

    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
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

    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
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


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.security_audit import (  # noqa: F401
        _sel,
    )
    from kiro_crew.dashboard.handlers.security_governance_policy import (  # noqa: F401
        _distribution_posture,
        _other_bound_surfaces,
        _serialize_control,
        _serialize_ruleset,
        _surface_scope_note,
        api_governance_policy,
        build_governance_policy_snapshot,
        build_governance_policy_snapshot_async,
    )
    from kiro_crew.dashboard.handlers.security_trusted_apps import (  # noqa: F401
        _TRUST_SETTING_NAMES,
        TrustSettingOverlayOwned,
        _mutate_agent_config,
        _overlay_owned_trust_settings,
        _preflight_agent_config_mutable,
        _stop_apps_running_on_blanket_trust,
        _trusted_apps_response,
        _trusted_list,
        _trusted_list_raw,
        _trusted_local_list_raw,
        _trusted_repositories_raw,
        api_trusted_app_grant,
        api_trusted_app_revoke,
        api_trusted_apps_allow_all,
        api_trusted_apps_list,
        build_trusted_apps_snapshot,
        build_trusted_apps_snapshot_async,
    )
    from kiro_crew.dashboard.handlers.security_trusted_registries import (  # noqa: F401
        _expire_registry_index_cache,
        _matching_operator_registry,
        _operator_registry_rows,
        _registry_served_state,
        _served_state_for_key,
        _trusted_registries_response,
        _trusted_registry_repo_from_body,
        api_trusted_registries_list,
        api_trusted_registry_grant,
        api_trusted_registry_revoke,
        build_trusted_registries_snapshot,
    )


if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.apps.execution import (  # noqa: F401
        APP_NAME_RE,
        _repository_grant_denied_for_binding,
        builtin_app_names,
        trusted_app_names,
    )
    from kiro_crew.apps.manager import (  # noqa: F401
        disable_app,
        get_app,
        list_apps,
    )
    from kiro_crew.apps.official_catalog import (  # noqa: F401
        CatalogUnavailable,
    )
    from kiro_crew.apps.registry import (  # noqa: F401
        _entry_git_url,
        _expire_cache_file,
        _external_registry_cache_identity,
        _external_registry_cache_path,
        _git_target_is_unsupported,
        _git_url_host,
        _is_supported_registry_transport,
        _normalize_git_target,
        _pinned_registries,
        _same_git_target,
        _strip_git_target_userinfo,
        get_registry_app,
        resolve_installed_trust_repository,
    )
    from kiro_crew.apps.routes import (  # noqa: F401
        app_lifecycle_lock,
    )
    from kiro_crew.apps.teardown import (  # noqa: F401
        teardown_app_runtime,
    )
    from kiro_crew.config.loader import (  # noqa: F401
        ConfigReadError,
        KiroCrewConfig,
        _invalidate_config_cache,
        config_local_path,
        config_path,
        read_config_text,
        update_config_locked,
    )
    from kiro_crew.executors import (  # noqa: F401
        governance_executor,
    )
    from kiro_crew.platform.context import (  # noqa: F401
        current_context,
    )
    from kiro_crew.platform.governance import (  # noqa: F401
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
        policy_level_control,
    )
    from kiro_crew.platform.governance_profiles import (  # noqa: F401
        HOST_SESSION_KEY,
        bound_surfaces,
        fallback_profile_names,
        resolve_active_scope,
        unknown_profile_scopes,
    )


# This module is the facade of the Settings > Security API handlers: every name is
# reached through it as ``kiro_crew.dashboard.handlers.security.<name>``. A read is
# served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. The denied-commands surface above stays here. New
# work goes to the owner of its responsibility:
#
#   ``security_audit`` -- the SEL audit and the write-path pieces the write endpoints share
#   ``security_trusted_apps`` -- per-app third-party trust grants
#   ``security_governance_policy`` -- the read-only governance policy viewer
#   ``security_trusted_registries`` -- registry owner-trust grants
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.security_audit",
    "kiro_crew.dashboard.handlers.security_trusted_apps",
    "kiro_crew.dashboard.handlers.security_governance_policy",
    "kiro_crew.dashboard.handlers.security_trusted_registries",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
