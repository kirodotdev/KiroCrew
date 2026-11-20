"""ServiceNow record reads and search over ServiceNow's Table API. Read-only.

ServiceNow has no CLI, so the gateway calls the instance itself with an inbound
REST API key (Washington DC and later) sent as the ``x-sn-apikey`` header. The
key and the instance it belongs to live in Crew's vault, vault-only, as
``SERVICENOW_API_KEY`` and ``SERVICENOW_INSTANCE``; both are read on each
request, and the key is never logged or returned. Every error this module raises
is its own fixed text, never ServiceNow's.

The credential is bound to its instance: anyone can register a
``*.service-now.com`` instance, so a request whose host is not exactly the
stored ``SERVICENOW_INSTANCE`` is refused before any network call, and a
redirect is never followed.

A search reads ONE table from :data:`links.SERVICENOW_TABLES`. The URL path is
built from that constant and the validated host, the field list and paging are
the gateway's, and the encoded query is the gateway's own clauses with the raw
query's terms ANDed after them. A raw term can only narrow: ``^OR``, ``^NQ``,
ordering and every other keyword are refused, so no term can join a gateway
clause or start a second query (:func:`raw_terms`).

Importing this module installs ServiceNow into issue search and into the
single-issue read, as ``linear`` does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import string
from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from kiro_crew.config import loader as config_loader
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.source_providers import (
    LOGGER_NAME,
    cache,
    issue_search,
    links,
    projection,
    sanitize,
)
from kiro_crew.dashboard.source_providers.contract import SourceProviderError, SourceRef
from kiro_crew.dashboard.source_providers.issue_search import IssueQuery, IssueQueryError
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(LOGGER_NAME)

PROVIDER = "servicenow"
#: Vault-only names: never read from ``.env``, so never in the process environment.
API_KEY_NAME = "SERVICENOW_API_KEY"
INSTANCE_NAME = "SERVICENOW_INSTANCE"
_FETCH_TIMEOUT = 15  # seconds per HTTP call
# The fields a read returns; a raw query cannot change them.
_FIELDS = (
    "sys_id,number,short_description,description,active,state,priority,"
    "assigned_to,opened_by,sys_created_on,sys_updated_on,closed_at"
)
_MAX_PERSON_CHARS = 255
_MAX_RAW_TERMS = 20
_MAX_KEY_CHARS = 40
# A raw term is ``<field><operator><value>``. Fields are lower-case, as every
# ServiceNow column is, so no field can spell an upper-case keyword.
_FIELD_FIRST = frozenset(string.ascii_lowercase)
_FIELD_CHARS = frozenset(string.ascii_lowercase + string.digits + "_.")
_MAX_FIELD_CHARS = 128
# Keywords an encoded query reads at the start of a term (``^OR``, ``^NQ``,
# ``^EQ``, ``^ORDERBY``, ``^GROUPBY``, related-list and sub-queries). A field
# starting with one is refused in any letter case, in case a release reads them
# case-blind.
_KEYWORD_PREFIXES = (
    "or",
    "nq",
    "eq",
    "groupby",
    "rlquery",
    "endrlquery",
    "subquery",
    "endsubquery",
    "join",
    # IR_AND_OR_QUERY and friends (text-search pseudo-fields), and GOTO.
    "ir_",
    "goto",
)
# Longest first, so ``<=`` is not read as ``<``. ``DYNAMIC`` (a scripted filter)
# and the field-to-field and date-script operators are not offered.
_OPERATORS = (
    "!=",
    "<=",
    ">=",
    "=",
    "<",
    ">",
    # "does not contain": the condition builder writes it with a space.
    "NOT LIKE",
    "NOTLIKE",
    "LIKE",
    "STARTSWITH",
    "ENDSWITH",
    "NOT IN",
    "IN",
    "ISNOTEMPTY",
    "ISEMPTY",
    "BETWEEN",
)
_VALUELESS_OPERATORS = ("ISNOTEMPTY", "ISEMPTY")
# ``^`` separates terms and has no escape; ``%`` is refused so no value can carry
# an encoded separator past a second decode; a ``javascript:`` value runs on the
# instance.
_SCRIPT_PREFIX = "javascript:"
_ME = "javascript:gs.getUserID()"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_SETUP_MESSAGE = (
    "Add a ServiceNow credential to read ServiceNow records: create an inbound REST API "
    f"key on the instance and store it in Settings > Secrets as {API_KEY_NAME}, with the "
    f"instance host (like acme.service-now.com) as {INSTANCE_NAME}."
)
_INSTANCE_MESSAGE = (
    f"The stored {INSTANCE_NAME} is not a ServiceNow instance host like acme.service-now.com."
)
_UNBOUND_MESSAGE = "host is not the ServiceNow instance the stored credential belongs to."
_AUTH_MESSAGE = (
    f"ServiceNow rejected the stored API key. Replace {API_KEY_NAME} in Settings > Secrets "
    "with a current inbound REST API key."
)
_FORBIDDEN_MESSAGE = "The stored ServiceNow API key is not allowed to read this."
_RATE_LIMIT_MESSAGE = "ServiceNow's rate limit was reached. Try again in a minute."
_REDIRECT_MESSAGE = "ServiceNow answered with a redirect, which is not followed."


# ── Credential and transport ────────────────────────────────────────────────


def _vault_secret(vault: SecretVault, name: str) -> str:
    secret = vault.get(name)
    return secret.reveal().strip() if secret is not None else ""


def _stored_credential() -> tuple[str, str]:
    """``(instance, key)`` from the vault, ``""`` for each one absent or unreadable."""
    try:
        vault = SecretVault(config_loader.config_dir())
        return _vault_secret(vault, INSTANCE_NAME), _vault_secret(vault, API_KEY_NAME)
    except Exception:
        logger.debug("ServiceNow credential unavailable", exc_info=True)
        return "", ""


def bound_instance(value: str) -> str | None:
    """The stored instance as a host, or None when it is not one.

    ``acme.service-now.com`` or ``https://acme.service-now.com/``; nothing else
    (no path, port, userinfo or other scheme).
    """
    host = value.strip().lower().removeprefix("https://").removesuffix("/").rstrip(".")
    return host if links.is_servicenow_host(host) else None


async def _api_key(host: str) -> str:
    """The stored key, once *host* is the instance it belongs to.

    BLOCKING vault read, so it runs in a worker thread. Every refusal here comes
    before any network call.
    """
    instance, key = await asyncio.to_thread(_stored_credential)
    if not instance or not key:
        raise SourceProviderError(_SETUP_MESSAGE)
    bound = bound_instance(instance)
    if bound is None:
        raise SourceProviderError(_INSTANCE_MESSAGE)
    if host != bound:
        raise IssueQueryError(_UNBOUND_MESSAGE)
    # A header value holds no whitespace or control character; refuse the key
    # instead of letting the HTTP client raise with it in hand.
    if not key.isascii() or not key.isprintable() or " " in key:
        raise SourceProviderError(f"The stored {API_KEY_NAME} is not a ServiceNow API key.")
    return key


def table_url(host: str, table: str, sys_id: str = "") -> str:
    """The Table API URL for *table* (one of ``SERVICENOW_TABLES``) on *host*."""
    # The path's table is the constant's own string, never the caller's.
    path_table = links.SERVICENOW_TABLES[links.SERVICENOW_TABLES.index(table)]
    record = f"/{sys_id}" if sys_id else ""
    return f"https://{host}/api/now/table/{path_table}{record}"


async def _get(url: str, key: str) -> tuple[int, str, bytes]:
    """GET *url* (already encoded); ``(status, X-Total-Count, body)``, body capped."""
    headers = {"x-sn-apikey": key, "Accept": "application/json"}
    timeout = ClientTimeout(total=_FETCH_TIMEOUT)
    async with ClientSession(timeout=timeout) as session:
        async with session.get(
            URL(url, encoded=True), headers=headers, allow_redirects=False
        ) as resp:
            body = await _shared.read_capped_response(resp, sanitize._MAX_PAYLOAD_BYTES)
            return resp.status, resp.headers.get("X-Total-Count", ""), body


def _with_params(url: str, params: list[tuple[str, str]]) -> str:
    return f"{url}?{urlencode(params, safe='', quote_via=quote)}"


async def _read(
    host: str, url: str, params: list[tuple[str, str]], *, refused: str, list_read: bool = False
) -> tuple[Any, str]:
    """Run one read; ``(result, X-Total-Count)``.

    *refused* is the message for a 400 or 404. ServiceNow's own error text is
    never shown: it can repeat the submitted values.
    """
    key = await _api_key(host)
    try:
        status, total, raw = await _get(_with_params(url, params), key)
    except (ClientError, asyncio.TimeoutError) as exc:
        raise SourceProviderError(f"Could not reach ServiceNow: {type(exc).__name__}") from None
    if len(raw) > sanitize._MAX_PAYLOAD_BYTES:
        raise SourceProviderError("ServiceNow's answer exceeds the size limit.")
    if 300 <= status < 400:
        raise SourceProviderError(_REDIRECT_MESSAGE)
    if status == 401:
        raise SourceProviderError(_AUTH_MESSAGE)
    if status == 403:
        raise SourceProviderError(_FORBIDDEN_MESSAGE)
    if status == 429:
        raise SourceProviderError(_RATE_LIMIT_MESSAGE)
    if status == 404 and list_read:
        # Some releases answer a list read that matches nothing with 404.
        return [], "0"
    if status in (400, 404):
        raise SourceProviderError(refused)
    if status != 200:
        raise SourceProviderError(f"ServiceNow returned HTTP {status} for this request.")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise SourceProviderError("ServiceNow returned HTTP 200 with no readable answer.") from None
    if not isinstance(payload, dict) or "result" not in payload:
        raise SourceProviderError("ServiceNow returned an invalid answer.")
    return payload["result"], total


def _base_params() -> list[tuple[str, str]]:
    # ``all`` answers each field as {"value", "display_value"}: raw values for
    # active, ids and times, display values for people, state and priority.
    return [
        ("sysparm_fields", _FIELDS),
        ("sysparm_display_value", "all"),
        ("sysparm_exclude_reference_link", "true"),
    ]


# ── Row fields ──────────────────────────────────────────────────────────────


def _value(row: dict[str, Any], field: str) -> str:
    cell = row.get(field)
    value = cell.get("value") if isinstance(cell, dict) else cell
    return value if isinstance(value, str) else ""


def _display(row: dict[str, Any], field: str) -> str:
    cell = row.get(field)
    value = cell.get("display_value") if isinstance(cell, dict) else cell
    return value if isinstance(value, str) else ""


def _time(value: str) -> str:
    """ServiceNow's UTC ``YYYY-MM-DD HH:MM:SS`` as ISO 8601, else ``""``."""
    try:
        moment = datetime.strptime(value, _TIME_FORMAT)
    except ValueError:
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _closed(row: dict[str, Any]) -> bool:
    """``active`` is false: the same meaning on every ``task`` table."""
    return _value(row, "active") == "false"


