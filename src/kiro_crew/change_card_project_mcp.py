"""The ``mcp.project_server`` change card: start one server a folder's own MCP file names.

Raised by the chat itself at session start (``dashboard.project_mcp_cards``)
for each server the session held back (``agent_sdk.project_mcp_holds``), never by an agent:
the kind is gateway-resolved, so ``propose_change`` refuses it. The card shows the
file, the server's launch and, for a changed server, what changed since it was
approved. Apply is one step, ``POST /api/mcp/project-servers/approve``, which records
the approval and restarts open chats, so the chat that raised it starts the server.

Pure: validates the parameters and builds the preview from the snapshot
:func:`server_snapshot` takes. No Undo (``change_card_catalog_plans.offers_no_undo``).
"""

from __future__ import annotations

import os
import string
from typing import Any, Mapping

from kiro_crew.project_mcp_approvals import MAX_FOLDER_CHARS, MAX_SERVER_NAME_CHARS

KIND_PROJECT_MCP_SERVER = "mcp.project_server"

_PARAMS = ("folder", "file", "server", "fingerprint")
_HEX = frozenset(string.digits + "abcdef")

_TITLES = {
    "new": "Start {name} from this folder's {file}?",
    "changed": "{name} changed in this folder's {file}. Start it?",
}
#: The ``Open chats`` row. Apply resets every open session (``reset_sessions``):
#: a chat keeps its history, and one mid-reply is reset when that turn ends.
_RESTART_NOTE = "Allowing restarts every open chat so {server} loads; history is kept."


def params_schema() -> dict[str, Any]:
    """The card's ``params`` schema."""
    return {
        "type": "object",
        "properties": {k: {"type": "string"} for k in _PARAMS},
        "required": list(_PARAMS),
        "additionalProperties": False,
    }


def clean_params(p: dict[str, Any]) -> dict[str, Any]:
    """Normalize the card's parameters. Raises ``CardCatalogError``."""
    from kiro_crew.change_card_catalog_kinds import _fail, _only
    from kiro_crew.project_mcp_files import PROJECT_MCP_FILES

    _only(p, set(_PARAMS))
    folder, file, server, fp = (p.get(k) for k in _PARAMS)
    if not isinstance(folder, str) or not os.path.isabs(folder) or len(folder) > MAX_FOLDER_CHARS:
        raise _fail("invalid_param", "'folder' must be an absolute path")
    if file not in PROJECT_MCP_FILES:
        raise _fail("invalid_param", "'file' must be a project MCP file")
    if not isinstance(server, str) or not server or len(server) > MAX_SERVER_NAME_CHARS:
        raise _fail("invalid_param", "'server' must name a server")
    if not isinstance(fp, str) or len(fp) != 64 or not set(fp) <= _HEX:
        raise _fail("invalid_param", "'fingerprint' must be a sha256 digest")
    return {"folder": folder, "file": file, "server": server, "fingerprint": fp}


def server_snapshot(p: Mapping[str, Any]) -> dict[str, Any]:
    """The server as the folder's file and the record hold it now. Blocking."""
    from kiro_crew.project_mcp_approvals import pending_servers

    for server in pending_servers(p["folder"], files=(p["file"],)):
        if server.name == p["server"]:
            return {
                "pending": True,
                "path": server.path,
                "status": server.status,
                "fingerprint": server.fingerprint,
                "entry": dict(server.entry),
                "approved": dict(server.approved),
            }
    return {"pending": False}


def _pair_rows(
    label: str, sep: str, now: Mapping[str, Any], was: Mapping[str, str] | None
) -> list[dict[str, Any]]:
    """The add and remove lists of one env or headers map; *was* holds value digests."""
    from kiro_crew.change_card_catalog_plans import _display, _plain
    from kiro_crew.project_mcp_approvals import _digest

    now = now if isinstance(now, Mapping) else {}
    if was is None:
        added = sorted(now)
        removed: list[str] = []
    else:
        added = sorted(k for k in now if was.get(str(k)) != _digest(now[k]))
        removed = sorted(k for k in was if k not in now or k in added)
    if not added and not removed:
        return []
    row: dict[str, Any] = {"label": label}
    if added:
        row["add"] = [_display(f"{k}{sep}{_plain(now[k])}") for k in added]
    if removed:
        row["remove"] = [_display(str(k)) for k in removed]
    return [row]


