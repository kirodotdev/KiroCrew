"""Dashboard edit mode (feature preview ``dashboard_edit_mode``): the gateway's half.

Off, every page, write, fold row and turn is what it was without the preview. On, a
page's components carry ids, the frame gets the edit bridge, a picked path resolves to a
lineage built from the gateway's own data, the person can correct a value the Bot
writes, and a script's record carries what it read beside the gateway's own stamp.
"""

from __future__ import annotations

import inspect
import json
import pathlib
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


@pytest.fixture(autouse=True)
def _no_package_page(_floor_monkeypatch):
    """No package page in force unless a test says so, so a lineage read here never
    reaches the real instance store."""
    from kiro_crew import dashboard_package

    _floor_monkeypatch.setattr(
        dashboard_package,
        "read_package_model",
        lambda slug: SimpleNamespace(
            model=None, state="empty", state_reason="", from_package=False
        ),
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


def test_on_a_block_id_that_is_not_a_slug_is_refused_on_write(preview_on):
    bad = {"blocks": [{"id": 'x" onclick="1', "fields": ["accepted"]}]}
    with pytest.raises(ArtifactValidationError, match=r"view\.blocks\[0\]\.id"):
        pkg.canonical_package_content(_package(bad))


@pytest.mark.parametrize("stored_id", ["openPrs", "Lead", "x" * 60, 'x" onclick="1', 7])
def test_on_a_stored_block_id_that_is_not_a_slug_still_reads(preview_on, stored_id):
    """A page stored before ids meant anything keeps drawing with the preview on:
    the id is left out and the block is a plain section. Reddens if the read refuses."""
    page = pkg.parse_package(_package({"blocks": [{"id": stored_id, "fields": ["accepted"]}]}))
    assert page["view"]["html"].startswith("<section><h2>") or page["view"]["html"].startswith(
        "<section><dl>"
    )
    assert "data-dashboard-block" not in page["view"]["html"]


def test_on_the_bots_snapshot_reads_a_stored_block_id_that_is_not_a_slug(preview_on, monkeypatch):
    """The snapshot canonicalises stored bytes: the write-only id check stays out of it."""
    from kiro_crew.dashboard.handlers import agent_panel_snapshot

    stored = _package({"blocks": [{"id": "openPrs", "fields": ["accepted"]}]})
    with pytest.raises(ArtifactValidationError):
        pkg.canonical_package_content(stored)  # the write gate still refuses it
    from kiro_crew.dashboard_templates import instance as instance_store

    monkeypatch.setattr(instance_store, "read", lambda slug: SimpleNamespace(package_slug=""))
    monkeypatch.setattr(instance_store, "pinned_package", lambda record: None)
    monkeypatch.setattr(instance_store, "keeps_older_package_rule", lambda record: True)
    monkeypatch.setattr(agent_panel_snapshot, "_older_page", lambda slug: ("atlas-dash", 3, stored))
    package, package_slug, version = agent_panel_snapshot._page_in_force("atlas")
    assert (package_slug, version) == ("atlas-dash", 3)
    assert "data-dashboard-block" not in package["view"]["html"]


def test_on_dashboard_apply_puts_a_stored_page_with_an_old_block_id_live(preview_on, monkeypatch):
    """``dashboard_apply`` re-pins stored bytes: the write-only id check stays out of it,
    so turning the preview on never strands a page the Bot already saved."""
    from kiro_crew.dashboard.handlers import agent_panel_dashboard
    from kiro_crew.dashboard_templates import instance as instance_store

    stored = _package({"blocks": [{"id": "openPrs", "fields": ["accepted"]}]})
    store = SimpleNamespace(get=lambda slug: SimpleNamespace(content=stored, version=3))
    applied: dict[str, Any] = {}
    monkeypatch.setattr(agent_panel_dashboard, "_dashboard_artifact_store", lambda: store)
    monkeypatch.setattr(pkg, "resolve_bound_slug", lambda binding, store: "atlas-dash")
    monkeypatch.setattr(agent_panel_dashboard, "_history_session", lambda slot: "sess")
    monkeypatch.setattr(
        instance_store, "apply_package", lambda slug, **kw: applied.update(kw) or "ok"
    )
    assert agent_panel_dashboard._apply_own_package("atlas", "crew", "member-atlas") == "ok"
    assert (applied["package_slug"], applied["package_version"]) == ("atlas-dash", 3)


DUPLICATE = {
    "html": '<div data-dashboard-block="a"><b data-dashboard-field="accepted"></b></div>'
    "<div data-dashboard-block='a'></div>"
}


def test_on_the_write_gate_refuses_a_component_id_used_twice(preview_on):
    with pytest.raises(ArtifactValidationError, match="names component 'a' twice"):
        pkg.canonical_package_content(_package(DUPLICATE))


def test_the_bridge_forwards_only_keys_the_person_pressed():
    """A page script's dispatched KeyboardEvent is untrusted and is never forwarded."""
    bridge = dashboard_frame._EDIT_BRIDGE_JS
    keydown = bridge[bridge.index("addEventListener('keydown'") :]
    assert "!event.isTrusted" in keydown[: keydown.index("postMessage")]


def test_the_bridge_forwards_the_edit_key_from_a_focused_value_only():
    """A key typed in the frame never reaches the host window, so the bridge forwards
    a bare E on a bound value, and nothing with a modifier held."""
    bridge = dashboard_frame._EDIT_BRIDGE_JS
    i = bridge.index("kirocrew-dashboard:edit-key")
    guard = bridge[bridge.rindex("else if", 0, i) : i]
    assert "bound(event.target)" in guard
    for modifier in ("ctrlKey", "metaKey", "altKey"):
        assert f"!event.{modifier}" in guard


def test_the_bridge_scrolls_the_box_under_the_pointer_before_the_document():
    """A forwarded scroll moves the scrollable box under the point (a drawer) first;
    the document scrolls only when nothing under the point can move that way."""
    bridge = dashboard_frame._EDIT_BRIDGE_JS
    wheel = bridge[bridge.index("'kirocrew-dashboard:wheel'") :]
    assert "scrollUnder(data.x, data.y, dy)" in wheel[: wheel.index("}, true);")]
    under = bridge[bridge.index("function scrollUnder") :]
    assert under.index("elementFromPoint") < under.index("el.scrollBy(0, dy)")
    assert under.index("el.scrollBy(0, dy)") < under.index("window.scrollBy(0, dy)")


def test_a_component_selector_in_script_or_style_is_not_a_component():
    """Only an attribute on an element names a component; a script's selector, a
    style rule and a comment naming the same id do not count it twice."""
    page = (
        '<section data-dashboard-block="chart"><b data-dashboard-field="accepted"></b></section>'
        "<script>document.querySelector('[data-dashboard-block=\"chart\"]')</script>"
        '<style>[data-dashboard-block="chart"] { color: red }</style>'
        '<!-- <div data-dashboard-block="chart"> -->'
    )
    assert dashboard_edit_mode.block_ids(page) == ["chart"]
    assert dashboard_edit_mode.block_id_refusal(page) == ""
    # Two real elements with one id are still refused.
    assert "twice" in dashboard_edit_mode.block_id_refusal(
        page + '<i data-dashboard-block="chart"></i>'
    )


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


#: One table for both parsers; dashboardEdit.test.ts runs the same rows against the TS side.
_SHARED = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "dashboard_edit_paths.json").read_text("utf-8")
)


