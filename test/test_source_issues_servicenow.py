"""ServiceNow records: the single-record read and ``POST /api/source/issues`` search.

Every test drives the real routes against a fake Table API (the one
``servicenow._get`` seam) with the credential in a real vault, so the vault
wiring, the instance binding, the owner gate, the redaction in ``cache`` and the
error mapping are the product's own. One test runs the transport itself against
a local server to show a redirect is not followed.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_shared_fetch_state  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

from kiro_crew.dashboard.source_providers import cache, links, servicenow
from kiro_crew.secrets import SecretVault
from kiro_crew.testing.synthetic_credentials import github_classic

_KEY = "now_" + "k" * 40
_HOST = "acme.service-now.com"
_SECRET = github_classic(seed=112)
_SYS_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_URL = f"https://{_HOST}/incident.do?sys_id={_SYS_ID}"


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch, tmp_path):
    """An empty vault of our own and empty caches for every test."""
    _floor_monkeypatch.setattr(servicenow.config_loader, "config_dir", lambda: tmp_path)
    cache._SEARCH_CACHE.clear()
    yield
    cache._SEARCH_CACHE.clear()


def _store(tmp_path, instance: str | None = _HOST, key: str | None = _KEY) -> None:
    vault = SecretVault(tmp_path)
    if instance is not None:
        vault.set_sync(servicenow.INSTANCE_NAME, instance)
    if key is not None:
        vault.set_sync(servicenow.API_KEY_NAME, key)


@pytest.fixture
def stored(tmp_path) -> None:
    _store(tmp_path)


def _cell(value: str, display: str | None = None) -> dict[str, str]:
    return {"value": value, "display_value": value if display is None else display}


def _row(n: int = 1, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sys_id": _cell(f"{n:032x}"),
        "number": _cell(f"INC{n:07d}"),
        "short_description": _cell(f"Record {n}"),
        "description": _cell("Printer on 3rd floor\nis on fire"),
        "active": _cell("true"),
        "state": _cell("2", "In Progress"),
        "priority": _cell("1", "1 - Critical"),
        "assigned_to": _cell("0" * 32, "Alice Admin"),
        "opened_by": _cell("1" * 32, "Bob Caller"),
        "sys_created_on": _cell("2026-11-01 10:00:00"),
        "sys_updated_on": _cell("2026-11-09 11:30:15"),
        "closed_at": _cell(""),
        # Anything ServiceNow says about where the record lives is ignored.
        "link": "https://evil.example/incident.do",
    }
    row.update(extra)
    return row


class _Table:
    """A fake Table API: one answer per call in order, records each URL and key."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.urls: list[str] = []
        self.keys: list[str] = []

    async def __call__(self, url: str, key: str) -> tuple[int, str, bytes]:
        self.urls.append(url)
        self.keys.append(key)
        answer = self.answers.pop(0) if self.answers else {"result": []}
        if isinstance(answer, BaseException):
            raise answer
        status, total, payload = answer if isinstance(answer, tuple) else (200, "", answer)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, total, raw

    def params(self, index: int = -1) -> dict[str, str]:
        return dict(parse_qsl(urlsplit(self.urls[index]).query, keep_blank_values=True))

    def path(self, index: int = -1) -> str:
        parts = urlsplit(self.urls[index])
        return f"{parts.scheme}://{parts.netloc}{parts.path}"


@pytest.fixture
def table(monkeypatch) -> _Table:
    fake = _Table()
    monkeypatch.setattr(servicenow, "_get", fake)
    return fake


async def _post(path: str, body: Any) -> tuple[int, Any]:
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(path, json=body)
        return response.status, await response.json()


async def _search(**body: Any) -> tuple[int, Any]:
    request = {"provider": "servicenow", "scope": "incident", "host": _HOST, **body}
    return await _post("/api/source/issues", request)