def _key(row: dict[str, Any]) -> str:
    """The record number (``INC0010001``), checked before it is shown as the key."""
    number = _value(row, "number")
    ok = 0 < len(number) <= _MAX_KEY_CHARS and number.isascii() and number.isalnum()
    return number if ok else ""


def _number(key: str) -> int | None:
    """The digits of a record number (``INC0010001`` -> 10001), else None."""
    digits = key.lstrip(string.ascii_letters)
    return int(digits) if digits.isdigit() and digits.isascii() else None


def _priority(row: dict[str, Any]) -> str | None:
    return (_display(row, "priority") or None) if _value(row, "priority") else None


def _person(row: dict[str, Any], field: str) -> dict[str, str] | None:
    name = _display(row, field)
    # The Table API names no avatar; a person is their display name.
    return {"login": name, "name": name, "avatarUrl": ""} if name else None


# ── One record ──────────────────────────────────────────────────────────────


async def _fetch_servicenow_record(ref: SourceRef) -> dict[str, Any]:
    """Read one record in the single-issue payload shape. Unredacted: ``cache`` redacts."""
    table, sys_id = ref.owner, ref.repo
    if table not in links.SERVICENOW_TABLES or not links.is_servicenow_sys_id(sys_id):
        raise SourceProviderError("This is not a ServiceNow record this gateway reads.")
    result, _total = await _read(
        ref.host,
        table_url(ref.host, table, sys_id),
        _base_params(),
        refused="This ServiceNow record was not found, or the stored API key cannot read it.",
    )
    row = result if isinstance(result, dict) else {}
    if _value(row, "sys_id") != sys_id:
        raise SourceProviderError("This ServiceNow record was not found.")
    key = _key(row)
    closed = _closed(row)
    priority = _priority(row)
    assignee = _display(row, "assigned_to")
    return {
        "provider": PROVIDER,
        "url": ref.url,
        "number": _number(key) or 0,
        "title": _display(row, "short_description"),
        "description": _display(row, "description"),
        "state": "closed" if closed else "open",
        "stateReason": "completed" if closed else "",
        "author": _display(row, "opened_by"),
        "createdAt": _time(_value(row, "sys_created_on")),
        "updatedAt": _time(_value(row, "sys_updated_on")),
        "closedAt": _time(_value(row, "closed_at")),
        "closedBy": "",
        # ServiceNow has no labels. The issue payload has no priority field, so
        # it rides as a label, as Jira's and Linear's do.
        "labels": (
            [{"name": f"Priority: {priority}", "color": "", "description": ""}] if priority else []
        ),
        "assignees": [assignee] if assignee else [],
        "milestone": None,
        "commentCount": 0,
        "locked": False,
        "reactions": None,
        # shortcut: comments and work notes live in sys_journal_field, which this
        # read does not query, so the section is marked partial; read that table
        # if the issue view needs them.
        "comments": [],
        "linkedChanges": [],
        "partialSections": ["comments"],
        "failedSections": [],
    }