@pytest.mark.parametrize("row", _SHARED["paths"], ids=lambda r: repr(r["path"])[:40])
def test_the_shared_path_table_parses_the_same_in_python(row):
    parsed = dashboard_edit_mode.parse_field_path(row["path"])
    assert (parsed[0] if parsed else None) == row["root"]


@pytest.mark.parametrize("row", _SHARED["block_ids"], ids=lambda r: repr(r["id"])[:40])
def test_the_shared_block_id_table_checks_the_same_in_python(row):
    assert dashboard_edit_mode.is_block_id(row["id"]) is row["ok"]
    assert pkg._block_id_ok(row["id"]) is row["ok"]


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
    assert out["edit"] == {"kind": "fold"}


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
        lambda slot, name, **_: SimpleNamespace(value={"fields": {"verdict": row}}),
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
                "source": {"kind": "cloudwatch", "metric": "Latency", "stat": "p99"},
                "points": [[1794141900, 400], [1794141960, 412]],
                "provenance": {
                    "job": "p99-hourly",
                    "run_at": "x",
                    "script": "crons/p.py:run",
                    "script_sha": "abc",
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
    kinds = [hop["kind"] for hop in out["hops"]]
    # Current? still running? which script? what it says it read?
    assert kinds == ["field", "fold", "record", "freshness", "run", "script", "source"]
    by_kind = {hop["kind"]: hop for hop in out["hops"]}
    assert by_kind["record"]["writer"] == "cron:p99-hourly"
    # A digest an older stamp carried is not shown: it vouched for the file, not the run.
    assert by_kind["script"]["by"] == "gateway" and "script_sha" not in by_kind["script"]
    assert by_kind["script"]["needs"] == member_dashboard_lineage.NEEDS_RUN_ID
    assert (
        by_kind["source"]["by"] == "script" and by_kind["source"]["source"]["metric"] == "Latency"
    )
    assert by_kind["freshness"]["by"] == "script"
    assert by_kind["run"]["last_error"] == "throttled"
    assert out["edit"] == {"kind": "source", "writer": "cron:p99-hourly", "job": "p99-hourly"}


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
                "source": {"kind": "cloudwatch", "metric": "Latency"},
                "points": [[1, 2.5]],
                # A digest is not a cell the fold keeps: no stamp vouches for the code.
                "provenance": {"job": "x", "script_sha": "abc", "evil": "dropped"},
            }
        ),
    )
    row = state["latest"]["p99"]
    assert row["source"] == {"kind": "cloudwatch", "metric": "Latency"}
    assert row["points"] == [[1, 2.5]]
    assert row["provenance"] == {"job": "x", "run_at": "", "script": ""}


@pytest.mark.parametrize(
    "extra",
    [{"points": [[1, True]]}, {"points": [1, 2]}, {"source": {"kind": "ftp"}}],
)
def test_the_custom_fold_drops_lineage_cells_it_would_refuse(extra):
    state = fold._custom_start()
    fold._custom_step(state, _custom({"type": "p", "data": {"v": 1}, "writer": "cron:x", **extra}))
    assert "points" not in state["latest"]["p"] and "source" not in state["latest"]["p"]


# ---------------------------------------------------------------- record door


@pytest.mark.parametrize(
    "body,code",
    [
        ({"source": "CloudWatch"}, "bad_source"),
        ({"source": {"kind": "ftp"}}, "bad_source"),
        ({"source": {"kind": "other"}}, "bad_source"),
        ({"source": {"kind": "http", "console_url": "http://x"}}, "bad_source"),
        ({"source": {"kind": "http", "label": "s" * 3000}}, "bad_source"),
        ({"points": []}, "bad_points"),
        ({"points": [[1, "2"]]}, "bad_points"),
        ({"points": [[1, float("inf")]]}, "bad_points"),
        # A JSON integer past a float's range: refused as bad points, not a 500.
        ({"points": [[1, 10**400]]}, "bad_points"),
        # An integer a float could hold but that serializes as 201 digits: the row
        # charge assumes the widest float, so a wider reading is refused.
        ({"points": [[1, 10**200]]}, "bad_points"),
        ({"points": [1, 2]}, "bad_points"),
        (
            {
                "source": {"kind": "cloudwatch", "console_url": "https://console.example"},
                "points": [[1794141900, 351]],
            },
            None,
        ),
    ],
)
def test_the_record_door_checks_a_declared_source_and_points(body, code):
    refused = agent_panel_record._lineage_body_refusal(body)
    assert (refused[0] if refused else None) == code