# ── The credential and its instance ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("instance", "key"), [(None, None), (_HOST, None), (None, _KEY)])
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/source/issues", {"provider": "servicenow", "scope": "incident", "host": _HOST}),
        ("/api/source/issue", {"url": _URL}),
    ],
)
async def test_a_missing_credential_asks_for_one_before_any_call(
    tmp_path, table, instance, key, path, body
) -> None:
    _store(tmp_path, instance, key)
    status, payload = await _post(path, body)
    assert status == 503
    assert payload["code"] == "provider_error"
    assert payload["error"] == servicenow._SETUP_MESSAGE
    assert table.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/source/issues",
            {"provider": "servicenow", "scope": "incident", "host": "attacker.service-now.com"},
        ),
        ("/api/source/issue", {"url": _URL.replace("acme.", "attacker.")}),
    ],
)
async def test_another_instance_never_gets_the_credential(stored, table, path, body) -> None:
    # Anyone can register a *.service-now.com instance: only the stored one is read.
    status, payload = await _post(path, body)
    assert status == 400
    # The fixed text names no instance, the stored one included.
    assert payload["error"] == servicenow._UNBOUND_MESSAGE
    assert table.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "instance",
    [
        "evil.example",
        "acme.service-now.com:8443",
        "https://acme.service-now.com/path",
        "http://acme.service-now.com",
        "user@acme.service-now.com",
    ],
)
async def test_a_stored_instance_that_is_not_one_host_is_refused_unsent(
    tmp_path, table, instance
) -> None:
    _store(tmp_path, instance=instance)
    status, payload = await _search()
    assert status == 503
    assert payload["error"] == servicenow._INSTANCE_MESSAGE
    assert table.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "instance", ["acme.service-now.com", "ACME.Service-Now.com.", " https://acme.service-now.com/ "]
)
@pytest.mark.parametrize(
    "host", ["acme.service-now.com", "ACME.service-now.COM", "acme.service-now.com."]
)
async def test_spellings_of_the_bound_instance_read_it(tmp_path, table, instance, host) -> None:
    _store(tmp_path, instance=instance)
    status, _payload = await _search(host=host)
    assert status == 200
    assert table.path() == "https://acme.service-now.com/api/now/table/incident"
    assert table.keys == [_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        "",
        "service-now.com",
        ".service-now.com",
        "acme.service-now.com:443",
        "acme.service-now.com:8443",
        "u@acme.service-now.com",
        "u:p@acme.service-now.com",
        "acmé.service-now.com",
        "acme.service-now.com.evil.example",
        "evil.example/acme.service-now.com",
        "acme.service-now.com/api",
        "acme.service-now.com#x",
        "a.acme.service-now.com",
        "-acme.service-now.com",
        "acme.servicenow.com",
    ],
)
async def test_other_host_spellings_are_refused_before_any_call(stored, table, host) -> None:
    status, payload = await _search(host=host)
    assert status == 400 and payload["code"] == "invalid_request"
    # Refused by the host's own shape, before the vault is even read.
    assert payload["error"] != servicenow._UNBOUND_MESSAGE
    assert table.urls == []


@pytest.mark.asyncio
async def test_a_punycode_instance_is_only_read_when_it_is_the_stored_one(tmp_path, table) -> None:
    _store(tmp_path)
    status, _payload = await _search(host="xn--acm-bma.service-now.com")
    assert status == 400 and table.urls == []
    _store(tmp_path, instance="xn--acm-bma.service-now.com")
    status, _payload = await _search(host="xn--acm-bma.service-now.com")
    assert status == 200 and len(table.urls) == 1


@pytest.mark.asyncio
async def test_the_key_is_sent_only_as_the_header_and_never_returned(
    stored, table, caplog, _mock_source_sel
) -> None:
    # ServiceNow's own error text can repeat what it was sent; none of it comes back.
    leak = {"error": {"message": f"bad key {_KEY}", "detail": _KEY}, "status": "failure"}
    table.answers = [(400, "", leak), (401, "", leak)]
    caplog.set_level(logging.DEBUG)
    search_status, search = await _search()
    issue_status, issue = await _post("/api/source/issue", {"url": _URL})
    assert (search_status, issue_status) == (503, 503)
    assert table.keys == [_KEY, _KEY]
    assert all(_KEY not in url for url in table.urls)
    for text in (json.dumps(search), json.dumps(issue), caplog.text):
        assert _KEY not in text
    assert _KEY not in str(_mock_source_sel.log_api_access.call_args_list)
    assert issue["error"] == servicenow._AUTH_MESSAGE


@pytest.mark.asyncio
async def test_a_key_with_whitespace_is_refused_unsent(tmp_path, table) -> None:
    _store(tmp_path, key="now key")
    status, payload = await _search()
    assert status == 503
    assert "now key" not in payload["error"]
    assert table.urls == []