# ── Search ──────────────────────────────────────────────────────────────────


def _ascii_fold(char: str) -> str:
    """*char*, or the ASCII letter a case-blind compare would take it for
    (``ſ`` -> ``S``, ``ı`` -> ``I``, the Kelvin sign -> ``k``)."""
    if char.isascii():
        return char
    for cased in (char.upper(), char.lower()):
        if cased.isascii():
            return cased
    return char


def _has_script(value: str) -> bool:
    """Whether *value* holds ``javascript:`` in any letter case, Unicode look-alike
    case forms included, since the instance may compare it case-blind."""
    return _SCRIPT_PREFIX in "".join(map(_ascii_fold, value)).lower()


def _is_value(value: str) -> bool:
    """A value the gateway can put in one term: no separator, escape or script."""
    return value.isprintable() and "^" not in value and "%" not in value and not _has_script(value)


def _raw_term(term: str) -> str:
    """One raw term, checked. A term the instance parses differently from this
    check (another operator at the same place) is still ONE condition on one
    field, ANDed: only a keyword at the start of a term can join or drop another."""
    if term.lower().startswith(_KEYWORD_PREFIXES):
        raise IssueQueryError("raw cannot use ^OR, ^NQ, ORDERBY or other query keywords.")
    field_end = 0
    while field_end < len(term) and term[field_end] in _FIELD_CHARS:
        field_end += 1
    field, rest = term[:field_end], term[field_end:]
    if not 0 < len(field) <= _MAX_FIELD_CHARS or field[0] not in _FIELD_FIRST:
        raise IssueQueryError("each raw term must start with a lower-case field name.")
    if "." in field:
        # A dot-walk filters on another table's record (``caller_id.phone``), so
        # the match count would answer questions about that table.
        raise IssueQueryError("raw fields are this table's own columns; dot-walking is refused.")
    operator = next((op for op in _OPERATORS if rest.startswith(op)), None)
    if operator is None:
        raise IssueQueryError("raw uses an operator this endpoint does not allow.")
    value = rest[len(operator) :]
    if operator in _VALUELESS_OPERATORS and value:
        raise IssueQueryError("raw uses an operator this endpoint does not allow.")
    if not _is_value(value):
        raise IssueQueryError("raw values cannot hold ^, % or javascript:.")
    return term


