"""Message-body fields of third-party MCP tools: scanned as documents, not as commands.

The always-enforced tool_input scan in ``llm_helpers._resolve_permission`` refuses
any string over ``MAX_SCANNABLE_COMMAND_CHARS`` (20 KiB) before a detector runs,
because the shell-command matchers are what that ceiling bounds. A mail tool's
``send_email`` with a 40-100 KB HTML body was therefore always refused. This module
names the fields that are a message or document BODY and judges them the way a cron
script body is judged: whole-body document detectors only (sensitive path,
credential, exfiltration URL), with the 256 KiB source-body ceiling
(``MAX_SCANNABLE_SOURCE_BODY_CHARS``). A body field never reaches the shell
matcher or the deny-rule list. Every other argument of the call keeps the full
scan. Design: https://github.com/kirodotdev/KiroCrew/issues/8053.

A row is ``(server, tool, field)``. It counts only when the gate's cached, trusted
server AND tool identity match it (the same bar ``MCP_DOCUMENT_BODY_FIELDS`` uses),
so a same-named tool on another server, or an identity that came from the payload,
gets nothing. ``field`` is a top-level argument name.

Two sources of rows:

* :data:`DEFAULT_BODY_FIELDS`, a small reviewed list for common mail and message
  tools. A server name is whatever the user called the server in their MCP config,
  so a default matches only when that name is the usual one.
* Rows the dashboard OWNER added, stored in ``mcp_body_fields.json`` in the data
  home. The only writer is the owner-gated dashboard route
  (``dashboard/handlers/mcp_body_fields.py``), and it adds only a row the gate
  itself refused for size (:func:`record_size_refusal`), so the owner never types a
  field name and the agent cannot nominate one. The leaf is on the read+write
  keystone floor and mounted read-only in the sandbox, so the agent cannot write it.

Fail direction: a store that cannot be read gives no owner rows, which means the
20 KiB refusal stays in force.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.platform.tool_paths import strip_server_qualifier
from kiro_crew.security.exfil import scan_exfiltration_urls
from kiro_crew.security.paths import (
    MAX_SCANNABLE_SOURCE_BODY_CHARS,
    is_unverifiable_path_refusal,
    sensitive_path_refusal,
)
from kiro_crew.security.redaction_entropy import _contains_fixed_credential

logger = logging.getLogger(__name__)

#: Longest body field the document detectors will scan; longer is refused.
MAX_BODY_FIELD_CHARS = MAX_SCANNABLE_SOURCE_BODY_CHARS

#: A server or tool name a row may hold: what MCP configs and tool lists use.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
#: A top-level argument name a row may hold.
_FIELD_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
#: Owner rows kept; a store longer than this is read up to it.
_MAX_OWNER_ROWS = 200
#: Size refusals remembered for the dashboard's allow button.
_MAX_REFUSALS = 20


@dataclass(frozen=True)
class BodyField:
    """One ``(server, tool, field)`` row."""

    server: str
    tool: str
    field: str

    def to_dict(self) -> dict[str, str]:
        return {"server": self.server, "tool": self.tool, "field": self.field}


def is_valid_row(server: object, tool: object, field: object) -> bool:
    """Whether the three values make a row this module will store."""
    return (
        isinstance(server, str)
        and isinstance(tool, str)
        and isinstance(field, str)
        and bool(_NAME_RE.match(server))
        and bool(_NAME_RE.match(tool))
        and bool(_FIELD_RE.match(field))
    )


#: The reviewed defaults: the body of a sent or drafted mail, a posted message, a
#: created page. Each field is text the server delivers or stores; none is a field
#: the server runs. Additive only, and only for a field with that meaning.
DEFAULT_BODY_FIELDS: tuple[BodyField, ...] = (
    BodyField("gmail", "send_email", "body"),
    BodyField("gmail", "draft_email", "body"),
    BodyField("outlook", "send_email", "body"),
    BodyField("outlook", "draft_email", "body"),
    BodyField("slack", "post_message", "text"),
    BodyField("notion", "create_page", "content"),
)


def store_path() -> Path:
    """``mcp_body_fields.json`` in the data home (respects ``KIROCREW_HOME``)."""
    # Deferred, as ``redaction_switch`` does: keep ``config`` off the import path.
    from kiro_crew.config.loader import config_dir

    return config_dir() / "mcp_body_fields.json"


#: Serialises the store's read-modify-write. In-process: the one writer is the
#: owner-gated dashboard route in the gateway process.
_STORE_LOCK = threading.RLock()


def owner_rows() -> list[dict[str, str]]:
    """The owner-added rows, read fresh. Fails soft to none."""
    try:
        raw = json.loads(store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("mcp_body_fields.json is unreadable; no owner-added body fields apply")
        return []
    rows = raw.get("fields") if isinstance(raw, dict) else None
    out: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if not is_valid_row(row.get("server"), row.get("tool"), row.get("field")):
            continue
        out.append(
            {
                "server": row["server"],
                "tool": row["tool"],
                "field": row["field"],
                "added_at": str(row.get("added_at", "")),
            }
        )
        if len(out) >= _MAX_OWNER_ROWS:
            break
    return out


def _write_rows(rows: list[dict[str, str]]) -> None:
    from kiro_crew.atomic_write import atomic_write

    atomic_write(
        store_path(),
        json.dumps({"fields": rows}, indent=2, sort_keys=True),
        restrict_to_owner=True,
    )


def transaction() -> threading.RLock:
    """The lock the dashboard route holds around an audit-and-write pair."""
    return _STORE_LOCK


def add_owner_row(row: BodyField) -> bool:
    """Store *row*; ``False`` when it is already allowed (default or owner)."""
    with _STORE_LOCK:
        if row in DEFAULT_BODY_FIELDS:
            return False
        rows = owner_rows()
        if any(BodyField(r["server"], r["tool"], r["field"]) == row for r in rows):
            return False
        if len(rows) >= _MAX_OWNER_ROWS:
            raise OSError("owner body-field list is full")
        added_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows.append({**row.to_dict(), "added_at": added_at})
        _write_rows(rows)
    forget_size_refusal(row)
    return True


def remove_owner_row(row: BodyField) -> bool:
    """Drop an owner-added *row*; ``False`` when no such owner row exists."""
    with _STORE_LOCK:
        rows = owner_rows()
        kept = [r for r in rows if BodyField(r["server"], r["tool"], r["field"]) != row]
        if len(kept) == len(rows):
            return False
        _write_rows(kept)
    return True


def _bare_tool(tool_name: str, mcp_server_name: str) -> str:
    return strip_server_qualifier(tool_name, mcp_server_name) or tool_name


def body_field_keys(tool_name: str | None, mcp_server_name: str | None) -> frozenset[str]:
    """The body fields allowed for this call's TRUSTED identity, else empty.

    Both arguments MUST come from the client's identity caches and the caller must
    require the identity-trusted flag, as for ``mcp_document_body_keys``. Reads the
    owner store, so call it off the event loop.
    """
    if not tool_name or not mcp_server_name:
        return frozenset()
    names = {tool_name, _bare_tool(tool_name, mcp_server_name)}
    rows = [*DEFAULT_BODY_FIELDS]
    rows.extend(BodyField(r["server"], r["tool"], r["field"]) for r in owner_rows())
    return frozenset(r.field for r in rows if r.server == mcp_server_name and r.tool in names)


def body_field_denial(bodies: list[str]) -> tuple[str, str, str] | None:
    """The first document-detector refusal among *bodies*, or ``None``.

    Size ceiling, then the path tier, then the credential and exfiltration-URL
    detectors. No shell-command matcher and no deny-rule list: a body is text the
    server delivers, not a command line. The tuple is ``(kind, reason, matched)``
    as ``_first_tool_input_denial`` returns it; *matched* never carries the body.
    """
    for s in bodies:
        if len(s) > MAX_BODY_FIELD_CHARS:
            return (
                "oversize",
                (
                    "Blocked: a message body field is too large to security-scan "
                    f"({len(s)} chars > {MAX_BODY_FIELD_CHARS} limit); refused rather "
                    "than left unscanned"
                ),
                s[:64],
            )
        path_refusal = sensitive_path_refusal(s)
        if path_refusal:
            if is_unverifiable_path_refusal(path_refusal):
                return ("path", path_refusal, "")
            return ("path", "Blocked: sensitive path in a message body field", "")
        # The fixed-format predicate the request-blocking gates use, not the
        # redaction pass: its entropy guess would read an inline base64 image in
        # an HTML mail as a key.
        if _contains_fixed_credential(s):
            return ("body", "Blocked: a message body field contains a credential", "")
        if scan_exfiltration_urls(s):
            return (
                "body",
                "Blocked: a message body field contains a link that carries data out",
                "",
            )
    return None


def oversize_field(params: Mapping[str, Any], limit: int) -> str | None:
    """The first top-level argument of *params* whose string is over *limit*."""
    for key, value in params.items():
        if isinstance(value, str) and len(value) > limit and _FIELD_RE.match(str(key)):
            return str(key)
    return None


# ── Size refusals the dashboard owner may turn into a row ───────────────────
#
# shortcut: held in gateway memory only, so a refusal made in another process
# (a CLI chat) or before a restart is not offered; upgrade to a gateway-owned
# store if owners report a refusal they could not find.
_REFUSALS: OrderedDict[BodyField, str] = OrderedDict()
_REFUSALS_LOCK = threading.Lock()


def record_size_refusal(server: str, tool: str, field: str) -> None:
    """Remember that the gate refused this trusted ``(server, tool, field)`` for size."""
    if not is_valid_row(server, tool, field):
        return
    row = BodyField(server, tool, field)
    seen = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _REFUSALS_LOCK:
        _REFUSALS.pop(row, None)
        _REFUSALS[row] = seen
        while len(_REFUSALS) > _MAX_REFUSALS:
            _REFUSALS.popitem(last=False)


def size_refusals() -> list[dict[str, str]]:
    """The remembered refusals, newest first."""
    with _REFUSALS_LOCK:
        items = list(_REFUSALS.items())
    return [{**row.to_dict(), "refused_at": seen} for row, seen in reversed(items)]


def is_size_refusal(row: BodyField) -> bool:
    with _REFUSALS_LOCK:
        return row in _REFUSALS


def forget_size_refusal(row: BodyField) -> None:
    with _REFUSALS_LOCK:
        _REFUSALS.pop(row, None)


def named_size_refusal(
    params: Mapping[str, Any], server: str, tool_name: str, limit: int
) -> str | None:
    """The size refusal naming the oversize top-level field of a TRUSTED call, or ``None``.

    Records the refusal for the owner's allow button. ``None`` when no top-level
    string is over *limit* (a nested one), so the caller keeps its generic reason.
    """
    field = oversize_field(params, limit)
    if field is None:
        return None
    tool = _bare_tool(tool_name, server)
    record_size_refusal(server, tool, field)
    return size_refusal_reason(server, tool, field, len(params[field]), limit)


def size_refusal_reason(server: str, tool: str, field: str, length: int, limit: int) -> str:
    """The refusal that names the tool and field, and where the owner allows it."""
    return (
        f"Blocked: the {field!r} field of @{server}/{tool} is too large to "
        f"security-scan ({length} chars > {limit} limit); refused rather than left "
        "unscanned. If this field is a message or document body, the dashboard owner "
        "can allow it under Settings > Security > Message body fields"
    )