def _extra_rows(entry: Mapping[str, Any], was: Mapping[str, str] | None) -> list[dict[str, Any]]:
    """One row per field outside the launch rows that is new or changed, or removed.

    *was* holds value digests, so a changed field shows its new value only.
    """
    from kiro_crew.change_card_catalog_plans import _display, _plain
    from kiro_crew.project_mcp_approvals import _digest, extra_fields

    now = extra_fields(entry)
    was = was if isinstance(was, Mapping) else None
    rows: list[dict[str, Any]] = [
        {"label": _display(k), "after": _display(_plain(now[k]))}
        for k in sorted(now)
        if was is None or was.get(k) != _digest(now[k])
    ]
    if was is not None:
        rows += [
            {"label": _display(k), "remove": [_display(k)]} for k in sorted(was) if k not in now
        ]
    return rows


def build_preview(p: dict[str, Any], snapshot: dict[str, Any], out: dict[str, Any]) -> None:
    """Fill *out* (title, changes, risk, apply). Raises ``CardCatalogError``."""
    from kiro_crew.change_card_catalog_kinds import RISK_CODE_EXEC, _fail
    from kiro_crew.change_card_catalog_plans import _display, step
    from kiro_crew.project_mcp_approvals import _MAX_LAUNCH_CHARS, launch_digest, launch_rows

    name = p["server"]
    if not snapshot.get("pending"):
        raise _fail("no_change", f"'{name}' in {p['file']} needs no approval now")
    if snapshot.get("fingerprint") != p["fingerprint"]:
        raise _fail(
            "changed", f"'{name}' changed in {p['file']} again; a new chat shows the new card"
        )
    entry: dict[str, Any] = snapshot.get("entry") or {}
    was: dict[str, Any] = snapshot.get("approved") or {}
    changed = snapshot.get("status") == "changed"
    rows: list[dict[str, Any]] = [{"label": "File", "after": snapshot.get("path", "")}]
    launch = launch_rows(entry)
    if any(len(text) > _MAX_LAUNCH_CHARS for _label, _key, text in launch):
        raise _fail("card_too_large", "this launch is too long to show as a card")
    # Allow approves the launch shown, so a launch the redactor would alter gets no
    # card; an earlier launch shown only as "before" is display, not what Allow approves.
    if any(_display(text) != text for _label, _key, text in launch):
        raise _fail("card_hides_launch", "this launch cannot be shown in full as a card")
    if not changed:
        rows += [{"label": label, "after": _display(text)} for label, _key, text in launch]
    elif was.get("launch_digest") != launch_digest(entry):
        # Each row shows its own change; a change no shown text reveals shows them all.
        moved = [row for row in launch if was.get(row[1]) != row[2]]
        now_keys = {key for _label, key, _text in launch}
        gone = [
            (label, key)
            for label, key in (("Runs", "launch"), ("URL", "url"))
            if key not in now_keys and key in was
        ]
        for label, key, text in moved or ([] if gone else launch):
            row: dict[str, Any] = {"label": label, "after": _display(text)}
            if key in was:
                row["before"] = _display(str(was[key]))
            rows.append(row)
        rows += [{"label": label, "remove": [_display(str(was[key]))]} for label, key in gone]
    for label, key, sep in (("Environment", "env", "="), ("Headers", "headers", ": ")):
        pairs = _pair_rows(label, sep, entry.get(key) or {}, was.get(key) if changed else None)
        if not pairs and changed and (key == "env" or entry.get(key)):
            pairs = [{"label": label, "after": "unchanged"}]
        rows += pairs
    for key in ("type", "cwd"):
        now, before = entry.get(key), was.get(key)
        if now is not None and (not changed or str(now) != before):
            rows.append({"label": key, "after": _display(str(now))})
        elif changed and now is None and before is not None:
            rows.append({"label": key, "remove": [_display(before)]})
    rows += _extra_rows(entry, was.get("extra") if changed else None)
    rows.append({"label": "Open chats", "after": _RESTART_NOTE.format(server=name)})
    out["title"] = _TITLES["changed" if changed else "new"].format(name=name, file=p["file"])
    out["changes"] = rows
    out["risk"] = RISK_CODE_EXEC
    out["apply"] = [step("POST", "/api/mcp/project-servers/approve", {k: p[k] for k in _PARAMS})]