@pytest.mark.asyncio
async def test_the_transport_sends_the_header_and_follows_no_redirect() -> None:
    seen: list[tuple[str, str]] = []

    async def first(request: web.Request) -> web.Response:
        seen.append((request.path, request.headers.get("x-sn-apikey", "")))
        raise web.HTTPFound("/elsewhere")

    async def elsewhere(request: web.Request) -> web.Response:
        seen.append((request.path, request.headers.get("x-sn-apikey", "")))
        return web.json_response({"result": []})

    app = web.Application()
    app.router.add_get("/api/now/table/incident", first)
    app.router.add_get("/elsewhere", elsewhere)
    async with TestServer(app) as server:
        url = str(server.make_url("/api/now/table/incident")) + "?sysparm_query=active%3Dtrue"
        status, _total, _body = await servicenow._get(url, _KEY)
    assert status == 302
    assert seen == [("/api/now/table/incident", _KEY)]


# ── Errors ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ((302, "", b""), servicenow._REDIRECT_MESSAGE),
        ((401, "", {"error": {"message": "User Not Authenticated"}}), servicenow._AUTH_MESSAGE),
        ((403, "", {"error": {"message": "ACL"}}), servicenow._FORBIDDEN_MESSAGE),
        ((429, "", b"slow down"), servicenow._RATE_LIMIT_MESSAGE),
        (
            (400, "", {"error": {"message": "Invalid field 'x'"}}),
            "ServiceNow refused this search. Check the raw encoded query field names.",
        ),
        ((502, "", b"<html>bad gateway</html>"), "ServiceNow returned HTTP 502 for this request."),
        ((200, "", b"<html>login</html>"), "ServiceNow returned HTTP 200 with no readable answer."),
        ((200, "", {"records": []}), "ServiceNow returned an invalid answer."),
        ((200, "", {"result": {}}), "ServiceNow returned an invalid record list"),
        (
            aiohttp.ClientConnectionError("down"),
            "Could not reach ServiceNow: ClientConnectionError",
        ),
    ],
)
async def test_provider_errors_map_to_fixed_messages(stored, table, answer, expected) -> None:
    table.answers = [answer]
    status, payload = await _search()
    assert status == 503
    assert payload == {"error": expected, "code": "provider_error"}


@pytest.mark.asyncio
async def test_a_list_read_answered_404_is_empty(stored, table) -> None:
    table.answers = [(404, "", {"error": {"message": "No Record found"}})]
    status, payload = await _search()
    assert status == 200
    assert (payload["issues"], payload["nextCursor"]) == ([], None)


# ── One record ──────────────────────────────────────────────────────────────


def _record(**extra: Any) -> dict[str, Any]:
    return {"result": _row(1, sys_id=_cell(_SYS_ID), **extra)}


@pytest.mark.asyncio
async def test_a_record_url_reads_the_record_in_the_shared_shape(stored, table) -> None:
    table.answers = [_record()]
    status, payload = await _post(
        "/api/source/issue",
        {"url": f"https://ACME.service-now.com/incident.do?sys_id={_SYS_ID.upper()}"},
    )
    assert status == 200
    assert table.path() == f"https://{_HOST}/api/now/table/incident/{_SYS_ID}"
    assert table.params() == {
        "sysparm_fields": servicenow._FIELDS,
        "sysparm_display_value": "all",
        "sysparm_exclude_reference_link": "true",
    }
    assert payload["provider"] == "servicenow"
    assert payload["url"] == _URL
    assert payload["number"] == 1
    assert payload["title"] == "Record 1"
    assert payload["description"] == "Printer on 3rd floor\nis on fire"
    assert (payload["state"], payload["stateReason"]) == ("open", "")
    assert (payload["author"], payload["assignees"]) == ("Bob Caller", ["Alice Admin"])
    assert payload["labels"] == [{"name": "Priority: 1 - Critical", "color": "", "description": ""}]
    assert (payload["createdAt"], payload["updatedAt"]) == (
        "2026-11-01T10:00:00Z",
        "2026-11-09T11:30:15Z",
    )
    assert payload["comments"] == [] and payload["partialSections"] == ["comments"]


@pytest.mark.asyncio
async def test_an_inactive_record_is_closed(stored, table) -> None:
    table.answers = [_record(active=_cell("false"), closed_at=_cell("2026-11-10 08:00:00"))]
    _status, payload = await _post("/api/source/issue", {"url": _URL})
    assert (payload["state"], payload["stateReason"], payload["closedAt"]) == (
        "closed",
        "completed",
        "2026-11-10T08:00:00Z",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [{"result": _row(2)}, {"result": []}, (404, "", {"error": {"message": "No Record found"}})],
)
async def test_another_record_or_none_is_not_found(stored, table, answer) -> None:
    table.answers = [answer]
    status, payload = await _post("/api/source/issue", {"url": _URL})
    assert status == 503
    assert "not found" in payload["error"]
    assert "No Record" not in payload["error"]


