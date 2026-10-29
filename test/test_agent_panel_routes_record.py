"""The record door: ``POST /api/agent-panel/dashboard/record``.

A script cron bound to a crewmate appends one custom record to that crewmate's own DM
session log; the ``custom`` fold keeps the latest record per type. These tests pin the
door's refusals (who may write, what a record may be, how fast), that the provenance is
the GATEWAY's and never the body's, and that a landed record folds where a page reads it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from agent_panel_routes_helpers import (  # noqa: F401 -- fixtures are used by name
    CREW,
    SLUG,
    _client,
    _crew_log_on,
    _crew_slot,
    _mounted,
    _unit_for,
)
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from off_loop_helpers import off_loop

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.dashboard.handlers import agent_panel_record, member_dashboard
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

pytestmark = pytest.mark.asyncio

RECORD_PATH = "/api/agent-panel/dashboard/record"
JOB_ID = "stock-poll"
CRON_KEY = f"cron:{JOB_ID}"
MEMBER_ID = "fleet-crew"


def _execution(member_id: str | None = MEMBER_ID) -> ExecutionContext:
    """A cron's captured execution: bound to *member_id*, or to no crewmate."""
    if member_id is None:
        return ExecutionContext(
            member_id=None,
            store=MemoryStoreRef("default"),
            selection_kind="template",
            template_id="",
        )
    return ExecutionContext(
        member_id=member_id,
        store=MemoryStoreRef(f"member-{member_id}", member_id),
        selection_kind="member",
        template_id="kirocrew",
    )


def _scope_is(monkeypatch: pytest.MonkeyPatch, scope: _shared.MemberScope) -> None:
    async def _resolved(_request):
        return scope

    monkeypatch.setattr(routes, "member_request_scope", _resolved)


@pytest.fixture(name="bound_cron")
def bound_cron_fixture(monkeypatch):
    """A verified, member-bound cron caller whose crewmate has a live DM unit.

    The scope is the transport's answer and the member lookup the config's; both are
    stubbed at the seams the route reads them through, so what is under test is the
    route's own stamping and refusals rather than the scheduler or the config loader.
    """
    _scope_is(monkeypatch, _shared.MemberScope(CRON_KEY, True, "", _execution()))
    monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: True)
    from kiro_crew import execution_context

    monkeypatch.setattr(
        execution_context,
        "member_config_for_id",
        lambda _cfg, member_id: (CREW, SimpleNamespace(member_id=member_id)),
    )
    unit = _unit_for(CREW)
    monkeypatch.setattr(member_dashboard, "_newest_unit", lambda _slot: unit)
    # A fresh quota window per test: the window is process memory.
    agent_panel_record._recent_records.clear()
    return unit


def _headers() -> dict[str, str]:
    return {"X-Session-Key": CRON_KEY}


async def _post(c: TestClient, body: Any, headers: dict[str, str] | None = None):
    return await c.post(RECORD_PATH, json=body, headers=headers or _headers())


def _custom_fold() -> dict[str, Any]:
    crew_log_projection.forget_slot_folds()
    value = off_loop(crew_log_projection.read_slot_projection, _crew_slot(CREW), "custom").value
    assert isinstance(value, dict)
    return value


# ----------------------------------------------------------------- who may write


async def test_the_door_needs_the_internal_secret(bound_cron):
    async with _client(internal=False) as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "internal_secret_required"


async def test_the_door_closes_with_the_crew_panel_switch(bound_cron, monkeypatch):
    """A record is a dashboard value, so the switch that withdraws the dashboard
    surface withdraws this door with it."""
    monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: False)
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "crew_panel_disabled"


async def test_an_app_token_caller_is_turned_away_like_every_member_route(bound_cron):
    """The 404 the member routes give an app identity, BEFORE any identity is read:
    an installed app's own grant is a separate decision and is not made here."""
    app = _mounted()

    @web.middleware
    async def _as_app(request, handler):
        request["app"] = "some-app"
        return await handler(request)

    app.middlewares.insert(0, _as_app)
    async with TestClient(TestServer(app)) as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 404, await resp.text()


