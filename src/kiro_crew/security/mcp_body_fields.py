"""Message-body fields of third-party MCP tools: scanned as documents, not as commands.

The always-enforced tool_input scan in ``llm_helpers._resolve_permission`` refuses
any string over ``MAX_SCANNABLE_COMMAND_CHARS`` (20 KiB) before a detector runs,
because the shell-command matchers are what that ceiling bounds. A mail tool's
``send_email`` with a 40-100 KB HTML body was therefore always refused. This module
names the fields that are a message or document BODY. The gate judges them with
the same scan as a Kiro Crew document body (``MCP_DOCUMENT_BODY_FIELDS``):
``_first_tool_input_denial(..., command_rules=False)``, the size ceiling and the
path tier only, with the 256 KiB source-body ceiling
(``MAX_SCANNABLE_SOURCE_BODY_CHARS``) in place of 20 KiB. A hard credential (a
provider token, an AWS secret, a private key; :func:`body_field_match_refusal`),
in the text or in a link, refuses a body over 20 KiB, which the size ceiling
refused before it was a row. Every other match,
and any match in a shorter body, is logged as a warning. A body field never
reaches the shell matcher or the deny-rule list.
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
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_plus

from kiro_crew.mcp_gateway.tool_surface import admits_name
from kiro_crew.platform.tool_paths import strip_server_qualifier
from kiro_crew.security import pem_scan
from kiro_crew.security.exfil import _URL_SCAN_RE, scan_exfiltration_urls
from kiro_crew.security.exfil_url_rules import _MAX_URL_DECODE_PASSES
from kiro_crew.security.redaction import (
    redact_credentials,
    redact_credentials_with_records,
)
from kiro_crew.security.redaction_entropy import _looks_like_secret_key

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
        "bash",
        "cmd",
        "cmdline",
        "cmds",
        "code",
        "command",
        "commandline",
        "commands",
        "eval",
        "exec",
        "expression",
        "expressions",
        "powershell",
        "pwsh",
        "queries",
        "query",
        "script",
        "scripts",
        "sh",
        "shell",
        "sql",
        "statement",
        "statements",
        "zsh",
    }
)

#: Stored fields already warned about, so a dropped row is logged once per process.
_warned_dropped: set[tuple[object, object, object]] = set()


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


def _warn_dropped_executable(row: dict[str, Any]) -> None:
    """Log, once per process, a stored row dropped because its field names something run."""
    server, tool, field = seen = (row.get("server"), row.get("tool"), row.get("field"))
    if (
        isinstance(field, str)
        and _is_field_name(field)
        and is_executable_field(field)
        and is_valid_row(server, tool, "body")
        and seen not in _warned_dropped
        and len(_warned_dropped) < _MAX_OWNER_ROWS
    ):
        _warned_dropped.add(seen)
        logger.warning(
            "mcp_body_fields.json: dropped %r on %r/%r: a server runs it", field, server, tool
        )


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
            _warn_dropped_executable(row)
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


#: Rule ids of the credential check (:class:`CredentialMatch` ``rule``, the names
#: ``redaction_structured.compat`` gives the rule table's rows) whose detector shape tells a
#: token from ordinary mail text: an alphanumeric-only body of a fixed minimum
#: length (GitHub, npm, DigitalOcean), a labelled AWS secret, and a whole private
#: key block (see :func:`_record_is_hard`). The generic kinds are left out (a JWT,
#: a generic ``credential_pattern``, an encoded
#: blob, a ``?token=`` value, URL userinfo, an AWS key id), and so is every
#: provider rule whose body also takes ``-`` or ``.`` so that a hyphenated word or
#: an AWS ARN matches it, or that names a key which is not a live secret:
#: ``telegram_bot_token``, ``pypi_token``, ``sendgrid_key``, ``stripe_key``,
#: ``slack_token``, ``discord_bot_token``, ``gitlab_token``, ``openai_key``,
#: ``anthropic_key``, ``google_oauth_secret`` and the labelled
#: ``aws_session_token`` (``SessionToken: expired`` matches it).
_HARD_CREDENTIAL_RULES = frozenset(
    {
        "aws_secret_access_key",
        "private_key",
        "github_token",
        "npm_token",
        "digitalocean_token",
    }
)


def _has_credential(text: str) -> bool:
    """Whether the credential check finds a credential in *text*.

    The check is the rule table, which masks only a value that grants access, so
    an inline image, a document link, an SRI digest or an ARN is not a hit.
    """
    return redact_credentials(text)[0] != text


def _link_decodings(text: str) -> Iterator[str]:
    """Each percent-decoded form of every link in *text*, up to the exfiltration
    check's decode budget, so a token written as ``ghp%5F...`` is read as written.
    Each pass also yields the form-decoded form (``+`` read as a space, as
    ``urlencode`` writes it) when it differs; the percent-only form stays, so a
    literal ``+`` in a token still matches."""
    for m in _URL_SCAN_RE.finditer(text):
        url = m.group(0)
        for _ in range(_MAX_URL_DECODE_PASSES):
            plus = unquote_plus(url)
            decoded = unquote(url)
            if plus != decoded:
                yield plus
            if decoded == url:
                break
            url = decoded
            yield url


#: The characters of a PEM body line (base64).
_PEM_BODY_CHARS = frozenset(string.ascii_letters + string.digits + "+/=")
#: The shortest line counted as key material. A PEM body line is 64 characters
#: (the last may be shorter); a single word after a header in prose is not one.
#: shortcut: a key cut to one short last line passes, revisit if one is reported.
_PEM_MIN_LINE = 20
#: How much of a key body is joined before it is judged; a key's first lines decide.
_PEM_BODY_JUDGED = 4096


def _credential_matches(text: str) -> Iterator[tuple[str, str, bool]]:
    """``(rule, value, starts_word)`` of every credential-check match in *text*, in
    text order. *starts_word* is whether the match is not right after a letter, a
    digit, ``_`` or ``-`` (:func:`_starts_a_word`). Each match is found from the end
    of the one before, so the walk is linear in *text*."""
    records = redact_credentials_with_records(text)[2]
    cursor = 0
    for rec in records:
        at = text.find(rec.value, cursor) if rec.value else -1
        if at != -1:
            cursor = at + len(rec.value)
        yield rec.rule, rec.value, at <= 0 or _starts_a_word(text[max(at - 2, 0) : at])


def _is_whole_key_block(rule: str, value: str) -> bool:
    """Whether a credential-check match holds key material: some header in it is
    followed, on its own line and the next ones past any ``Proc-Type:`` and
    ``DEK-Info:`` lines, by PEM base64 lines that together are at least
    :data:`_PEM_MIN_LINE` long and :func:`_looks_random`. The lines are joined
    before they are judged, so a key wrapped at any width counts. JSON-escaped
    line breaks (``\\n``) count as line breaks.

    The detector also matches a header line on its own (a truncated key), and from a
    header to an END marker named later in prose, which a tutorial is. The rule
    table records a key block's body without its armour lines, so a
    ``private_key`` match (*rule*) is judged as the body it is.
    """
    text = value.replace("\\r\\n", "\n").replace("\\n", "\n")
    if rule == "private_key" and _holds_key_material(text):
        return True
    return any(_holds_key_material(text[h.end() :]) for h in pem_scan._HEADER_RE.finditer(text))


def _holds_key_material(text: str) -> bool:
    """Whether the lines of *text*, up to an END marker or the next header, are PEM
    base64 that together is at least :data:`_PEM_MIN_LINE` long and
    :func:`_looks_random` (see :func:`_is_whole_key_block`)."""
    body = ""
    for n, raw in enumerate(text.splitlines()):
        if n and raw.strip().startswith("-----"):
            break  # an END marker or the next header ends the body
        line = raw.split("-----", 1)[0].strip()
        if not line or line.startswith(("Proc-Type:", "DEK-Info:")):
            continue
        if not set(line) <= _PEM_BODY_CHARS:
            break
        body += line
        if "-----" in raw or len(body) >= _PEM_BODY_JUDGED:
            break
    return len(body) >= _PEM_MIN_LINE and _looks_random(body)


#: The fewest different characters a random token body or key line holds.
_MIN_DISTINCT_CHARS = 8


def _looks_random(text: str) -> bool:
    """Whether *text* has a letter, a digit and at least :data:`_MIN_DISTINCT_CHARS`
    different characters: a filler run (``xxxx``, ``0000``, ``AAAA``) does not.
    shortcut: a placeholder with digits and many letters (``abc...xyz0123456789``,
    ``YourBase64Key...``) still counts; revisit if one is reported refused."""
    return (
        any(c.isdigit() for c in text)
        and any(c.isalpha() for c in text)
        and len(set(text)) >= _MIN_DISTINCT_CHARS
    )


def _labelled_value(value: str) -> str:
    """The value of a labelled AWS match without what the label rule also took: a
    closing quote, markup (``</pre>``), a further query parameter (``&ref=``) and
    trailing punctuation. ``+``, ``/`` and ``=`` belong to a secret and stay."""
    value = unquote(value).strip("\"'")
    for stop in ("<", "&"):
        value = value.partition(stop)[0]
    return value.rstrip(".,;:!?)]}\"'")


def _is_hard_token(rule: str, value: str, starts_word: bool) -> bool:
    """Whether a match of one of the token kinds of :data:`_HARD_CREDENTIAL_RULES`
    is hard. The AWS label rule takes any value after the label (up to
    markup such as ``</pre>``), so a labelled secret counts only when it has the
    bare secret key's shape and is not the AWS docs' ``EXAMPLEKEY``. A token counts
    only when the part after its last ``_`` looks random (:func:`_looks_random`), so
    a placeholder such as ``ghp_xxxx...`` in a how-to is sent, as on main, and only
    when it *starts_word* (:func:`_starts_a_word`)."""
    if rule == "aws_secret_access_key":
        secret = _labelled_value(value)
        return _looks_like_secret_key(secret) and "EXAMPLE" not in secret
    if rule in _TOKEN_RULES:
        return starts_word and _looks_random(value.rsplit("_", 1)[-1])
    return False


#: The hard kinds that are a prefixed token (no label, no key block).
_TOKEN_RULES = _HARD_CREDENTIAL_RULES - {"aws_secret_access_key", "private_key"}
#: The characters of a base64url run besides letters and digits.
_RUN_CHARS = "_-"


def _starts_a_word(before: str) -> bool:
    """Whether a match right after *before* (the one or two characters ahead of
    it) starts a word: the last one is not a letter, a digit, ``_`` or ``-``, or
    the two are a JSON or C escape for a line break or tab (``\\n``, ``\\t``,
    ``\\r``), so a token at the start of a line in a pasted log still counts.
    Random base64url data (a click-tracking ``upn=`` value) can hold ``npm_`` and
    24 letters and digits by chance; a real token starts at a line, a space,
    ``=``, ``:`` or a quote."""
    if before[-2:] in ("\\n", "\\t", "\\r"):
        return True
    # ASCII only: prose with no spaces between words (Chinese, Japanese) puts a
    # token right after a letter, and a base64url run never holds a non-ASCII one.
    last = before[-1:]
    return not ((last.isascii() and last.isalnum()) or last in _RUN_CHARS)


#: The characters a wrapping match is split at before its words are judged again.
_WORD_BREAKS = str.maketrans(":=@/", "    ")


def _record_is_hard(rule: str, value: str, starts_word: bool) -> bool:
    """Whether one credential-check match is a :data:`_HARD_CREDENTIAL_RULES` kind.

    Any match that holds a whole key block is hard, whatever rule id the
    credential check gave it (a key block far after a header named in prose is
    labelled ``credential_pattern``). Any match that is not hard itself, a key
    block or a labelled AWS value that
    failed its shape check included, can enclose a hard token in a wider span
    (``https://user:TOKEN@``, ``Bearer TOKEN``, ``SessionToken: TOKEN``), so each
    word of it, split at ``:``, ``=``, ``@``, ``/`` and whitespace, is judged again
    by the whole credential check, as written and percent-decoded. A token match
    is judged where it stands (*starts_word*) and its words are not judged again, so a
    token shape inside random data is not cut loose from what it sits in.
    """
    if _is_whole_key_block(rule, value) or _is_hard_token(rule, value, starts_word):
        return True
    if rule in _TOKEN_RULES:
        return False
    for word in value.translate(_WORD_BREAKS).split():
        word = word.strip("\"'-")
        for _ in range(_MAX_URL_DECODE_PASSES + 1):
            if any(_is_hard_token(r, v, w) for r, v, w in _credential_matches(word)):
                return True
            decoded = unquote(word)
            if decoded == word:
                break
            word = decoded
    return False


def _holds_hard_credential(text: str) -> bool:
    """Whether the credential check finds a :data:`_HARD_CREDENTIAL_RULES` kind in
    *text* or in a percent-decoded form of one of its links."""
    return any(
        _record_is_hard(rule, value, starts_word)
        for form in (text, *_link_decodings(text))
        for rule, value, starts_word in _credential_matches(form)
    )


def body_field_match_refusal(
    bodies: Mapping[str, str], server: str, tool: str, command_limit: int
) -> str | None:
    """The refusal for a listed body field over *command_limit* that holds a hard
    credential, else ``None``.

    The gate judges the *bodies* (field name to text) with the one document-body
    scan (``_first_tool_input_denial(..., command_rules=False)``: size ceiling and
    path tier) and calls this only for a call that scan let through. *command_limit*
    is the 20 KiB command-scan ceiling: without a row, a field over it is refused for
    its size, so for such a field a credential of a :data:`_HARD_CREDENTIAL_RULES`
    kind refuses the call, in the text or in a link. Every other hit there is logged
    and allowed, as on main: the generic credential kinds and every exfiltration
    rule fire on ordinary mail (a safe link, a presigned image, a signature block)
    with no secret in it. A field within the limit got the full command scan
    without a row, which has no credential or link check, so any match there is
    logged and allowed. The warning and the refusal name the server, tool, field
    and check, never the body or the match.
    """
    bare = _bare_tool(tool, server)
    for field, s in bodies.items():
        has_credential = _has_credential(s)
        has_link = bool(scan_exfiltration_urls(s))
        if (has_credential or has_link) and len(s) > command_limit and _holds_hard_credential(s):
            logger.warning(
                "message body field %r of @%s/%s matched the credential check; refused",
                field,
                server,
                bare,
            )
            return (
                f"Blocked: the {field!r} field of @{server}/{bare} matched the credential "
                f"check and is over the {command_limit} char command-scan limit; "
                "refused rather than sent"
            )
        for check, hit in (("credential", has_credential), ("exfiltration-URL", has_link)):
            if hit:
                logger.warning(
                    "message body field %r of @%s/%s matched the %s check; allowed",
                    field,
                    server,
                    bare,
                    check,
                )
    return None


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