@pytest.mark.asyncio
async def test_secrets_in_a_record_come_back_redacted(stored, table) -> None:
    table.answers = [
        _record(short_description=_cell(f"leaked {_SECRET}"), description=_cell(f"use {_SECRET}"))
    ]
    status, payload = await _post("/api/source/issue", {"url": _URL})
    assert status == 200
    assert _SECRET not in json.dumps(payload)


@pytest.mark.parametrize(
    "url",
    [
        _URL,
        f"https://ACME.Service-Now.com/incident.do?sys_id={_SYS_ID.upper()}",
        f"https://acme.service-now.com:443/incident.do?sys_id={_SYS_ID}",
        f"https://acme.service-now.com./incident.do/?sys_id={_SYS_ID}#tab",
    ],
)
def test_record_urls_normalize_to_one_ref(url) -> None:
    ref = links.parse_source_url(url)
    assert (ref.provider, ref.url, ref.host, ref.owner, ref.repo, ref.kind) == (
        "servicenow",
        _URL,
        _HOST,
        "incident",
        _SYS_ID,
        "issue",
    )


@pytest.mark.parametrize(
    "url",
    [
        f"https://acme.service-now.com/sys_user.do?sys_id={_SYS_ID}",
        f"https://acme.service-now.com/task.do?sys_id={_SYS_ID}",
        f"https://acme.service-now.com/x/../incident.do?sys_id={_SYS_ID}",
        f"https://acme.service-now.com//incident.do?sys_id={_SYS_ID}",
        f"https://acme.service-now.com/incident.do?sys_id={_SYS_ID}&sysparm_view=ess",
        f"https://acme.service-now.com/incident.do?sys_id={_SYS_ID}&sys_id={_SYS_ID}",
        f"https://acme.service-now.com/incident.do?sys_id={_SYS_ID[:-1]}",
        f"https://acme.service-now.com/incident.do?sys_id={_SYS_ID[:-1]}g",
        "https://acme.service-now.com/incident.do?sys_id=",
        "https://acme.service-now.com/incident.do",
        f"https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D{_SYS_ID}",
        f"https://acme.service-now.com/now/nav/ui/classic/params/target/incident.do%3Fsys_id%3D{_SYS_ID}",
        f"https://acme.service-now.com:8443/incident.do?sys_id={_SYS_ID}",
        f"https://u@acme.service-now.com/incident.do?sys_id={_SYS_ID}",
        f"http://acme.service-now.com/incident.do?sys_id={_SYS_ID}",
    ],
)
def test_other_servicenow_urls_are_refused(url) -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(url)


# ── Search ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_structured_filters_become_one_anded_query(stored, table) -> None:
    status, _payload = await _search(
        scope="Problem",
        state="closed",
        assignee="me",
        author="dana@acme.dev",
        text="disk ^NQ full 100%",
        sort="created",
        order="asc",
        limit=10,
        cursor="3",
    )
    assert status == 200
    assert table.path() == f"https://{_HOST}/api/now/table/problem"
    assert table.params() == {
        "sysparm_query": (
            "short_descriptionLIKEdisk NQ full 100^ORdescriptionLIKEdisk NQ full 100"
            "^active=false^assigned_to=javascript:gs.getUserID()^opened_by.email=dana@acme.dev"
            "^ORDERBYsys_created_on"
        ),
        "sysparm_fields": servicenow._FIELDS,
        "sysparm_display_value": "all",
        "sysparm_exclude_reference_link": "true",
        "sysparm_limit": "10",
        "sysparm_offset": "20",
    }


@pytest.mark.asyncio
async def test_default_search_is_open_records_newest_update_first(stored, table) -> None:
    await _search(assignee="alice")
    assert table.params()["sysparm_query"] == (
        "active=true^assigned_to.user_name=alice^ORDERBYDESCsys_updated_on"
    )
    assert table.params()["sysparm_limit"] == "25"