def test_a_source_is_judged_in_the_form_it_is_stored():
    """A descriptor that fits raw but not once its credential is redacted is refused
    at the door, instead of being accepted and then dropped by the fold. Reddens if
    the door stops judging the scrubbed form."""
    from kiro_crew.crew_log.entry_types import CUSTOM_SOURCE_BYTES
    from kiro_crew.crew_log.projection_dashboard import custom_source_refusal

    def source(pad):
        return {"kind": "http", "label": "p" * pad, "headers": {"Authorization": "Bearer ab12"}}

    size = len(json.dumps(source(0), ensure_ascii=False).encode("utf-8"))
    raw = source(CUSTOM_SOURCE_BYTES - size - 2)
    assert custom_source_refusal(raw) == ""  # it fits as sent
    stored, refused = agent_panel_record._stored_source(raw)
    assert stored is None and refused.startswith("after redaction")
    small, ok = agent_panel_record._stored_source(source(0))
    assert ok == "" and "Bearer" not in json.dumps(small)


def test_a_console_link_the_redactor_rewrites_is_dropped_not_the_record(monkeypatch):
    """A valid https link that redaction turns into a placeholder loses the LINK, and
    the record's source is kept. Reddens if the door refuses the whole source again."""
    real = agent_panel_record._redacted_record

    def scrub(value, key=""):
        out = real(value, key)
        if isinstance(out, dict) and "console_url" in out:
            out = {**out, "console_url": "[REDACTED]"}
        return out

    monkeypatch.setattr(agent_panel_record, "_redacted_record", scrub)
    stored, refused = agent_panel_record._stored_source(
        {"kind": "cloudwatch", "label": "p99", "console_url": "https://console.example/x"}
    )
    assert refused == ""
    assert stored == {"kind": "cloudwatch", "label": "p99"}


@pytest.mark.parametrize(
    "url,kept",
    [
        # A real https console link the script's own redactor rewrites: the LINK goes.
        ("https://console.aws.amazon.com/cloudwatch/home?view=" + "a" * 500, False),
        # A link that was never https is sent as given, so the door still refuses it.
        ("http://console.example/x", True),
    ],
)
def test_a_console_link_the_script_redacts_is_left_out_not_the_record(url, kept):
    """``ctx.crew_log_append`` scrubs the source before it posts. A valid https link
    the scrub turns into a placeholder is not https, and the door would refuse
    the whole record with 400 bad_source. Reddens if the client sends the placeholder."""
    from kiro_crew.cron_script_context import ScriptContext

    sent: list[dict] = []
    ctx = ScriptContext(job=SimpleNamespace(id="j1"))
    ctx._post = lambda path, body: sent.append(body) or {"ok": True}
    ctx.crew_log_append("p99", {"ms": 412}, source={"kind": "cloudwatch", "console_url": url})
    source = sent[0]["source"]
    assert ("console_url" in source) is kept
    assert source["kind"] == "cloudwatch"
    if kept:
        assert source["console_url"] == url


def test_the_gateway_stamps_the_job_and_never_a_digest_of_the_file_on_disk(monkeypatch):
    """The file when the record arrives may not be the code that ran (an edit mid-run),
    so no digest of it is stamped; the stamp never reads the script at all."""
    import kiro_crew.cron_script as cron_script

    def no_read(*a, **k):
        raise AssertionError("the stamp must not read the script file")

    monkeypatch.setattr(cron_script, "resolve_script_path", no_read)
    state = SimpleNamespace(
        crons=SimpleNamespace(_jobs=[SimpleNamespace(id="j1", script="p.py:run")])
    )
    stamp = agent_panel_record.cron_provenance(state, "cron:j1")

    assert stamp["job"] == "j1" and stamp["script"] == "p.py:run"
    assert "script_sha" not in stamp
    assert "run_id" not in stamp
    assert agent_panel_record.cron_provenance(state, "cron:gone")["script"] == ""


@pytest.mark.parametrize(
    "spec,shown",
    [
        # Under the home directory: home-relative, so the user name is not shown.
        ("{home}/.kiro/crew/crons/checkout_p99.py:run", "~/.kiro/crew/crons/checkout_p99.py:run"),
        # Outside it: only the file name, never the host's directory layout.
        ("/opt/runner/work/checkout_p99.py:run", "checkout_p99.py:run"),
        # A Windows host's path, on any host: still only the file name.
        ("C:\\runner\\work\\checkout_p99.py:run", "checkout_p99.py:run"),
        # Already relative: kept as written.
        ("crons/checkout_p99.py:run", "crons/checkout_p99.py:run"),
    ],
)
def test_the_stamp_names_the_script_without_the_host_layout(monkeypatch, tmp_path, spec, shown):
    """The lineage shows this stamp in the page's popover and job panel, so an
    absolute path would print the host's directory layout there. Reddens if the
    stamp keeps the job's spec as written."""
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    state = SimpleNamespace(
        crons=SimpleNamespace(
            _jobs=[SimpleNamespace(id="j1", script=spec.format(home=tmp_path.as_posix()))]
        )
    )
    assert agent_panel_record.cron_provenance(state, "cron:j1")["script"] == shown


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


# ------------------------------------------------------------------ the routes, on


@pytest.fixture
def gate_open(preview_on, monkeypatch):
    """The preview on and the member, owner and agent-caller checks passing, so a
    route test exercises the route's own logic."""
    from kiro_crew.dashboard.handlers import member_dashboard as md

    async def resolve(request):
        return ("atlas", "Atlas")

    async def owner_only(request, operation):
        return None

    monkeypatch.setattr(md, "_resolve", resolve)
    monkeypatch.setattr(md, "_owner_only", owner_only)
    monkeypatch.setattr(md, "_refuse_agent_caller", lambda request, op, message: None)
    return md


async def _call(method, path, **kwargs):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    member_dashboard_lineage.register_member_dashboard_lineage_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, **kwargs)
        return resp.status, await resp.json()


_LINEAGE = "/api/members/atlas/dashboard/lineage?member=Atlas&path=verdict"
_CORRECT = "/api/members/atlas/dashboard/correct?member=Atlas"