async def test_a_caller_that_is_not_an_attested_cron_is_refused(bound_cron, monkeypatch):
    _scope_is(monkeypatch, _shared.MemberScope("dashboard:chat-1", True, "", None))
    async with _client() as c:
        resp = await _post(
            c, {"type": "stock_check", "data": {"price": 1}}, {"X-Session-Key": "dashboard:chat-1"}
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "caller_not_cron"


async def test_an_unverified_cron_key_is_refused(bound_cron, monkeypatch):
    """A header that merely asserts ``cron:<id>`` resolves to nothing: the scope says
    the transport could not vouch for it."""
    _scope_is(monkeypatch, _shared.MemberScope(CRON_KEY, False, None, None))
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "caller_not_cron"


async def test_a_cron_bound_to_no_crewmate_is_refused(bound_cron, monkeypatch):
    _scope_is(monkeypatch, _shared.MemberScope(CRON_KEY, True, "", _execution(None)))
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 403, await resp.text()
        body = await resp.json()
        assert body["code"] == "cron_not_member_bound"
        assert "bind the job" in body["error"]


async def test_a_member_the_config_no_longer_holds_is_a_409(bound_cron, monkeypatch):
    from kiro_crew import execution_context

    def _gone(_cfg, _member_id):
        raise RuntimeError("member identity is missing or ambiguous")

    monkeypatch.setattr(execution_context, "member_config_for_id", _gone)
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["code"] == "member_identity_unavailable"


# ------------------------------------------------------------ what a record may be


async def test_a_body_that_is_not_json_is_refused(bound_cron):
    async with _client() as c:
        resp = await c.post(RECORD_PATH, data=b"not json", headers=_headers())
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "invalid_json"


@pytest.mark.parametrize(
    "body, code",
    [
        (["a", "list"], "invalid_body"),
        ({"data": {"price": 1}}, "bad_record_type"),
        ({"type": "", "data": {"price": 1}}, "bad_record_type"),
        ({"type": "stock/check", "data": {"price": 1}}, "bad_record_type"),
        # A dot or a hyphen is refused because the name is a PATH SEGMENT a page binds:
        # the feed's path walk splits on dots and the manifest's path pattern has no
        # hyphen, so either would be a record that lands and a cell that stays empty.
        ({"type": "stock.check", "data": {"price": 1}}, "bad_record_type"),
        ({"type": "stock-check", "data": {"price": 1}}, "bad_record_type"),
        ({"type": "app:stock", "data": {"price": 1}}, "bad_record_type"),
        ({"type": "stock check", "data": {"price": 1}}, "bad_record_type"),
        ({"type": ".hidden", "data": {"price": 1}}, "bad_record_type"),
        ({"type": "x" * 65, "data": {"price": 1}}, "bad_record_type"),
        ({"type": "stock_check"}, "invalid_record"),
        ({"type": "stock_check", "data": [1, 2]}, "invalid_record"),
        ({"type": "stock_check", "data": "price=1"}, "invalid_record"),
    ],
)
async def test_a_malformed_record_is_refused_before_anything_is_appended(
    bound_cron, monkeypatch, body, code
):
    appended: list[Any] = []
    monkeypatch.setattr(
        crew_log_emit, "on_dashboard_custom", lambda *a, **k: appended.append(a) or True
    )
    async with _client() as c:
        resp = await _post(c, body)
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == code
    assert appended == []


async def test_a_record_over_the_byte_ceiling_is_refused(bound_cron):
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"blob": "n" * 9000}})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "record_too_large"


async def test_a_record_nested_too_deep_is_refused(bound_cron):
    deep: dict[str, Any] = {"v": 1}
    for _ in range(9):
        deep = {"d": deep}
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": deep})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "record_too_deep"


async def test_a_record_nested_a_thousand_deep_is_a_400_not_a_crash(bound_cron):
    """Parser-valid JSON can nest far past the ceiling. The depth walk stops at the
    ceiling, so the route answers the documented refusal rather than raising
    ``RecursionError`` out as a 500 with no code."""
    deep: dict[str, Any] = {"v": 1}
    for _ in range(1000):
        deep = {"d": deep}
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": deep})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "record_too_deep"


