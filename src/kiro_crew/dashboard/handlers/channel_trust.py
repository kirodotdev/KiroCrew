"""Channel trust roster REST API — the enrol / revoke surface.

The roster (``<config_dir>/channel_trust.json``) decides which chat connections
may attach. It is a KEYSTONE file (on ``security._SENSITIVE_HOME_DIRS`` — the
AGENT can neither read nor write it), and this module is what lets the OPERATOR
edit it anyway, from the dashboard, without hand-editing JSON.

Those two facts are not in tension, and conflating them is what left the roster
read-only at first: "the agent must not write this" is a property of the agent
sandbox, while the dashboard runs inside the gateway process. The precedent is
``denied_commands.json`` next door — the same keystone class, edited through
Settings > Security by exactly this shape — and this module copies it
deliberately: mutations run under the shared config lock, write atomically at
0600, offload the blocking read-modify-write to an executor so the event loop
never stalls, emit a SEL record per attempt, and return the refreshed snapshot so
the caller never has to re-fetch to learn what happened.

Enrolment takes effect on the next message with no restart: the gates read the
roster per decision (``trust.load_roster``), which is what makes a revoke here an
immediate control rather than a queued intention.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.messaging import trust
from kiro_crew.messaging.connections import ConnectionNameError, parse_item

logger = logging.getLogger(__name__)

#: An operator note is prose for whoever reads the roster later, never matched
#: against anything. Bounded so one paste cannot bloat a file every gate reads.
_MAX_NOTE = 200


def _sel():
    from kiro_crew import sel as _pkg

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


class RosterCorruptError(Exception):
    """The roster exists but cannot be parsed — refuse to mutate it.

    Rewriting a file we could not read would silently discard whatever the
    operator (or a fleet push) had put there, so a corrupt roster is a 409 the
    human resolves rather than something this API repairs by overwriting.
    """


def _read_for_mutation() -> dict:
    """The roster as a mutable dict. Absent is ``{}``; corrupt raises."""
    import json

    path = trust.roster_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise RosterCorruptError(str(exc)) from exc
    if not isinstance(data, dict):
        raise RosterCorruptError("roster root is not an object")
    return data


async def _write_roster(mutate) -> dict:
    """Read-modify-write the keystone roster atomically, under the config lock.

    ``mutate(doc: dict) -> None`` edits the document in place. The blocking
    read-modify-write runs in an executor so it never stalls the gateway loop; the
    async config lock still serializes concurrent mutations, so two operators
    toggling at once cannot lose one another's entry.
    """
    from kiro_crew.agent import _atomic_json_write

    path: Path = trust.roster_path()

    def _read_modify_write() -> dict:
        doc = _read_for_mutation()
        doc.setdefault("version", trust.ROSTER_VERSION)
        doc.setdefault("connections", [])
        mutate(doc)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(path, doc)
        try:
            from kiro_crew.platform_compat import chmod_safe

            chmod_safe(path, 0o600)
        except Exception:
            logger.debug("could not chmod channel_trust.json to 0600", exc_info=True)
        return doc

    async with _get_config_lock():
        return await asyncio.get_running_loop().run_in_executor(None, _read_modify_write)


def _entries(doc: dict) -> list:
    raw = doc.get("connections")
    return raw if isinstance(raw, list) else []


def _entry_id(entry: object) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        value = entry.get("id")
        return value.strip() if isinstance(value, str) else ""
    return ""


async def _snapshot(request: web.Request) -> web.Response:
    """The refreshed read model, so a caller never re-fetches to see the result."""
    from kiro_crew.dashboard.handlers_system import _collect_connections
    from kiro_crew.executors import governance_executor

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(governance_executor(), _collect_connections)
    return web.json_response(data)


async def api_channel_trust_enrol(request: web.Request) -> web.Response:
    """POST /api/connections/enrol — allow a connection to attach.

    Body: ``{"id": "telegram/ops-bot", "note": "optional prose"}``. Idempotent: a
    connection already on the roster returns the snapshot unchanged rather than a
    duplicate entry, so a double-click cannot corrupt the file.
    """
    op = "channel_trust_enrol"
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON body"}, status=400)
    raw_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(raw_id, str) or not raw_id.strip():
        _audit(request, operation=op, outcome="denied", resources="missing_id")
        return web.json_response({"error": "id is required"}, status=400)
    try:
        # Normalize through the connection model so a bare transport enrols its
        # DEFAULT connection and an unusable name is refused here rather than
        # written into a file every gate reads.
        item = parse_item(raw_id).governance_item()
    except ConnectionNameError as exc:
        _audit(request, operation=op, outcome="denied", resources=f"{raw_id}=bad_name")
        return web.json_response({"error": str(exc)}, status=400)
    note = body.get("note") if isinstance(body, dict) else None
    note = note.strip()[:_MAX_NOTE] if isinstance(note, str) else ""

    def _mutate(doc: dict) -> None:
        entries = _entries(doc)
        if any(_entry_id(e) == item for e in entries):
            return
        entries.append({"id": item, "note": note} if note else {"id": item})
        doc["connections"] = entries

    try:
        await _write_roster(_mutate)
    except RosterCorruptError as exc:
        _audit(request, operation=op, outcome="denied", resources=f"{item}=roster_corrupt")
        return web.json_response(
            {"error": f"the trust roster is unreadable and was not modified: {exc}"},
            status=409,
        )
    _audit(request, operation=op, outcome="ok", resources=item)
    return await _snapshot(request)


async def api_channel_trust_revoke(request: web.Request) -> web.Response:
    """POST /api/connections/revoke — stop a connection from attaching.

    Body: ``{"id": "telegram/ops-bot"}``. Takes effect on that connection's next
    message (the inbound gate re-reads the roster per decision); the transport
    stays connected until it restarts, which is why the revoke is enforced
    per-message rather than only at attach.

    Idempotent: revoking something absent is a success with an unchanged
    snapshot, so a retry after a dropped response cannot 404 confusingly.
    """
    op = "channel_trust_revoke"
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON body"}, status=400)
    raw_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(raw_id, str) or not raw_id.strip():
        _audit(request, operation=op, outcome="denied", resources="missing_id")
        return web.json_response({"error": "id is required"}, status=400)
    try:
        item = parse_item(raw_id).governance_item()
    except ConnectionNameError as exc:
        _audit(request, operation=op, outcome="denied", resources=f"{raw_id}=bad_name")
        return web.json_response({"error": str(exc)}, status=400)

    def _mutate(doc: dict) -> None:
        # Drop by NORMALIZED id, so an entry written as the terse `telegram`
        # is revoked by a request naming `telegram/default` and vice versa —
        # otherwise a revoke silently no-ops against a roster an operator
        # hand-wrote in the other spelling.
        kept = []
        for entry in _entries(doc):
            raw = _entry_id(entry)
            try:
                normalized = parse_item(raw).governance_item() if raw else ""
            except ConnectionNameError:
                normalized = ""
            if normalized != item:
                kept.append(entry)
        doc["connections"] = kept

    try:
        await _write_roster(_mutate)
    except RosterCorruptError as exc:
        _audit(request, operation=op, outcome="denied", resources=f"{item}=roster_corrupt")
        return web.json_response(
            {"error": f"the trust roster is unreadable and was not modified: {exc}"},
            status=409,
        )
    _audit(request, operation=op, outcome="ok", resources=item)
    return await _snapshot(request)