def raw_terms(raw: str) -> list[str]:
    """Split a raw encoded query into terms that are each safe to AND.

    Each term is ``<field><operator><value>`` with a lower-case field and an
    operator from :data:`_OPERATORS`. ``^OR`` is refused: it binds to the term
    before it, and the instance drops a term naming an unknown field, so an OR
    could reach a gateway clause. ``^NQ`` (a second query ORed with the first),
    ``^EQ``, ordering and the other keywords are refused, and so is an empty
    term (``^^``).
    """
    terms = raw.split("^")
    if len(terms) > _MAX_RAW_TERMS:
        raise IssueQueryError(f"raw has more than {_MAX_RAW_TERMS} terms.")
    if any(not term for term in terms):
        raise IssueQueryError("raw has an empty term.")
    return [_raw_term(term) for term in terms]


def _person_term(field: str, value: str) -> str:
    if value == "me":
        return f"{field}={_ME}"
    column = "email" if "@" in value else "user_name"
    return f"{field}.{column}={value}"


def _text(text: str) -> str:
    """Free text as plain words: a separator or escape becomes a space."""
    return " ".join(text.replace("^", " ").replace("%", " ").split())


def encoded_query(query: IssueQuery) -> str:
    """The ``sysparm_query``: the gateway's clauses, the raw terms, then its ordering.

    Every clause is ANDed. The free-text clause is the one ``^OR`` the gateway
    writes, between two of its own terms, and comes first so nothing precedes it.
    """
    terms: list[str] = []
    words = _text(query.text)
    if words:
        terms.append(f"short_descriptionLIKE{words}^ORdescriptionLIKE{words}")
    if query.state != "all":
        terms.append(f"active={'false' if query.state == 'closed' else 'true'}")
    if query.assignee:
        terms.append(_person_term("assigned_to", query.assignee))
    if query.author:
        terms.append(_person_term("opened_by", query.author))
    if query.raw:
        terms.extend(raw_terms(query.raw))
    column = "sys_created_on" if query.sort == "created" else "sys_updated_on"
    terms.append(f"{'ORDERBY' if query.order == 'asc' else 'ORDERBYDESC'}{column}")
    return "^".join(terms)