@pytest.mark.asyncio
@pytest.mark.parametrize("table_name", list(links.SERVICENOW_TABLES))
async def test_each_listed_table_is_read_at_its_own_path(stored, table, table_name) -> None:
    status, payload = await _search(scope=table_name)
    assert status == 200
    assert table.path() == f"https://{_HOST}/api/now/table/{table_name}"
    assert payload["scope"] == table_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        "sys_user",
        "task",
        "incident/../sys_user",
        "../sys_user",
        "incident/1",
        "incident?sysparm_limit=1",
        "incident#",
        "incident.do",
        "incident%2F..%2Fsys_user",
        "sys_user_has_role",
        "incident_task",
    ],
)
async def test_other_tables_are_refused_before_any_call(stored, table, scope) -> None:
    status, payload = await _search(scope=scope)
    assert status == 400 and payload["code"] == "invalid_request"
    assert table.urls == []


@pytest.mark.asyncio
async def test_raw_terms_are_anded_after_every_structured_filter(stored, table) -> None:
    status, _payload = await _search(
        assignee="me",
        raw="priority<=2^category=network^stateIN1,2^short_descriptionNOT LIKEvpn",
    )
    assert status == 200
    assert table.params()["sysparm_query"] == (
        "active=true^assigned_to=javascript:gs.getUserID()"
        "^priority<=2^category=network^stateIN1,2^short_descriptionNOT LIKEvpn"
        "^ORDERBYDESCsys_updated_on"
    )


@pytest.mark.asyncio
async def test_a_raw_separator_inside_a_value_stays_in_the_query(stored, table) -> None:
    # "&" or "#" in a value is encoded into sysparm_query, never a parameter of its own.
    await _search(raw="short_descriptionLIKEa&sysparm_limit=10000#x")
    params = table.params()
    assert params["sysparm_limit"] == "25"
    assert params["sysparm_query"].startswith("active=true^short_descriptionLIKEa&sysparm_limit")
    names = [pair.partition("=")[0] for pair in urlsplit(table.urls[-1]).query.split("&")]
    assert sorted(names) == sorted(table.params())
    assert urlsplit(table.urls[-1]).fragment == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        # A second query, ORed with the gateway's (``^NQ``), in any letter case.
        "priority=1^NQactive=false",
        "NQactive=false",
        "priority=1^nqactive=false",
        # An OR that would join the gateway's last clause.
        "ORactive=false",
        "priority=1^ORactive=false",
        "orassigned_toISEMPTY",
        # Ordering, grouping, the end marker and sub-queries.
        "ORDERBYnumber",
        "priority=1^ORDERBYDESCnumber",
        "GROUPBYstate",
        "priority=1^EQ",
        "RLQUERYtask_sla.task,>=1^ENDRLQUERY",
        "SUBQUERYsys_id,task,sys_user^ENDSUBQUERY",
        "JOINincident.caller_id=sys_user.sys_id",
        # An empty term, ``^^``, and a raw that starts or ends with ``^``.
        "priority=1^^ORactive=false",
        "^active=false",
        "priority=1^",
        # An encoded separator a second decode would turn into ``^NQ``.
        "short_descriptionLIKEa%5ENQactive=false",
        # Fields that are not lower-case names, or keyword pseudo-fields.
        "Priority=1",
        "123TEXTQUERY321=x",
        "IR_AND_OR_QUERY=x",
        "ir_or_query=x",
        "gotonumber>=INC0",
        ".priority=1",
        "priority.=1",
        "assigned_to..name=x",
        # A dot-walk filters on another table's records.
        "assigned_to.user_password=x",
        "caller_id.phoneSTARTSWITH555",
        # Case forms that a case-blind compare reads as ``javascript:``.
        "short_descriptionLIKEjavaſcript:gs.getUserID()",
        "short_descriptionLIKEjavascrıpt:gs.getUserID()",
        "=1",
        # Operators this endpoint does not offer, and scripts.
        "assigned_toDYNAMIC90d1921e5f510100a9ad2572f2b477fe",
        "assigned_to=javascript:gs.getUserID()",
        "sys_created_on>=JavaScript:gs.daysAgoStart(7)",
        "priority",
        "priority~1",
        "assigned_toISEMPTYx",
        "priority=1\nactive=false",
        "^".join(["priority=1"] * 21),
    ],
)
async def test_a_raw_query_that_could_widen_or_reorder_is_refused(stored, table, raw) -> None:
    status, payload = await _search(raw=raw)
    assert status == 400 and payload["code"] == "invalid_request"
    assert table.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"labels": ["bug"]},
        {"assignee": "a^NQactive=false"},
        {"author": "javascript:gs.getUserID()"},
        {"assignee": "100%5E"},
        {"text": "javascript:gs.sleep(1)"},
        {"text": "JAVAſCRIPT:gs.sleep(1)"},
        {"assignee": "javaſcript:gs.getUserID()"},
        {"assignee": "x" * 256},
    ],
)
async def test_other_invalid_fields_are_refused_before_any_call(stored, table, body) -> None:
    status, payload = await _search(**body)
    assert status == 400 and payload["code"] == "invalid_request"
    assert table.urls == []