#: A template page in force, and the page a correction names when typed against it.
_LIVE_TEMPLATE = SimpleNamespace(
    state="live",
    state_reason="",
    from_package=False,
    model=SimpleNamespace(instance_version=4, manifest=SimpleNamespace(id="project-report")),
)
_PAGE = {"page": "template:project-report", "page_version": 4}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [("GET", _LINEAGE), ("POST", _CORRECT)],
)
async def test_a_non_owner_is_refused_both_routes_by_the_real_owner_check(
    preview_on, monkeypatch, method, path
):
    """The owner check in `_gate`, exercised for real: `_owner_only` is NOT stubbed. A
    dashboard caller who is not the configured owner gets 403 and the route never reads
    the page or writes. Reddens if `_gate` stops calling `md._owner_only`."""
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import member_dashboard as md

    async def resolve(request):
        return ("atlas", "Atlas")

    reached: list[str] = []
    monkeypatch.setattr(md, "_resolve", resolve)
    monkeypatch.setattr(
        member_dashboard_lineage, "_read_lineage", lambda *a: reached.append("read") or {}
    )
    monkeypatch.setattr(
        member_dashboard_lineage, "_append_correction", lambda *a: reached.append("write") or True
    )

    @web.middleware
    async def as_someone_else(request, handler):
        # A dashboard (not app) caller signed in as a user who is not the owner.
        request["app"] = ""
        request["user"] = "someone-else"
        return await handler(request)

    app = web.Application(middlewares=[as_someone_else])
    app["state"] = SimpleNamespace(owner_id="the-owner")
    member_dashboard_lineage.register_member_dashboard_lineage_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, json={"field": "verdict", "value": "x"})
        assert resp.status == 403, await resp.text()
    assert reached == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,status,code",
    [("no_page", 404, "no_page"), ("invalid_path", 400, "invalid_path"), (None, 500, None)],
)
async def test_the_lineage_route_answers_a_refusal_or_a_failure_by_its_code(
    gate_open, monkeypatch, answer, status, code
):
    def read(slug, member, path, state):
        if answer is None:
            raise OSError("disk")
        return answer

    monkeypatch.setattr(member_dashboard_lineage, "_read_lineage", read)
    got_status, body = await _call("GET", _LINEAGE)
    assert got_status == status
    assert body["code"] == (code or "lineage_failed")


@pytest.mark.asyncio
async def test_the_lineage_route_returns_the_chain(gate_open, monkeypatch):
    monkeypatch.setattr(
        member_dashboard_lineage, "_read_lineage", lambda *a: {"path": "verdict", "hops": []}
    )
    assert await _call("GET", _LINEAGE) == (200, {"path": "verdict", "hops": []})


def test_the_lineage_read_refuses_a_bad_path_a_missing_page_and_an_undeclared_field(
    monkeypatch,
):
    from kiro_crew import dashboard_agentic

    read = member_dashboard_lineage._read_lineage
    assert read("atlas", "Atlas", "Items", None) == "invalid_path"
    monkeypatch.setattr(dashboard_agentic, "page_in_force", lambda slug: None)
    assert read("atlas", "Atlas", "verdict", None) == "no_page"
    manifest = SimpleNamespace(fields={})
    monkeypatch.setattr(dashboard_agentic, "page_in_force", lambda slug: (None, manifest))
    assert read("atlas", "Atlas", "verdict", None) == "field_not_declared"


def test_a_package_page_is_traced_against_the_package_not_the_template(monkeypatch):
    """The read route serves an older record's bound package; the lineage must read
    that package's fields, not the template's. Reddens if `_read_lineage` goes back to
    `page_in_force` alone."""
    from kiro_crew import dashboard_agentic, dashboard_package

    package = SimpleNamespace(fields={"p99": _spec("p99", fold_name="custom", path="latest.p99")})
    template = SimpleNamespace(fields={"accepted": _spec("accepted", fold_name="w", path="a")})
    monkeypatch.setattr(
        dashboard_package,
        "read_package_model",
        lambda slug: SimpleNamespace(
            model=SimpleNamespace(manifest=package),
            state="live",
            state_reason="",
            from_package=True,
        ),
    )
    monkeypatch.setattr(dashboard_agentic, "page_in_force", lambda slug: (None, template))
    assert member_dashboard_lineage._manifest_in_force("atlas") == (package, True)
    assert member_dashboard_lineage._read_lineage("atlas", "Atlas", "accepted", None) == (
        "field_not_declared"
    )


def test_the_lineage_read_traces_a_script_record_through_the_custom_fold(monkeypatch):
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md

    spec = _spec("p99", fold_name="custom", path="latest.p99.data.ms")
    custom = {"latest": {"p99": {"writer": "cron:p99-probe", "data": {"ms": 412}}}}
    monkeypatch.setattr(
        dashboard_agentic,
        "page_in_force",
        lambda slug: (None, SimpleNamespace(fields={"p99": spec})),
    )
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    monkeypatch.setattr(
        md, "read_fields", lambda *a, **k: SimpleNamespace(fields={"p99": 412}, missing=[])
    )
    monkeypatch.setattr(md, "page_safe_for", lambda state, slot: lambda value: value)
    monkeypatch.setattr(
        projection, "read_slot_projection", lambda slot, name, **_: SimpleNamespace(value=custom)
    )
    out = member_dashboard_lineage._read_lineage("atlas", "Atlas", "p99", None)
    assert out["kind"] == "custom" and out["value"] == 412
    assert out["edit"] == {"kind": "source", "writer": "cron:p99-probe", "job": "p99-probe"}


def test_the_lineage_read_walks_the_value_the_page_drew_with_worker_facts(monkeypatch):
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md

    spec = _spec("items", fold_name="board", path="items")
    monkeypatch.setattr(
        dashboard_agentic,
        "page_in_force",
        lambda slug: (None, SimpleNamespace(fields={"items": spec})),
    )
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    monkeypatch.setattr(
        md,
        "read_fields",
        lambda *a, **k: SimpleNamespace(fields={"items": [{"item_id": "it_1"}]}, missing=[]),
    )
    monkeypatch.setattr(
        projection, "read_slot_projection", lambda slot, name, **_: SimpleNamespace(value={})
    )
    # The page's pass stamps each work item; the lineage walks what the page was sent.
    monkeypatch.setattr(
        md,
        "page_safe_for",
        lambda state, slot: lambda value: (
            [dict(v, worker="running") for v in value] if isinstance(value, list) else value
        ),
    )
    out = member_dashboard_lineage._read_lineage("atlas", "Atlas", "items[0].worker", None)
    assert out["value"] == "running"