async def test_a_lone_surrogate_in_a_record_is_a_refusal_not_a_crash(bound_cron):
    """A JSON-escaped lone surrogate parses and serializes but cannot be encoded as
    UTF-8; the size check answers "does not fit" rather than raising."""
    body = '{"type": "stock_check", "data": {"s": "\\ud83d"}}'
    async with _client() as c:
        resp = await c.post(
            RECORD_PATH,
            data=body.encode("utf-8"),
            headers={**_headers(), "Content-Type": "application/json"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "record_too_large"


async def test_a_record_at_the_depth_ceiling_is_taken(bound_cron):
    deep: dict[str, Any] = {"v": 1}
    for _ in range(7):
        deep = {"d": deep}
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": deep})
        assert resp.status == 200, await resp.text()


async def test_the_quota_refuses_the_record_past_the_window(bound_cron, monkeypatch):
    """Counted at the door, so a wedged script is bounded whatever it sends; a refused
    record is answered 429 and never queued."""
    appended: list[Any] = []
    monkeypatch.setattr(
        crew_log_emit, "on_dashboard_custom", lambda *a, **k: appended.append(a) or True
    )
    async with _client() as c:
        for _ in range(agent_panel_record.RECORD_QUOTA_PER_MINUTE):
            resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
            assert resp.status == 200, await resp.text()
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 429, await resp.text()
        assert (await resp.json())["code"] == "quota_exceeded"
    assert len(appended) == agent_panel_record.RECORD_QUOTA_PER_MINUTE


async def test_a_refused_record_costs_a_quota_slot_too(bound_cron, monkeypatch):
    """The quota is counted BEFORE the body is read, so a script retrying a bad payload
    in a loop is throttled to 429 rather than parsed and scrubbed once per retry."""
    appended: list[Any] = []
    monkeypatch.setattr(
        crew_log_emit, "on_dashboard_custom", lambda *a, **k: appended.append(a) or True
    )
    async with _client() as c:
        for _ in range(agent_panel_record.RECORD_QUOTA_PER_MINUTE):
            resp = await _post(c, {"type": "bad.name", "data": {"price": 1}})
            assert resp.status == 400, await resp.text()
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 429, await resp.text()
        assert (await resp.json())["code"] == "quota_exceeded"
    assert appended == []


async def test_a_type_that_looks_like_a_credential_is_refused_not_stored(bound_cron):
    """The grammar admits a token-shaped name; a type is a key the door cannot scrub, so
    one the scrubber would change is refused rather than persisted or echoed."""
    pat = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert agent_panel_record.custom_type_name_ok(pat)
    async with _client() as c:
        resp = await _post(c, {"type": pat, "data": {"price": 1}})
        assert resp.status == 400, await resp.text()
        body = await resp.json()
        assert body["code"] == "bad_record_type"
        assert pat not in json.dumps(body)
    assert "latest" in _custom_fold() and pat not in _custom_fold()["latest"]


def test_the_quota_map_forgets_retired_writers_and_is_bounded():
    """Every call trims every writer's window and drops the ones that emptied, so the map
    holds the writers inside the last minute and not every job the gateway ever ran; a
    new writer past the bound is refused while live writers keep their counts."""
    agent_panel_record._recent_records.clear()
    for index in range(agent_panel_record.RECORD_QUOTA_WRITERS):
        assert agent_panel_record._over_quota(f"cron:old-{index}", now=100.0) is False
    assert len(agent_panel_record._recent_records) == agent_panel_record.RECORD_QUOTA_WRITERS
    # At capacity, a NEW writer is refused and no live writer is evicted.
    assert agent_panel_record._over_quota("cron:new", now=100.5) is True
    assert "cron:new" not in agent_panel_record._recent_records
    assert agent_panel_record._over_quota("cron:old-0", now=100.5) is False
    # A minute on, the old writers' windows are empty: one call sweeps them all out.
    assert agent_panel_record._over_quota("cron:new", now=170.0) is False
    assert set(agent_panel_record._recent_records) == {"cron:new"}


def test_colliding_keys_are_suffixed_in_linear_time():
    """N keys that scrub to one name cost N suffix probes, not N squared: the suffix
    continues from the last one given out instead of searching up from 2 each time.
    Counted, not timed: the probes are membership tests on the output map."""

    class _Counting(dict):
        probes = 0

        def __contains__(self, item: object) -> bool:
            type(self).probes += 1
            return super().__contains__(item)

    out: _Counting = _Counting()
    suffixes: dict[Any, int] = {}
    n = 2000
    for _ in range(n):
        out[agent_panel_record._unique_key(out, "[REDACTED: credential]", suffixes)] = 1
    assert len(out) == n
    # Per key: one probe for the base, and for every collision one probe for the
    # suffix it is given -- so at most two per key, linear in N.
    assert _Counting.probes <= 2 * n, _Counting.probes
    secret = "AKIA" + "1234567890123456"
    scrubbed = agent_panel_record._redacted_record({f"{secret}{i:05d}": i for i in range(50)})
    assert len(scrubbed) == 50 and not any(secret in key for key in scrubbed)


async def test_a_record_over_the_ceiling_is_refused_before_it_is_scrubbed(bound_cron, monkeypatch):
    """The raw record is measured before the scrub walks it, so an oversize payload never
    reaches the regex pass."""
    from kiro_crew.dashboard.handlers import agent_panel_record as door

    scrubbed: list[Any] = []
    monkeypatch.setattr(
        door, "_redacted_record", lambda value, key="": scrubbed.append(value) or value
    )
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"blob": "n" * 9000}})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "record_too_large"
    assert scrubbed == []


