"""Dashboard edit mode (feature preview ``dashboard_edit_mode``): the gateway's half.

Off, every page, write, fold row and turn is what it was without the preview. On, a
page's components carry ids, the frame gets the edit bridge, a picked path resolves to a
lineage built from the gateway's own data, the person can correct a value the Bot
writes, and a script's record carries what it read beside the gateway's own stamp.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import dashboard_edit_mode, dashboard_frame
from kiro_crew.artifact_store import dashboard_package as pkg
from kiro_crew.artifact_store.model import ArtifactValidationError
from kiro_crew.crew_log import projection_dashboard as fold
from kiro_crew.crew_log.schema import Entry
from kiro_crew.dashboard.handlers import agent_panel_record, member_dashboard_lineage
from kiro_crew.dashboard_templates.manifest import FieldSpec

MODEL = {
    "types": {
        "accepted": {"type": "number", "source": {"fold": "workstreams", "path": "accepted"}},
        "verdict": {"type": "text", "source": {"agentic": True}},
    }
}


def _package(view: dict[str, Any]) -> str:
    return json.dumps(
        {"kind": "dashboard", "bound_to": "crewmate:atlas", "model": MODEL, "view": view}
    )


@pytest.fixture
def preview_on(monkeypatch):
    monkeypatch.setattr(dashboard_edit_mode, "edit_mode_on", lambda cfg=None: True)


@pytest.fixture
def preview_off(monkeypatch):
    monkeypatch.setattr(dashboard_edit_mode, "edit_mode_on", lambda cfg=None: False)


# --------------------------------------------------------------------- the switch


def test_the_switch_reads_the_named_preview_and_fails_closed():
    on = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={"dashboard_edit_mode": True}))
    other = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={"crew_mode": True}))
    assert dashboard_edit_mode.edit_mode_on(on) is True
    assert dashboard_edit_mode.edit_mode_on(other) is False
    assert dashboard_edit_mode.edit_mode_on(SimpleNamespace()) is False


# --------------------------------------------------------------- component ids


BLOCKS = {"blocks": [{"id": "tasks", "title": "Tasks", "fields": ["accepted"]}]}


def test_off_a_block_id_is_ignored_and_the_page_is_unchanged(preview_off):
    page = pkg.parse_package(_package(BLOCKS))["view"]["html"]
    assert "data-dashboard-block" not in page
    assert page.startswith("<section><h2>")


def test_on_a_block_id_becomes_the_component_id(preview_on):
    page = pkg.parse_package(_package(BLOCKS))["view"]["html"]
    assert page.startswith('<section data-dashboard-block="tasks"><h2>')


def test_on_a_block_id_that_is_not_a_slug_is_refused(preview_on):
    bad = {"blocks": [{"id": 'x" onclick="1', "fields": ["accepted"]}]}
    with pytest.raises(ArtifactValidationError, match=r"view\.blocks\[0\]\.id"):
        pkg.parse_package(_package(bad))


DUPLICATE = {
    "html": '<div data-dashboard-block="a"><b data-dashboard-field="accepted"></b></div>'
    "<div data-dashboard-block='a'></div>"
}


def test_on_the_write_gate_refuses_a_component_id_used_twice(preview_on):
    with pytest.raises(ArtifactValidationError, match="names component 'a' twice"):
        pkg.canonical_package_content(_package(DUPLICATE))


def test_a_stored_page_with_a_repeated_id_still_reads(preview_on):
    """Only the WRITE checks ids: a page already stored keeps drawing."""
    assert "data-dashboard-block" in pkg.parse_package(_package(DUPLICATE))["view"]["html"]


def test_off_the_write_gate_takes_any_component_ids(preview_off):
    assert pkg.canonical_package_content(_package(DUPLICATE))


def test_the_component_cap():
    page = "".join(f'<i data-dashboard-block="b{n}"></i>' for n in range(65))
    assert "at most 64" in dashboard_edit_mode.block_id_refusal(page)
    assert dashboard_edit_mode.block_id_refusal('<i data-dashboard-block="Bad"></i>') != ""
    assert dashboard_edit_mode.block_id_refusal('<i data-dashboard-block="ok-1"></i>') == ""


# ------------------------------------------------------------------ the bridge


def test_off_the_document_is_byte_for_byte_the_one_without_the_bridge(preview_off):
    read = dashboard_frame.read_payload({"accepted": 7})
    page = '<b data-dashboard-field="accepted"></b>'
    assert dashboard_frame.compose(page, read) == dashboard_frame.compose(page, read, edit=False)
    assert "__kirocrewPick" not in dashboard_frame.compose(page, read)
    assert "kirocrew-dashboard:point" not in dashboard_frame.compose_body(page, read)


def test_on_the_bridge_runs_before_the_bootstrap_and_attaches_pick(preview_on):
    document = dashboard_frame.compose("<p></p>", dashboard_frame.read_payload({}))
    bridge = document.index("kirocrew-dashboard:point")
    bootstrap = document.index("kirocrew-dashboard:refetch")
    page = document.index("<p></p>")
    assert bridge < bootstrap < page
    assert "value.pick = window.__kirocrewPick;" in document


def test_the_pick_anchor_is_in_the_bootstrap_exactly_once():
    assert dashboard_frame._BOOTSTRAP_JS.count(dashboard_frame._PICK_ANCHOR) == 1


# ---------------------------------------------------------------- field paths


@pytest.mark.parametrize(
    "path,parsed",
    [
        ("accepted", ("accepted", [])),
        ("items[2].accepted", ("items", [2, "accepted"])),
        ("items[*]", ("items", ["*"])),
        ("items[*].accepted", None),
        ("Items", None),
        ("items..x", None),
        ("x" * 201, None),
        (7, None),
    ],
)
def test_a_pick_names_a_field_path_or_nothing(path, parsed):
    assert dashboard_edit_mode.parse_field_path(path) == parsed


def test_walk_follows_keys_indexes_and_counts():
    value = {"rows": [{"n": 3}, {"n": 4}]}
    assert dashboard_edit_mode.walk(value, ["rows", 1, "n"]) == (True, 4)
    assert dashboard_edit_mode.walk(value, ["rows", "*"]) == (True, 2)
    assert dashboard_edit_mode.walk(value, ["rows", 5]) == (False, None)


# --------------------------------------------------------------------- lineage


def _spec(name, *, fold_name=None, path=None, agentic=False, type_="number"):
    return FieldSpec(name, type_, fold_name, path, agentic)


def test_a_fold_value_names_its_fold_and_the_gap_behind_it():
    out = member_dashboard_lineage.build_lineage(
        _spec("items", fold_name="workstreams", path="items", type_="array"),
        [0, "accepted"],
        path="items[0].accepted",
        field_value=[{"accepted": 7}],
        field_found=True,
    )
    assert out["value"] == 7 and out["kind"] == "fold"
    kinds = [hop["kind"] for hop in out["hops"]]
    assert kinds == ["field", "fold", "crew_log"]
    assert out["hops"][1]["path"] == "items[0].accepted"
    assert out["hops"][2]["needs"] == member_dashboard_lineage.NEEDS_FOLD_SEQ
    assert out["edit"] == {"kind": "none", "reason": "fold"}


def test_a_person_correction_is_the_top_write_and_keeps_the_bots():
    row = {
        "value": "green",
        "at": "2026-11-08T10:00:00+00:00",
        "actor": "person",
        "reason": "CI is green now",
        "seq": 41,
        "prior": {"value": "red", "at": "2026-11-08T09:00:00+00:00"},
    }
    out = member_dashboard_lineage.build_lineage(
        _spec("verdict", agentic=True, type_="string"),
        [],
        path="verdict",
        field_value="green",
        field_found=True,
        agentic_row=row,
        stale_after="2026-11-09T10:00:00+00:00",
    )
    write, earlier = out["hops"][1], out["hops"][2]
    assert (
        write["actor"] == "person" and write["reason"] == "CI is green now" and write["seq"] == 41
    )
    assert earlier["kind"] == "earlier_write" and earlier["actor"] == "bot"
    assert out["hops"][3] == {
        "kind": "freshness",
        "max_age_s": 24 * 3600,
        "stale_after": "2026-11-09T10:00:00+00:00",
        "by": "gateway",
    }
    assert out["edit"]["kind"] == "correct"


@pytest.mark.parametrize(
    ("declared", "max_age_s", "stale_after"),
    [(None, 24 * 3600, "S"), (3600, 3600, "S"), (0, None, "")],
)
def test_an_agentic_values_freshness_is_its_fields_stale_limit(declared, max_age_s, stale_after):
    spec = FieldSpec("verdict", "string", None, None, True, stale_after_s=declared)
    out = member_dashboard_lineage.build_lineage(
        spec,
        [],
        path="verdict",
        field_value="green",
        field_found=True,
        agentic_row={"value": "green", "at": "2026-11-08T10:00:00+00:00"},
        stale_after="S",
    )
    freshness = next(hop for hop in out["hops"] if hop["kind"] == "freshness")
    assert (freshness["max_age_s"], freshness["stale_after"]) == (max_age_s, stale_after)
    assert "needs" not in freshness


def test_the_route_dates_a_write_by_the_same_limit_the_page_marks(monkeypatch):
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md
    from kiro_crew.dashboard_templates.manifest import parse_manifest

    manifest = parse_manifest(
        {
            "id": "lineage-probe",
            "version": 1,
            "title": "Lineage probe",
            "description": "a page for this case",
            "source": "user",
            "fields": {
                "verdict": {
                    "type": "string",
                    "source": {"agentic": True},
                    "stale_after_s": 3600,
                }
            },
        }
    )
    row = {"value": "green", "at": "2026-11-08T10:00:00+00:00"}
    monkeypatch.setattr(dashboard_agentic, "page_in_force", lambda slug: (None, manifest))
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    monkeypatch.setattr(
        md,
        "read_fields",
        lambda *a, **k: SimpleNamespace(fields={"verdict": "green"}, missing=[]),
    )
    monkeypatch.setattr(md, "page_safe_for", lambda state, slot: lambda value: value)
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name: SimpleNamespace(value={"fields": {"verdict": row}}),
    )
    out = member_dashboard_lineage._read_lineage("atlas", "Atlas", "verdict", None)
    freshness = next(hop for hop in out["hops"] if hop["kind"] == "freshness")
    assert freshness["max_age_s"] == 3600
    assert freshness["stale_after"] == "2026-11-08T11:00:00+00:00"


def test_a_script_record_splits_what_the_gateway_stamped_from_what_the_script_said():
    custom = {
        "latest": {
            "p99": {
                "data": {"ms": 412},
                "writer": "cron:p99-hourly",
                "at": "2026-11-08T10:00:00+00:00",
                "seq": 9,
                "max_age_s": 7200,
                "stale_after": "2026-11-08T12:00:00+00:00",
                "failed_at": "2026-11-08T11:00:00+00:00",
                "last_error": "throttled",
                "source": "CloudWatch TargetResponseTime p99",
                "points": [400, 412],
                "provenance": {
                    "job": "p99-hourly",
                    "run_at": "x",
                    "script": "crons/p.py:run",
                    "sha": "abc",
                },
            }
        }
    }
    out = member_dashboard_lineage.build_lineage(
        _spec("p99", fold_name="custom", path="latest.p99.data.ms"),
        [],
        path="p99",
        field_value=412,
        field_found=True,
        custom=custom,
    )
    by_kind = {hop["kind"]: hop for hop in out["hops"]}
    assert by_kind["record"]["writer"] == "cron:p99-hourly"
    assert by_kind["script"]["by"] == "gateway" and by_kind["script"]["sha"] == "abc"
    assert by_kind["source"]["by"] == "script" and by_kind["source"]["points"] == [400, 412]
    assert by_kind["freshness"]["by"] == "script"
    assert by_kind["run"]["last_error"] == "throttled"
    assert out["edit"] == {"kind": "none", "reason": "cron", "writer": "cron:p99-hourly"}


def test_every_value_leaves_through_the_pages_redaction():
    out = member_dashboard_lineage.build_lineage(
        _spec("accepted", fold_name="workstreams", path="accepted"),
        [],
        path="accepted",
        field_value=7,
        field_found=True,
        redact=lambda value: {"redacted": True},
    )
    assert out == {"redacted": True}


# -------------------------------------------------------------------- the folds


def _agentic(data, seq=1):
    return Entry(
        type="dashboard/agentic_value", seq=seq, time=1_760_000_000_000, src="gateway", data=data
    )


def _write(value, **extra):
    return {"field": "verdict", "type": "string", "value": {"v": value}, **extra}


def test_a_bots_write_folds_to_the_row_it_always_did():
    state = fold._agentic_start()
    fold._agentic_step(state, _agentic(_write("red")))
    fold._agentic_step(state, _agentic(_write("amber"), seq=2))
    assert set(state["fields"]["verdict"]) == {
        "value",
        "type",
        "at",
        "instance_version",
        "crew_key",
        "order",
    }


def test_a_person_write_keeps_its_actor_reason_and_the_write_it_replaced():
    state = fold._agentic_start()
    fold._agentic_step(state, _agentic(_write("red")))
    fold._agentic_step(state, _agentic(_write("green", actor="person", reason="fixed"), seq=5))
    row = state["fields"]["verdict"]
    assert row["actor"] == "person" and row["reason"] == "fixed" and row["seq"] == 5
    assert row["prior"]["value"] == "red"
    fold._agentic_step(state, _agentic(_write("red"), seq=6))
    over = state["fields"]["verdict"]
    assert "actor" not in over and over["prior"]["actor"] == "person"


def _custom(data):
    return Entry(
        type="dashboard/custom_value", seq=3, time=1_760_000_000_000, src="gateway", data=data
    )


def test_the_custom_fold_keeps_a_records_lineage_cells():
    state = fold._custom_start()
    fold._custom_step(
        state,
        _custom(
            {
                "type": "p99",
                "data": {"ms": 1},
                "writer": "cron:x",
                "source": "CloudWatch p99",
                "points": [1, 2.5],
                "provenance": {"job": "x", "sha": "abc", "evil": "dropped"},
            }
        ),
    )
    row = state["latest"]["p99"]
    assert row["source"] == "CloudWatch p99" and row["points"] == [1, 2.5]
    assert row["provenance"] == {"job": "x", "run_at": "", "script": "", "sha": "abc"}


def test_the_custom_fold_drops_points_that_are_not_numbers():
    state = fold._custom_start()
    fold._custom_step(
        state, _custom({"type": "p", "data": {"v": 1}, "writer": "cron:x", "points": [1, True]})
    )
    assert "points" not in state["latest"]["p"]


# ---------------------------------------------------------------- record door


@pytest.mark.parametrize(
    "body,code",
    [
        ({"source": "x\ny"}, "bad_source"),
        ({"source": "s" * 241}, "bad_source"),
        ({"points": []}, "bad_points"),
        ({"points": [1, "2"]}, "bad_points"),
        ({"points": [float("inf")]}, "bad_points"),
        ({"source": "CloudWatch", "points": [1, 2]}, None),
    ],
)
def test_the_record_door_checks_a_declared_source_and_points(body, code):
    refused = agent_panel_record._lineage_body_refusal(body)
    assert (refused[0] if refused else None) == code


def test_the_gateway_stamps_the_job_and_the_scripts_sha(tmp_path, monkeypatch):
    script = tmp_path / "p.py"
    script.write_text("def run(ctx): pass\n", encoding="utf-8")
    import kiro_crew.cron_script as cron_script

    monkeypatch.setattr(cron_script, "resolve_script_path", lambda spec, **kw: (str(script), "run"))
    state = SimpleNamespace(
        crons=SimpleNamespace(_jobs=[SimpleNamespace(id="j1", script="p.py:run")])
    )
    stamp = agent_panel_record.cron_provenance(state, "cron:j1")
    import hashlib

    assert stamp["job"] == "j1" and stamp["script"] == "p.py:run"
    assert stamp["sha"] == hashlib.sha256(script.read_bytes()).hexdigest()[:12]
    assert agent_panel_record.cron_provenance(state, "cron:gone")["script"] == ""


# ---------------------------------------------------------------------- routes


def test_the_routes_are_bound_through_the_deferred_binder():
    from aiohttp import web

    from kiro_crew.dashboard import server_runtime
    from kiro_crew.dashboard.server_runtime import mcp_routes

    app = web.Application()
    member_dashboard_lineage.register_member_dashboard_lineage_routes(app)
    ours = {route.resource.canonical for route in app.router.routes()}
    flat = " ".join(inspect.getsource(mcp_routes).split())
    for path in ours:
        assert f'"{path}", _deferred("member_dashboard_lineage",' in flat, path
    assert server_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/members/atlas/dashboard/lineage?member=Atlas&path=accepted"),
        ("POST", "/api/members/atlas/dashboard/correct?member=Atlas"),
    ],
)
async def test_off_both_routes_answer_not_enabled(preview_off, method, path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    member_dashboard_lineage.register_member_dashboard_lineage_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, json={"field": "verdict", "value": "x"})
        assert resp.status == 404
        assert (await resp.json())["code"] == "not_enabled"


def test_the_bot_is_told_about_a_persons_correction(preview_on, monkeypatch):
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard

    rows = {
        "verdict": {"value": "green", "at": "T", "actor": "person", "reason": "fixed"},
        "other": {"value": 1, "at": "T"},
    }
    monkeypatch.setattr(member_dashboard, "_dashboard_slot", lambda member, slug: "slot")
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name: SimpleNamespace(value={"fields": rows}),
    )
    line = dashboard_agentic.person_corrections_line("atlas", "Atlas", ["verdict", "other"])
    assert 'verdict = "green" at T ("fixed")' in line and "other" not in line


def test_off_the_bot_is_told_nothing(preview_off):
    from kiro_crew import dashboard_agentic

    assert dashboard_agentic.person_corrections_line("atlas", "Atlas", ["verdict"]) == ""