def test_the_lineage_read_reaches_no_key_the_page_mask_removes(monkeypatch):
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md

    spec = _spec("items", fold_name="board", path="items")
    monkeypatch.setattr(
        dashboard_agentic,
        "page_in_force",
        lambda slug: (None, SimpleNamespace(fields={"items": spec})),
    )
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    item = {"item_id": "it_1", "worker_session_key": "chat-secret-1"}
    monkeypatch.setattr(
        md, "read_fields", lambda *a, **k: SimpleNamespace(fields={"items": [item]}, missing=[])
    )
    monkeypatch.setattr(
        projection, "read_slot_projection", lambda slot, name, **_: SimpleNamespace(value={})
    )
    # The page's real pass, which drops a worker's session key from what the page sees.
    out = member_dashboard_lineage._read_lineage(
        "atlas", "Atlas", "items[0].worker_session_key", None
    )
    assert "chat-secret-1" not in repr(out)


def test_the_lineage_read_resolves_a_tree_field_the_feed_reports_missing(monkeypatch):
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md

    spec = _spec("cycles", fold_name="worktree", path="cycles")
    monkeypatch.setattr(
        dashboard_agentic,
        "page_in_force",
        lambda slug: (None, SimpleNamespace(fields={"cycles": spec})),
    )
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    # The feed has no subscription for a tree fold, so it reports the field missing.
    monkeypatch.setattr(
        md, "read_fields", lambda *a, **k: SimpleNamespace(fields={}, missing=["cycles"])
    )
    monkeypatch.setattr(md, "page_safe_for", lambda state, slot: lambda value: value)
    folds: list[tuple[str, str, bool]] = []

    def warm(name, units, *, slot, cold=True):
        folds.append((slot, name, cold))
        return "checkpoint", 1

    monkeypatch.setattr(projection, "tree_fold_units", lambda root, name: (("u1",), 0))
    monkeypatch.setattr(projection, "fold_slot_warm_revised", warm)
    monkeypatch.setattr(
        projection,
        "projection_of",
        lambda checkpoint, revision=0: SimpleNamespace(value={"cycles": 4}),
    )
    out = member_dashboard_lineage._read_lineage("atlas", "Atlas", "cycles", None)
    assert out["found"] is True and out["value"] == 4
    # The tree rooted at the page's slot, read warm only: a request never folds cold.
    assert folds == [("slot", "worktree", False)]

    def not_warm(name, units, *, slot, cold=True):
        raise projection.SlotFoldNotReady()

    monkeypatch.setattr(projection, "fold_slot_warm_revised", not_warm)
    assert member_dashboard_lineage._read_lineage("atlas", "Atlas", "cycles", None) == "not_ready"