@pytest.mark.asyncio
async def test_summary_shape(stored, table) -> None:
    table.answers = [(200, "1", {"result": [_row(42)]})]
    status, payload = await _search()
    assert status == 200
    assert payload == {
        "provider": "servicenow",
        "scope": "incident",
        "nextCursor": None,
        "total": 1,
        "issues": [
            {
                "provider": "servicenow",
                "url": f"https://{_HOST}/incident.do?sys_id={42:032x}",
                "key": "INC0000042",
                "number": 42,
                "title": "Record 42",
                "state": "open",
                "statusLabel": "In Progress",
                "labels": [],
                "assignees": [{"login": "Alice Admin", "name": "Alice Admin", "avatarUrl": ""}],
                "author": {"login": "Bob Caller", "name": "Bob Caller", "avatarUrl": ""},
                "priority": "1 - Critical",
                "commentCount": 0,
                "createdAt": "2026-11-01T10:00:00Z",
                "updatedAt": "2026-11-09T11:30:15Z",
                "excerpt": "Printer on 3rd floor is on fire",
                "project": "incident",
                "linkedChanges": [],
            }
        ],
    }


@pytest.mark.asyncio
async def test_rows_without_a_valid_id_or_number_are_dropped(stored, table) -> None:
    rows = [
        _row(1),
        _row(2, sys_id=_cell("../../sys_user")),
        _row(3, sys_id=_cell("A" * 32)),
        _row(4, number=_cell("INC 4")),
        _row(5, number=_cell("")),
        "not a row",
    ]
    table.answers = [{"result": rows}]
    _status, payload = await _search()
    assert [issue["key"] for issue in payload["issues"]] == ["INC0000001"]


@pytest.mark.asyncio
async def test_unset_priority_assignee_and_times_read_as_empty(stored, table) -> None:
    row = _row(
        7,
        priority=_cell("", ""),
        assigned_to=_cell("", ""),
        sys_updated_on=_cell("yesterday"),
        state=_cell("7", ""),
        active=_cell("false"),
    )
    table.answers = [{"result": [row]}]
    _status, payload = await _search(state="all")
    issue = payload["issues"][0]
    assert (issue["priority"], issue["assignees"], issue["updatedAt"]) == (None, [], "")
    assert (issue["state"], issue["statusLabel"]) == ("closed", "Closed")


# ── Paging ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_total_header_drives_the_next_cursor(stored, table) -> None:
    table.answers = [(200, "25", {"result": [_row(i) for i in range(1, 11)]})]
    _status, first = await _search(limit=10, cursor="2")
    assert (first["nextCursor"], first["total"]) == ("3", 25)
    table.answers = [(200, "25", {"result": [_row(i) for i in range(1, 6)]})]
    _status, last = await _search(limit=10, cursor="3")
    assert (last["nextCursor"], last["total"]) == (None, 25)


@pytest.mark.asyncio
async def test_without_a_total_a_full_page_has_a_next(stored, table) -> None:
    table.answers = [(200, "", {"result": [_row(i) for i in range(1, 6)]})]
    _status, full = await _search(limit=5)
    assert (full["nextCursor"], full["total"]) == ("2", None)
    table.answers = [(200, "x", {"result": [_row(1)]})]
    _status, short = await _search(limit=5, cursor="2")
    assert (short["nextCursor"], short["total"]) == (None, None)


# ── Redaction ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_secrets_in_a_search_come_back_redacted(stored, table) -> None:
    leaky = _row(
        9,
        short_description=_cell(f"leaked {_SECRET} here"),
        description=_cell(f"use {_SECRET} to log in"),
        state=_cell("2", f"Blocked {_SECRET}"),
    )
    table.answers = [{"result": [leaky]}]
    status, payload = await _search()
    assert status == 200
    assert _SECRET not in json.dumps(payload)
    assert payload["issues"][0]["key"] == "INC0000009"