def validate_search(query: IssueQuery) -> IssueQuery:
    query = replace(query, scope=query.scope.lower())
    if query.scope not in links.SERVICENOW_TABLES:
        raise IssueQueryError(
            f"scope must be a ServiceNow table: {', '.join(links.SERVICENOW_TABLES)}."
        )
    if not query.host:
        raise IssueQueryError("host is required for ServiceNow.")
    # parse_issue_query has lower-cased the host and dropped a trailing dot.
    if not links.is_servicenow_host(query.host):
        raise IssueQueryError("host must be a ServiceNow instance like acme.service-now.com.")
    if query.labels:
        raise IssueQueryError("ServiceNow records have no labels; omit labels.")
    for field, value in (("assignee", query.assignee), ("author", query.author)):
        if value and (len(value) > _MAX_PERSON_CHARS or not _is_value(value)):
            raise IssueQueryError(
                f"{field} must be me, a ServiceNow user name or an email address."
            )
    if _has_script(query.text):
        raise IssueQueryError("text cannot hold javascript:.")
    if query.raw:
        raw_terms(query.raw)
    return replace(query, scope=links.SERVICENOW_TABLES[links.SERVICENOW_TABLES.index(query.scope)])


def search_params(query: IssueQuery) -> list[tuple[str, str]]:
    return [
        ("sysparm_query", encoded_query(query)),
        *_base_params(),
        ("sysparm_limit", str(query.limit)),
        ("sysparm_offset", str((query.page - 1) * query.limit)),
    ]


async def fetch_search(query: IssueQuery) -> dict[str, Any]:
    """Read one page of the table, unredacted (``cache`` redacts)."""
    result, total = await _read(
        query.host,
        table_url(query.host, query.scope),
        search_params(query),
        refused="ServiceNow refused this search. Check the raw encoded query field names.",
        list_read=True,
    )
    if not isinstance(result, list):
        raise SourceProviderError("ServiceNow returned an invalid record list")
    return {"rows": result, "total": total}


def _summary(query: IssueQuery, row: dict[str, Any]) -> dict[str, Any] | None:
    sys_id = _value(row, "sys_id")
    key = _key(row)
    if not links.is_servicenow_sys_id(sys_id) or not key:
        return None
    closed = _closed(row)
    assignee = _person(row, "assigned_to")
    return {
        "provider": PROVIDER,
        # Built from the validated host and table and the checked sys_id, never
        # from a link ServiceNow returns.
        "url": links.servicenow_record_url(query.host, query.scope, sys_id),
        "key": key,
        "number": _number(key),
        "title": _display(row, "short_description"),
        "state": "closed" if closed else "open",
        "statusLabel": _display(row, "state") or ("Closed" if closed else "Open"),
        "labels": [],
        "assignees": [assignee] if assignee else [],
        "author": _person(row, "opened_by"),
        "priority": _priority(row),
        # shortcut: the Table API reports no comment count; read
        # sys_journal_field per record if cards need it.
        "commentCount": 0,
        "createdAt": _time(_value(row, "sys_created_on")),
        "updatedAt": _time(_value(row, "sys_updated_on")),
        # Plain text already. Not cut here; finish_issue_list cuts it after
        # cache.search_issues redacts.
        "excerpt": _display(row, "description"),
        "project": query.scope,
        "linkedChanges": [],
    }


def project_search(query: IssueQuery, data: dict[str, Any]) -> dict[str, Any]:
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise SourceProviderError("ServiceNow returned an invalid record list")
    issues = [
        summary
        for summary in (
            _summary(query, row) for row in projection._as_list(rows) if isinstance(row, dict)
        )
        if summary
    ]
    total_text = str(data.get("total") or "")
    total = int(total_text) if total_text.isascii() and total_text.isdigit() else None
    end = (query.page - 1) * query.limit + len(rows)
    if total is not None:
        more = bool(rows) and end < total
    else:
        more = len(rows) >= query.limit
    return issue_search._result(query, issues, query.page + 1 if more else None, total)


issue_search.install_provider(
    PROVIDER,
    issue_search.IssueProvider(
        validate=validate_search, fetch=fetch_search, project=project_search
    ),
)
cache.install_issue_reader(PROVIDER, _fetch_servicenow_record)