def _trace_credits_unit(monkeypatch, *, package: bool) -> Any:
    """The ``session`` ``_read_lineage`` hands ``read_fields`` for a ``usage`` field."""
    from kiro_crew import dashboard_agentic, dashboard_package
    from kiro_crew.dashboard.handlers import member_dashboard as md

    spec = _spec("credits", fold_name="usage", path="credits")
    manifest = SimpleNamespace(fields={"credits": spec})
    monkeypatch.setattr(
        dashboard_package,
        "read_package_model",
        lambda slug: SimpleNamespace(
            model=SimpleNamespace(manifest=manifest),
            state="live",
            state_reason="",
            from_package=package,
        ),
    )
    monkeypatch.setattr(dashboard_agentic, "page_in_force", lambda slug: (None, manifest))
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(md, "_live_or_newest_unit", lambda state, slot: f"live-of-{slot}")

    def read_fields(*args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(fields={"credits": 94.1}, missing=[])

    monkeypatch.setattr(md, "read_fields", read_fields)
    monkeypatch.setattr(md, "page_safe_for", lambda state, slot: lambda value: value)
    member_dashboard_lineage._read_lineage("atlas", "Atlas", "credits", SimpleNamespace())
    return seen.get("session", "<not passed>")


def test_the_lineage_reads_the_session_unit_the_page_shows(monkeypatch):
    """A package page's session fold (``usage``) is traced from the unit its live view
    reads: the live session's, not the newest unit on the slot."""
    assert _trace_credits_unit(monkeypatch, package=True) == "live-of-slot"


def test_a_template_page_lineage_reads_the_unit_the_template_render_reads(monkeypatch):
    """A template page renders through ``read_fields`` with no unit, which picks
    ``_write_session``; the lineage passes none too, so its value is the shown one.
    Reddens if the lineage hands a template page the live unit."""
    assert _trace_credits_unit(monkeypatch, package=False) is None


def test_the_tree_lineage_counts_the_units_its_selection_dropped(monkeypatch):
    """``worktree.dropped`` reads what the page shows: the fold's own count plus the
    units the read's selection left out, as ``read_tree_projection`` adds them."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md

    spec = _spec("dropped", fold_name="worktree", path="dropped")
    monkeypatch.setattr(
        dashboard_agentic,
        "page_in_force",
        lambda slug: (None, SimpleNamespace(fields={"dropped": spec})),
    )
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    monkeypatch.setattr(
        md, "read_fields", lambda *a, **k: SimpleNamespace(fields={}, missing=["dropped"])
    )
    monkeypatch.setattr(md, "page_safe_for", lambda state, slot: lambda value: value)
    monkeypatch.setattr(projection, "tree_fold_units", lambda root, name: (("u1",), 1))
    monkeypatch.setattr(
        projection, "fold_slot_warm_revised", lambda name, units, *, slot, cold=True: ("c", 1)
    )
    monkeypatch.setattr(
        projection,
        "projection_of",
        lambda checkpoint, revision=0: SimpleNamespace(value={"dropped": 0}),
    )
    out = member_dashboard_lineage._read_lineage("atlas", "Atlas", "dropped", None)
    assert out["found"] is True and out["value"] == 1


def test_a_lineage_on_a_cold_slot_answers_not_ready_and_primes_it(monkeypatch):
    """A slot fold with no warm cell is not folded on the request: the route answers
    "not ready" (503) and hands the cell to one background prime."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection, slot_prime
    from kiro_crew.dashboard.handlers import member_dashboard as md

    spec = _spec("p99", fold_name="custom", path="latest.p99.data.ms")
    monkeypatch.setattr(
        dashboard_agentic,
        "page_in_force",
        lambda slug: (None, SimpleNamespace(fields={"p99": spec})),
    )
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    monkeypatch.setattr(
        md, "read_fields", lambda *a, **k: SimpleNamespace(fields={}, missing=["p99"])
    )
    reads: list[bool] = []

    def cold_cell(slot, name, *, cold=True):
        reads.append(cold)
        raise projection.SlotFoldNotReady()

    primed: list[tuple[str, str]] = []
    monkeypatch.setattr(projection, "read_slot_projection", cold_cell)
    monkeypatch.setattr(slot_prime, "schedule", lambda s, n, **k: primed.append((s, n)) or True)
    assert member_dashboard_lineage._read_lineage("atlas", "Atlas", "p99", None) == "not_ready"
    assert reads == [False] and primed == [("slot", "custom")]
    assert member_dashboard_lineage._REFUSALS["not_ready"][1] == 503


def test_the_lineage_value_comes_from_the_same_row_as_its_provenance(monkeypatch):
    """A correction landing between the page read and the fold read must not pair the
    Bot's earlier value with the person's write."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md

    spec = _spec("verdict", agentic=True, type_="string")
    monkeypatch.setattr(
        dashboard_agentic,
        "page_in_force",
        lambda slug: (None, SimpleNamespace(fields={"verdict": spec})),
    )
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "slot")
    # The page read still holds the Bot's value...
    monkeypatch.setattr(
        md,
        "read_fields",
        lambda *a, **k: SimpleNamespace(fields={"verdict": "Healthy"}, missing=[]),
    )
    monkeypatch.setattr(md, "page_safe_for", lambda state, slot: lambda value: value)
    # ...and the fold already holds the person's correction.
    row = {"value": "Degraded", "actor": "person", "at": "2026-11-09T05:00:00+00:00", "seq": 9}
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_: SimpleNamespace(value={"fields": {"verdict": row}}),
    )
    out = member_dashboard_lineage._read_lineage("atlas", "Atlas", "verdict", None)
    write = next(hop for hop in out["hops"] if hop["kind"] == "write")
    assert out["value"] == "Degraded" and write["actor"] == "person"


def test_a_field_bound_to_latest_finds_its_record_in_the_picked_subpath():
    row = {"data": {"ms": 412}, "writer": "cron:p99-hourly", "at": "x", "seq": 9}
    custom = {"latest": {"p99": row}}
    out = member_dashboard_lineage.build_lineage(
        _spec("records", fold_name="custom", path="latest", type_="object"),
        ["p99", "data", "ms"],
        path="records.p99.data.ms",
        field_value=custom["latest"],
        field_found=True,
        custom=custom,
    )
    record = next(hop for hop in out["hops"] if hop["kind"] == "record")
    assert record["found"] is True and record["type"] == "p99"
    assert out["edit"] == {"kind": "source", "writer": "cron:p99-hourly", "job": "p99-hourly"}


def test_readings_without_a_source_descriptor_still_reach_the_lineage():
    points = [[1, 2], [2, 3]]
    hops = member_dashboard_lineage._record_hops("p99", {"writer": "cron:j", "points": points})
    source = next(hop for hop in hops if hop["kind"] == "source")
    assert source["points"] == points and source["source"] == {}


def test_a_custom_path_that_is_not_a_latest_record_has_no_row():
    assert member_dashboard_lineage._custom_row({"latest": {}}, "items") == ("", None)
    assert member_dashboard_lineage._custom_row(None, "latest.p99") == ("p99", None)


@pytest.fixture
def correctable(gate_open, monkeypatch):
    """A live page whose ``verdict`` takes a string, and a log that takes the entry.
    Returns the appended entries, so a test reads what the route wrote."""
    from kiro_crew import agent_panel, dashboard_agentic, dashboard_package
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.platform import context as platform_context

    written: list[dict[str, Any]] = []
    monkeypatch.setattr(dashboard_package, "read_package_model", lambda slug: _LIVE_TEMPLATE)
    monkeypatch.setattr(dashboard_agentic, "package_refusal", lambda *a: None)
    monkeypatch.setattr(
        dashboard_agentic,
        "check_write",
        lambda model, field, value: {"field": field, "value": value, "type": "text"},
    )
    monkeypatch.setattr(agent_panel, "crew_key", lambda member: "crew:atlas")
    monkeypatch.setattr(platform_context, "redact_via_context", lambda text: text)
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)

    def append(slug, member, entry):
        written.append(entry)
        return True

    monkeypatch.setattr(member_dashboard_lineage, "_append_correction", append)
    return written


@pytest.mark.asyncio
async def test_a_persons_correction_is_written_as_theirs(correctable):
    status, body = await _call(
        "POST",
        _CORRECT,
        json={"field": "verdict", "value": "green", "reason": "CI is green", **_PAGE},
    )
    assert (status, body) == (200, {"ok": True, "field": "verdict"})
    assert correctable == [
        {
            "field": "verdict",
            "value": "green",
            "type": "text",
            "crew_key": "crew:atlas",
            "actor": "person",
            "reason": "CI is green",
        }
    ]


@pytest.mark.asyncio
async def test_a_correction_reads_the_page_and_appends_under_the_apply_lock(
    correctable, monkeypatch
):
    """A page apply commits under the instance lock. The correction holds the same lock
    from reading the page in force to appending, so no apply lands between its checks
    and its write."""
    from contextlib import contextmanager

    from kiro_crew import dashboard_package
    from kiro_crew.dashboard_templates import instance as instance_store

    held: list[str] = []
    seen: list[tuple[str, bool]] = []

    @contextmanager
    def lock(slug):
        held.append(slug)
        try:
            yield
        finally:
            held.pop()

    def read(slug):
        seen.append(("read", bool(held)))
        return _LIVE_TEMPLATE

    def append(slug, member, entry):
        seen.append(("append", bool(held)))
        return True

    monkeypatch.setattr(instance_store, "instance_write_lock", lock)
    monkeypatch.setattr(dashboard_package, "read_package_model", read)
    monkeypatch.setattr(member_dashboard_lineage, "_append_correction", append)
    status, _ = await _call("POST", _CORRECT, json={"field": "verdict", "value": "green", **_PAGE})
    assert status == 200
    assert seen == [("read", True), ("append", True)]


@pytest.mark.asyncio
async def test_a_package_page_correction_is_checked_against_the_version_the_page_shows(
    correctable, monkeypatch
):
    """A package page reports its PACKAGE version as ``instance_version``, and that is
    the number the person typed against. The record's own counter drifts from it."""
    from kiro_crew import dashboard_package

    monkeypatch.setattr(
        dashboard_package,
        "read_package_model",
        lambda slug: SimpleNamespace(
            state="live",
            state_reason="",
            from_package=True,
            slug="report",
            model=SimpleNamespace(
                instance_version=1,
                manifest=SimpleNamespace(id="dashboard-package", version=3),
            ),
        ),
    )
    page = {"page": "package:report@1"}
    status, _ = await _call(
        "POST", _CORRECT, json={"field": "verdict", "value": "green", "page_version": 3, **page}
    )
    assert status == 200
    status, body = await _call(
        "POST", _CORRECT, json={"field": "verdict", "value": "green", "page_version": 2, **page}
    )
    assert (status, body["code"]) == (409, "stale_page")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "in_force,typed_on",
    [
        # The first package apply over a template page: both versions count from 1.
        ("package", {"page": "template:project-report", "page_version": 1}),
        ("template", {"page": "package:report@1", "page_version": 1}),
        # Another package at the same version.
        ("package", {"page": "package:other@2", "page_version": 1}),
        # The same slug deleted and saved again: version 1 again, but a later apply.
        ("package", {"page": "package:report@1", "page_version": 1}),
        # Another template at the same version.
        ("template", {"page": "template:other", "page_version": 1}),
    ],
)
async def test_a_correction_typed_on_another_page_at_the_same_version_is_refused(
    correctable, monkeypatch, in_force, typed_on
):
    """The version alone does not name a page: a template's instance counter and a
    package's version both start at 1. The correction names the page it was typed on,
    and the page in force must be that one."""
    from kiro_crew import dashboard_package

    if in_force == "package":
        read = SimpleNamespace(
            state="live",
            state_reason="",
            from_package=True,
            slug="report",
            # The record's apply counter: 2 applies in, after a delete and save again.
            model=SimpleNamespace(
                instance_version=2, manifest=SimpleNamespace(id="dashboard-package", version=1)
            ),
        )
    else:
        read = SimpleNamespace(
            state="live",
            state_reason="",
            from_package=False,
            model=SimpleNamespace(
                instance_version=1, manifest=SimpleNamespace(id="project-report")
            ),
        )
    monkeypatch.setattr(dashboard_package, "read_package_model", lambda slug: read)
    status, body = await _call(
        "POST", _CORRECT, json={"field": "verdict", "value": "green", **typed_on}
    )
    assert (status, body["code"]) == (409, "stale_page")
    assert correctable == []


