"""Message-body fields of third-party MCP tools: scanned as documents, not as commands.

The always-enforced tool_input scan in ``llm_helpers._resolve_permission`` refuses
any string over ``MAX_SCANNABLE_COMMAND_CHARS`` (20 KiB) before a detector runs,
because the shell-command matchers are what that ceiling bounds. A mail tool's
``send_email`` with a 40-100 KB HTML body was therefore always refused. This module
names the fields that are a message or document BODY. The gate judges them with
the same scan as a Kiro Crew document body (``MCP_DOCUMENT_BODY_FIELDS``):
``_first_tool_input_denial(..., command_rules=False)``, the size ceiling and the
path tier only, with the 256 KiB source-body ceiling
(``MAX_SCANNABLE_SOURCE_BODY_CHARS``) in place of 20 KiB. A credential or a link
that carries data out is logged as a warning (:func:`warn_body_field_matches`),
never refused. A body field never reaches the shell matcher or the deny-rule list.
Every other argument of the call keeps the full scan. Design: https://github.com/kirodotdev/KiroCrew/issues/8053.

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
import string
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.mcp_gateway.tool_surface import admits_name
from kiro_crew.platform.tool_paths import strip_server_qualifier
from kiro_crew.security.exfil import scan_exfiltration_urls
from kiro_crew.security.redaction import redact_credentials

logger = logging.getLogger(__name__)

#: The dashboard route of the Settings > Security > Message body fields section.
SETTINGS_ROUTE = "/settings/security/bodyFields"

#: An MCP config server name's characters. Mirrored here because importing a
#: dashboard handler into the security gate would reverse the dependency and
#: load aiohttp.
_SERVER_FIRST = frozenset(string.ascii_letters + string.digits + "@")
_SERVER_CHARS = frozenset(string.ascii_letters + string.digits + "@/_.:-")
#: A top-level argument name's characters.
_FIELD_CHARS = frozenset(string.ascii_letters + string.digits + "_-")
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


#: Words that name an argument a server RUNS (a command line, SQL, code, argv).
#: A field named by one of them is never a body row: the size refusal does not
#: offer it, the route refuses to allow it, and a stored one is dropped on read,
#: so it keeps the full command scan.
_EXECUTABLE_WORDS = frozenset(
    {
        "args",
        "argv",
        "cmd",
        "code",
        "command",
        "eval",
        "exec",
        "expression",
        "query",
        "script",
        "shell",
        "sql",
        "statement",
    }
)


def _is_server_name(server: str) -> bool:
    return (
        0 < len(server) <= 128
        and server[0] in _SERVER_FIRST
        and all(c in _SERVER_CHARS for c in server)
    )


def _is_field_name(field: str) -> bool:
    return 0 < len(field) <= 64 and all(c in _FIELD_CHARS for c in field)


def _field_words(field: str) -> list[str]:
    """*field* split at ``_``, ``-`` and a lower-or-digit to upper case step."""
    words: list[str] = []
    current = ""
    for c in field:
        if c in "_-":
            words.append(current)
            current = ""
            continue
        if c.isupper() and current and (current[-1].islower() or current[-1].isdigit()):
            words.append(current)
            current = ""
        current += c
    words.append(current)
    return [w.lower() for w in words if w]


def is_executable_field(field: str) -> bool:
    """Whether *field* names, as a whole or by one of its words, an argument a server runs."""
    words = set(_field_words(field))
    return field.lower() in _EXECUTABLE_WORDS or not words.isdisjoint(_EXECUTABLE_WORDS)


def is_kirocrew_server(server: str) -> bool:
    """Whether *server* is one of Kiro Crew's own (``kirocrew-*``) MCP servers.

    Their fields follow ``platform.tool_paths.MCP_DOCUMENT_BODY_FIELDS`` and its
    carve-outs (a cron script job's ``message`` is its arguments), so they never
    get a row here. The prefix covers every managed name
    (``mcp_discovery_managed._MANAGED_SERVER_NAMES``) and any added later.
    """
    return server.startswith("kirocrew-")


def is_valid_row(server: object, tool: object, field: object) -> bool:
    """Whether the three values make a row this module will store."""
    return (
        isinstance(server, str)
        and isinstance(tool, str)
        and isinstance(field, str)
        and _is_server_name(server)
        and ".." not in server
        and not is_kirocrew_server(server)
        and bool(tool)
        and admits_name(tool)
        and _is_field_name(field)
        and not is_executable_field(field)
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


def owner_rows(*, strict: bool = False) -> list[dict[str, str]]:
    """The owner-added rows, read fresh. Fails soft to none.

    *strict* is the read a rewrite starts from: only a missing file reads as
    empty, and any other read error raises ``OSError`` so the change is abandoned
    instead of writing an empty list over the owner's rows.
    """
    try:
        raw = json.loads(store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        if strict:
            raise OSError("mcp_body_fields.json is unreadable") from exc
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
    """Rewrite the store in place, so a running Linux sandbox's read-only mount of
    it stays sealed; a torn read during the rewrite reads as no owner rows.
    ``restrict_to_owner=True`` is the fail-loud lockdown the sibling keystone stores
    use: the owner-only mode lands before any content."""
    from kiro_crew.atomic_write import rewrite_in_place

    rewrite_in_place(
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
        rows = owner_rows(strict=True)
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
        rows = owner_rows(strict=True)
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


def warn_body_field_matches(bodies: Mapping[str, str], server: str, tool: str) -> None:
    """Log one warning per listed body field that a credential or exfiltration check matches.

    The gate judges the *bodies* (field name to text) with the one document-body
    scan (``_first_tool_input_denial(..., command_rules=False)``: size ceiling and
    path tier) and calls this only for a call that scan let through. A credential
    or a link that carries data out is logged, never refused. The warning names
    the server, tool, field and check, never the body or the match.
    """
    for field, s in bodies.items():
        # The credential test is the rule table, which masks only a value that grants
        # access, so an inline image, a document link, an SRI digest or an ARN is not a hit.
        has_credential = redact_credentials(s)[0] != s
        for check, hit in (
            ("credential", has_credential),
            ("exfiltration-URL", bool(scan_exfiltration_urls(s))),
        ):
            if hit:
                logger.warning(
                    "message body field %r of @%s/%s matched the %s check; allowed",
                    field,
                    server,
                    _bare_tool(tool, server),
                    check,
                )


def oversize_field(params: Mapping[str, Any], limit: int) -> str | None:
    """The first top-level argument of *params* whose string is over *limit*.

    An executable field (:func:`is_executable_field`) is never named, so its
    refusal keeps the generic reason and offers nothing to allow.
    """
    for key, value in params.items():
        if (
            isinstance(value, str)
            and len(value) > limit
            and _is_field_name(str(key))
            and not is_executable_field(str(key))
        ):
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
    params: Mapping[str, Any],
    server: str,
    tool_name: str,
    limit: int,
    *,
    dashboard_origin: str = "",
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
    return size_refusal_reason(
        server, tool, field, len(params[field]), limit, dashboard_origin=dashboard_origin
    )


def size_refusal_reason(
    server: str, tool: str, field: str, length: int, limit: int, *, dashboard_origin: str = ""
) -> str:
    """The refusal that names the tool and field, and where the owner allows it.

    *dashboard_origin* (no token: the text can reach a shared channel) adds a link
    to the section; empty means the text names the section only.
    """
    where = "Settings > Security > Message body fields"
    if dashboard_origin:
        where += f" ({dashboard_origin.rstrip('/')}{SETTINGS_ROUTE})"
    return (
        f"Blocked: the {field!r} field of @{server}/{tool} is too large to "
        f"security-scan ({length} chars > {limit} limit); refused rather than left "
        "unscanned. If this field is a message or document body, the dashboard owner "
        f"can allow it in the dashboard under {where}"
    )