def test_the_quota_window_slides():
    """A slot taken a minute ago is free again; one taken just now is not."""
    agent_panel_record._recent_records.clear()
    writer = "cron:window"
    for _ in range(agent_panel_record.RECORD_QUOTA_PER_MINUTE):
        assert agent_panel_record._over_quota(writer, now=1000.0) is False
    assert agent_panel_record._over_quota(writer, now=1000.5) is True
    assert agent_panel_record._over_quota(writer, now=1060.0) is False
    assert agent_panel_record._over_quota("cron:other", now=1000.5) is False


# ------------------------------------------------- the append, and what it stamps


async def test_a_record_lands_stamped_by_the_gateway_and_folds_for_the_page(bound_cron):
    """The round trip the feature is: the script sends type and data, the gateway stamps
    writer and member, and the ``custom`` fold answers ``latest.<type>`` with all of it."""
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        resp = await _post(c, {"type": "stock_check", "data": {"symbol": "AMZN", "price": 187.4}})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body == {
            "ok": True,
            "recorded": {"type": "stock_check", "writer": CRON_KEY, "member": SLUG},
        }
        sent = app["state"].broadcasts
        assert ("dashboard_value_written", {"slug": SLUG}) in sent, f"nothing pushed: {sent}"
    folded = _custom_fold()
    row = folded["latest"]["stock_check"]
    assert row["data"] == {"symbol": "AMZN", "price": 187.4}
    assert row["writer"] == CRON_KEY
    assert row["member"] == SLUG
    assert row["crew_key"] == agent_panel.crew_key(CREW)
    assert row["at"].endswith("+00:00")
    assert row["seq"] >= 1
    assert folded["wrote"] == 1


async def test_the_body_cannot_set_who_wrote_it_or_for_whom(bound_cron):
    """``writer``, ``member``, ``crew_key`` and ``src`` in the body are IGNORED: the
    entry carries the gateway's reading of the caller and nothing the script said."""
    async with _client() as c:
        resp = await _post(
            c,
            {
                "type": "stock_check",
                "data": {"price": 1},
                "writer": "gateway",
                "member": "someone-else",
                "crew_key": "f" * 64,
                "src": "patrol",
            },
        )
        assert resp.status == 200, await resp.text()
    row = _custom_fold()["latest"]["stock_check"]
    assert row["writer"] == CRON_KEY
    assert row["member"] == SLUG
    assert row["crew_key"] == agent_panel.crew_key(CREW)