@pytest.mark.asyncio
async def test_a_recreated_package_at_the_same_slug_and_version_is_another_page(
    correctable, monkeypatch
):
    """Deleting a package and saving it again reuses its slug and starts at version 1.
    The record's apply counter only goes up, so the page name carries it, and a draft
    typed on the old page is refused on the new one."""
    from kiro_crew import dashboard_package

    def read(counter):
        return SimpleNamespace(
            state="live",
            state_reason="",
            from_package=True,
            slug="report",
            model=SimpleNamespace(
                instance_version=counter,
                manifest=SimpleNamespace(id="dashboard-package", version=1),
            ),
        )

    monkeypatch.setattr(dashboard_package, "read_package_model", lambda slug: read(5))
    old = {"page": "package:report@3", "page_version": 1}
    status, body = await _call("POST", _CORRECT, json={"field": "verdict", "value": "x", **old})
    assert (status, body["code"]) == (409, "stale_page") and correctable == []
    new = {"page": "package:report@5", "page_version": 1}
    status, _ = await _call("POST", _CORRECT, json={"field": "verdict", "value": "x", **new})
    assert status == 200


def test_a_package_read_names_the_package_it_read():
    """The correction compares the page it names with the package in force, so the
    read carries the package's slug: its manifest id is the same for every package."""
    from kiro_crew import dashboard_package as reader

    got = reader._package_model("atlas", "report", 3, _package(BLOCKS), 5)
    assert got.from_package is True and got.slug == "report"


@pytest.mark.asyncio
async def test_a_correction_typed_against_an_older_page_version_is_refused(
    correctable, monkeypatch
):
    """The person typed against the page they saw. If the page in force is a newer
    version, the field may mean something else now: refused, and nothing written."""
    from kiro_crew import dashboard_package

    current = 7
    monkeypatch.setattr(
        dashboard_package,
        "read_package_model",
        lambda slug: SimpleNamespace(
            state="live",
            state_reason="",
            model=SimpleNamespace(
                instance_version=current, manifest=SimpleNamespace(id="project-report")
            ),
        ),
    )
    page = {"page": "template:project-report"}
    status, body = await _call(
        "POST",
        _CORRECT,
        json={"field": "verdict", "value": "green", "page_version": current - 1, **page},
    )
    assert (status, body["code"]) == (409, "stale_page")
    assert correctable == []
    status, _ = await _call(
        "POST",
        _CORRECT,
        json={"field": "verdict", "value": "green", "page_version": current, **page},
    )
    assert status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,status,code",
    [
        ({"data": "{", "headers": {"Content-Type": "application/json"}}, 400, "invalid_json"),
        ({"json": ["verdict"]}, 400, "invalid_body"),
        ({"json": {"field": "verdict", **_PAGE}}, 400, "invalid_body"),
        # A correction that does not name the page it was typed on is refused, not
        # written into whatever page is in force.
        ({"json": {"field": "verdict", "value": "x"}}, 400, "invalid_body"),
        ({"json": {"field": "verdict", "value": "x", "page_version": 4}}, 400, "invalid_body"),
        (
            {"json": {"field": "verdict", "value": "x", "page": "template:project-report"}},
            400,
            "invalid_body",
        ),
        (
            {"json": {"field": "verdict", "value": "x", "page": "", "page_version": 4}},
            400,
            "invalid_body",
        ),
        (
            {"json": {"field": "verdict", "value": "x", "page": "p", "page_version": True}},
            400,
            "invalid_body",
        ),
        (
            {"json": {"field": "verdict", "value": "x", "reason": "r" * 241, **_PAGE}},
            400,
            "invalid_reason",
        ),
        (
            {"json": {"field": "verdict", "value": "x", "reason": "a\nb", **_PAGE}},
            400,
            "invalid_reason",
        ),
    ],
)
async def test_a_correction_body_that_is_not_one_field_and_one_line_is_refused(
    correctable, kwargs, status, code
):
    got_status, body = await _call("POST", _CORRECT, **kwargs)
    assert (got_status, body["code"]) == (status, code)
    assert correctable == []


@pytest.mark.asyncio
async def test_a_reason_is_measured_as_it_is_stored(correctable, monkeypatch):
    """The fold keeps at most DASHBOARD_REASON_LIMIT characters of the REDACTED reason,
    so the route measures that, not the raw text, and nothing is cut silently."""
    from kiro_crew.crew_log.entry_types import DASHBOARD_REASON_LIMIT
    from kiro_crew.platform import context as platform_context

    monkeypatch.setattr(platform_context, "redact_via_context", lambda text: text + "[REDACTED]")
    reason = "r" * (DASHBOARD_REASON_LIMIT - 5)
    status, body = await _call(
        "POST", _CORRECT, json={"field": "verdict", "value": "x", "reason": reason, **_PAGE}
    )
    assert (status, body["code"]) == (400, "invalid_reason") and correctable == []


@pytest.mark.asyncio
async def test_a_bot_cannot_make_a_correction(correctable, monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard.handlers import member_dashboard as md

    monkeypatch.setattr(
        md,
        "_refuse_agent_caller",
        lambda request, op, message: web.json_response({"code": "agent_caller"}, status=403),
    )
    status, _body = await _call("POST", _CORRECT, json={"field": "verdict", "value": "x", **_PAGE})
    assert status == 403 and correctable == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "refused", "not_scalar", "log_off", "append"])
async def test_a_correction_the_page_or_the_log_cannot_take_is_refused(
    correctable, monkeypatch, failure
):
    from kiro_crew import dashboard_agentic, dashboard_package
    from kiro_crew.crew_log import emit as crew_log_emit

    if failure == "unreadable":

        def unreadable(slug):
            raise dashboard_package.PackageReadError("gone")

        monkeypatch.setattr(dashboard_package, "read_package_model", unreadable)
    elif failure == "refused":

        def refused(model, field, value):
            raise dashboard_agentic.WriteRefused("type_mismatch", field, "not a string")

        monkeypatch.setattr(dashboard_agentic, "check_write", refused)
    elif failure == "not_scalar":
        monkeypatch.setattr(
            dashboard_agentic, "check_write", lambda m, f, v: {"field": f, "type": "array"}
        )
    elif failure == "log_off":
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
    else:
        monkeypatch.setattr(member_dashboard_lineage, "_append_correction", lambda *a: False)
    status, body = await _call("POST", _CORRECT, json={"field": "verdict", "value": "x", **_PAGE})
    expected = {
        "unreadable": (503, "package_unreadable"),
        "refused": (400, "type_mismatch"),
        "not_scalar": (400, "not_scalar"),
        "log_off": (503, "crew_log_off"),
        "append": (503, "append_failed"),
    }[failure]
    assert (status, body["code"]) == expected
    if failure != "append":
        assert correctable == []


def test_a_correction_needs_the_bots_session_and_an_entry_that_fits(monkeypatch):
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard.handlers import member_dashboard as md

    append = member_dashboard_lineage._append_correction
    monkeypatch.setattr(md, "_write_session", lambda slug, member: "")
    assert append("atlas", "Atlas", {}) is False
    monkeypatch.setattr(md, "_write_session", lambda slug, member: "unit")
    monkeypatch.setattr(crew_log_emit, "dashboard_entry_fits", lambda kind, entry: False)
    assert append("atlas", "Atlas", {}) is False
    monkeypatch.setattr(crew_log_emit, "dashboard_entry_fits", lambda kind, entry: True)
    monkeypatch.setattr(crew_log_emit, "on_dashboard_agentic", lambda unit, entry, timeout: True)
    assert append("atlas", "Atlas", {}) is True


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
        lambda slot, name, **_: SimpleNamespace(value={"fields": rows}),
    )
    line = dashboard_agentic.person_corrections_line("atlas", "Atlas", ["verdict", "other"])
    assert 'verdict = "green" at T ("fixed")' in line and "other" not in line


def test_a_warm_turn_tells_the_bot_about_a_persons_correction(preview_on, monkeypatch):
    """A Bot with no member_id gets its member section only at session start, so the
    warm turn's own notice must carry the correction, or the next turn can write over
    the person's value without being told. Reddens if `unresolved_notice` drops
    `person_corrections_line`."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard
    from kiro_crew.dashboard_templates.manifest import parse_manifest

    manifest = parse_manifest(
        {
            "id": "warm-probe",
            "version": 1,
            "title": "Warm probe",
            "description": "a page for this case",
            "source": "user",
            "fields": {"verdict": {"type": "string", "source": {"agentic": True}}},
        }
    )
    rows = {"verdict": {"value": "green", "at": "T", "actor": "person", "reason": "fixed"}}
    monkeypatch.setattr(dashboard_agentic, "_read_page", lambda slug: ({}, manifest))
    monkeypatch.setattr(dashboard_agentic, "_crew_panel_on", lambda: True)
    monkeypatch.setattr(dashboard_agentic, "_unresolved_fold_fields", lambda *a: {})
    monkeypatch.setattr(dashboard_agentic, "stale_fields_enabled", lambda: False)
    monkeypatch.setattr(member_dashboard, "_dashboard_slot", lambda member, slug: "slot")
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_: SimpleNamespace(value={"fields": rows}),
    )
    notice = dashboard_agentic.unresolved_notice("atlas", "Atlas")
    assert notice.startswith("[DASHBOARD]\nThe person corrected these values"), notice
    assert 'verdict = "green" at T ("fixed")' in notice


def test_off_the_bot_is_told_nothing(preview_off):
    from kiro_crew import dashboard_agentic

    assert dashboard_agentic.person_corrections_line("atlas", "Atlas", ["verdict"]) == ""