async def test_the_door_scrubs_the_record_even_when_the_client_did_not(bound_cron):
    """``ScriptContext`` redacts before it posts, but it is one caller and not the gate:
    a script posting through ``ctx._post`` sends whatever it holds. The DOOR scrubs
    what lands -- a credential on its own, and a bearer token that only matches beside
    its ``authorization`` key -- and the fold answers the scrubbed form."""
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG

    secret = "AKIA" + "1234567890123456"
    bearer = "Bearer " + "abcdefghijklmnopqrstuvwxyz0123"
    async with _client() as c:
        resp = await _post(
            c,
            {
                "type": "stock_check",
                "data": {
                    "note": f"key {secret} seen",
                    "headers": {"authorization": bearer, "accept": "text/plain"},
                    "rows": [{"authorization": bearer}, 7],
                    "n": 1,
                },
            },
        )
        assert resp.status == 200, await resp.text()
    row = _custom_fold()["latest"]["stock_check"]
    blob = json.dumps(row["data"])
    assert secret not in blob and "abcdefghijklmnopqrstuvwxyz0123" not in blob
    assert row["data"]["headers"]["authorization"] == REDACTED_CREDENTIAL_TAG
    assert row["data"]["headers"]["accept"] == "text/plain"
    assert row["data"]["rows"][0]["authorization"] == REDACTED_CREDENTIAL_TAG
    assert row["data"]["rows"][1] == 7 and row["data"]["n"] == 1


async def test_a_value_with_two_tokens_loses_both_at_the_door(bound_cron):
    """The keyed judgement runs on the CLEANED value: a header value carrying an access
    key the bare scrub catches AND a bearer only its key reveals keeps neither."""
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG

    secret = "AKIA" + "1234567890123456"
    bearer = "Bearer " + "abcdefghijklmnopqrstuvwxyz0123"
    async with _client() as c:
        resp = await _post(
            c, {"type": "stock_check", "data": {"authorization": f"{bearer} {secret}"}}
        )
        assert resp.status == 200, await resp.text()
    row = _custom_fold()["latest"]["stock_check"]
    assert row["data"]["authorization"] == REDACTED_CREDENTIAL_TAG
    assert "abcdefghijklmnopqrstuvwxyz0123" not in json.dumps(row["data"])


async def test_a_record_that_only_fits_before_scrubbing_is_judged_on_what_lands(bound_cron):
    """The size is measured on the scrubbed record, the bytes the fold re-measures."""
    from kiro_crew.dashboard.handlers import agent_panel_record as door

    seen: list[Any] = []

    def _grow(value: Any, key: str = "") -> Any:
        seen.append(value)
        return {"blob": "n" * 9000}

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(door, "_redacted_record", _grow)
    try:
        async with _client() as c:
            resp = await _post(c, {"type": "stock_check", "data": {"small": 1}})
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "record_too_large"
    finally:
        mp.undo()
    assert seen == [{"small": 1}]


async def test_the_latest_record_per_type_wins_and_other_types_stay(bound_cron):
    async with _client() as c:
        for price, kind in ((1, "stock_check"), (2, "stock_check"), (9, "weather")):
            resp = await _post(c, {"type": kind, "data": {"n": price}})
            assert resp.status == 200, await resp.text()
    latest = _custom_fold()["latest"]
    assert latest["stock_check"]["data"] == {"n": 2}
    assert latest["weather"]["data"] == {"n": 9}


def test_every_accepted_record_type_is_a_bindable_fold_path_segment():
    """A record type the door accepts must be a name a page can bind: one segment of a
    manifest fold path (``manifest._PATH``), so ``custom.latest.<type>.data`` is a path
    the manifest accepts and the feed's dot-split walk reads back under the stored key."""
    from kiro_crew.crew_log import projection_dashboard as fold
    from kiro_crew.dashboard_templates import manifest

    for name in ("stock_check", "Weather", "q1_2026", "a"):
        assert fold.custom_type_name_ok(name)
        assert manifest._PATH.match(f"custom.latest.{name}.data.price")
        assert "custom.latest.{}.data".format(name).split(".")[2] == name
    for name in ("stock.check", "stock-check", "a b", "a/b", "app:x", "", "x" * 65, "é"):
        assert not fold.custom_type_name_ok(name), name


async def test_the_fold_is_in_the_type_catalog_a_page_binds_from(bound_cron):
    """``dashboard_types`` lists ``custom`` with ``latest`` as an open-keyed map, and the
    seq it has been folded through advances once a record lands."""
    from kiro_crew import dashboard_types

    before = {row.name: row for row in dashboard_types.catalog()}
    assert before["custom"].keyed_by == "slot"
    latest = before["custom"].shape["properties"]["latest"]
    assert latest["opaque"] is True
    assert "record type" in latest["keyed_by"]
    assert "data" in latest["row_keys"] and "writer" in latest["row_keys"]
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 200, await resp.text()
    reached = off_loop(routes._folded_through, _crew_slot(CREW), "")
    assert reached["custom"] is not None and reached["custom"] >= 1


async def test_a_crewmate_whose_thread_never_ran_has_no_log_to_append_to(bound_cron, monkeypatch):
    monkeypatch.setattr(member_dashboard, "_newest_unit", lambda _slot: "")
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 409, await resp.text()
        body = await resp.json()
        assert body["code"] == "crew_log_unavailable"
        assert "open its chat" in body["error"]


async def test_an_entry_over_the_logs_line_ceiling_is_not_reported_as_stored(
    bound_cron, monkeypatch
):
    monkeypatch.setattr(crew_log_emit, "dashboard_entry_fits", lambda *_a, **_k: False)
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "record_too_large"


async def test_an_append_that_does_not_land_is_reported_as_failed(bound_cron, monkeypatch):
    monkeypatch.setattr(crew_log_emit, "on_dashboard_custom", lambda *_a, **_k: False)
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "append_failed"
        assert not app["state"].broadcasts, "a record that did not land must push nothing"


async def test_a_record_with_the_crew_log_off_is_refused_not_shrugged(bound_cron, monkeypatch):
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
    async with _client() as c:
        resp = await _post(c, {"type": "stock_check", "data": {"price": 1}})
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "crew_log_off"


# ----------------------------------------------------------- the fold's own guards


def _entry(data: dict[str, Any], seq: int = 1):
    from kiro_crew.crew_log.schema import Entry

    return Entry(
        type="dashboard/custom_value", seq=seq, time=1_760_000_000_000, src="gateway", data=data
    )


@pytest.mark.parametrize(
    "data",
    [
        {"data": {"v": 1}, "writer": "cron:x"},
        {"type": "bad/name", "data": {"v": 1}, "writer": "cron:x"},
        {"type": "dotted.name", "data": {"v": 1}, "writer": "cron:x"},
        {"type": "ok", "data": {"v": 1}},
        {"type": "ok", "data": "not an object", "writer": "cron:x"},
        {"type": "ok", "data": {"blob": "n" * 9000}, "writer": "cron:x"},
    ],
)
def test_the_fold_skips_a_planted_or_damaged_line(data):
    """The caps are RE-APPLIED on read: a line off disk that names no type or no writer,
    or carries a record the door would refuse, is skipped rather than keyed."""
    from kiro_crew.crew_log import projection_dashboard as fold

    state = fold._custom_start()
    fold._custom_step(state, _entry(data))
    assert fold._custom_render(state) == {"latest": {}, "types_omitted": 0, "wrote": 0}


def test_the_fold_skips_a_planted_line_nested_a_thousand_deep_without_raising():
    """The same bounded walk on read: a line that deep is skipped, never a crash out of
    the fold step."""
    from kiro_crew.crew_log import projection_dashboard as fold

    deep: dict[str, Any] = {"v": 1}
    for _ in range(1000):
        deep = {"d": deep}
    state = fold._custom_start()
    fold._custom_step(state, _entry({"type": "ok", "data": deep, "writer": "cron:x"}))
    assert fold._custom_render(state)["latest"] == {}
    assert fold.custom_record_depth(deep) > fold.CUSTOM_RECORD_DEPTH
    assert fold.custom_record_fits({"s": "\ud83d"}) is False


def test_the_fold_bounds_the_types_it_keeps_and_counts_what_it_evicts():
    from kiro_crew.crew_log import projection_dashboard as fold
    from kiro_crew.crew_log.entry_types import CUSTOM_TYPE_LIMIT

    state = fold._custom_start()
    for index in range(CUSTOM_TYPE_LIMIT + 3):
        fold._custom_step(
            state,
            _entry({"type": f"t{index}", "data": {"v": index}, "writer": "cron:x"}, index + 1),
        )
    rendered = fold._custom_render(state)
    assert len(rendered["latest"]) == CUSTOM_TYPE_LIMIT
    assert rendered["types_omitted"] == 3
    assert "t0" not in rendered["latest"] and f"t{CUSTOM_TYPE_LIMIT + 2}" in rendered["latest"]
    assert all("order" not in row for row in rendered["latest"].values())
