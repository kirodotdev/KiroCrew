"""The exported metric schema is pinned by ``schema_manifest.json`` and only grows.

``src/kiro_crew/metrics/schema_manifest.json`` lists every metric in
``PRODUCT_METRICS``: kind, unit, instrumentation scope, temporality, attribute
keys and, where the code closes them, the attribute values. A downstream reader
builds queries on those names, so a rename or a removal breaks it silently.

Two checks keep that from happening:

* ``test_manifest_matches_the_code`` -- the schema read from the code (see
  ``metrics_schema_helpers``) equals the manifest. A metric, key or value the code
  adds without a manifest row fails, so every addition is an explicit, reviewed
  edit. A metric, key or value the code drops fails until the row is marked
  ``deprecated``.
* ``test_manifest_only_grows_against_its_base`` -- the manifest against its copy
  at the base commit. A regenerated manifest that drops or renames a row, or
  changes a row's kind, unit, scope or temporality, fails here even though the
  first check passes.

The rules both checks apply are unit-tested below on small fixture documents,
so a check that passes is one that can be seen to fail.
"""

from __future__ import annotations

import ast
import copy
import inspect
from typing import Any, Dict

import metrics_schema_helpers as schema
import pytest

from kiro_crew.metrics import feature_usage, provider
from kiro_crew.metrics.behavioral_metrics import BEHAVIORAL_SCOPE
from kiro_crew.metrics.product_metrics import PRODUCT_METRICS


@pytest.fixture(scope="module")
def code_rows() -> Dict[str, Dict[str, Any]]:
    return schema.build_rows()


@pytest.fixture(scope="module")
def manifest() -> Dict[str, Any]:
    return schema.load_manifest()


def _fail(problems: list) -> str:
    return "metric schema check failed:\n  " + "\n  ".join(problems)


# -- the real checks ------------------------------------------------------------


def test_manifest_matches_the_code(manifest, code_rows):
    problems = schema.check_code(manifest, code_rows)
    assert not problems, _fail(problems)


def test_manifest_has_one_active_row_per_product_metric(manifest):
    active = {r["name"] for r in manifest["metrics"] if r.get("status") != "deprecated"}
    assert active == set(PRODUCT_METRICS), _fail(
        [f"{n}: in PRODUCT_METRICS, no active row" for n in sorted(set(PRODUCT_METRICS) - active)]
        + [
            f"{n}: active row, not in PRODUCT_METRICS"
            for n in sorted(active - set(PRODUCT_METRICS))
        ]
    )


def test_manifest_file_is_what_the_generator_writes(manifest, code_rows):
    """The file is generated, not typed: a hand edit that the generator would undo fails."""
    expected = schema.dump(schema.build_manifest(manifest, code_rows))
    actual = schema.MANIFEST_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "schema_manifest.json differs from what the generator writes; "
        "run: python test/metrics_schema_helpers.py --write"
    )


def test_manifest_only_grows_against_its_base(manifest):
    base, where = schema.base_manifest()
    if base is None:
        pytest.skip(f"no base manifest to compare with: {where}")
    problems = schema.is_additive(base, manifest)
    assert not problems, _fail([f"against {where}: {p}" for p in problems])


def test_counters_land_on_the_scope_and_temporality_in_the_manifest(manifest):
    """Record one point per counter row through the real recorder and read it back."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from kiro_crew.metrics.recorder import MetricsRecorder
    from kiro_crew.metrics.temporality import delta_preference

    reader = InMemoryMetricReader(preferred_temporality=delta_preference())
    meter_provider = MeterProvider(metric_readers=[reader])
    recorder = MetricsRecorder(
        meter_provider.get_meter(provider._SCOPE), meter_provider.get_meter(BEHAVIORAL_SCOPE)
    )
    counters = [
        r for r in manifest["metrics"] if r["kind"] == "counter" and r.get("status") != "deprecated"
    ]
    for row in counters:
        recorder.counter(row["name"], unit=row["unit"])
    seen: Dict[str, tuple] = {}
    data = reader.get_metrics_data()
    meter_provider.shutdown()
    for rm in data.resource_metrics if data else ():
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                temporality = metric.data.aggregation_temporality.name.lower()
                seen[metric.name] = (sm.scope.name, metric.unit, temporality)
    problems = []
    for row in counters:
        want = (row["scope"], row["unit"], row["temporality"])
        got = seen.get(row["name"])
        if got != want:
            problems.append(f"{row['name']}: recorded as {got}, manifest says {want}")
    assert not problems, _fail(problems)


def test_emit_wrappers_pass_no_unit():
    """The generator gives a wrapper's metrics the recorder's default unit; pin that."""
    for module_name, func_name in schema.UNITLESS_WRAPPERS:
        func = getattr(schema._module(module_name), func_name)
        tree = ast.parse(inspect.getsource(func).lstrip())
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "counter"
        ]
        assert calls, f"{module_name}.{func_name} no longer calls .counter()"
        for call in calls:
            assert not any(k.arg == "unit" for k in call.keywords), (
                f"{module_name}.{func_name} now passes unit=; "
                "teach metrics_schema_helpers to read it"
            )


# -- the checks can see a failure ----------------------------------------------


def test_dropping_a_closed_value_in_code_fails_naming_the_metric(manifest, monkeypatch):
    name = feature_usage.TIPS_FEEDBACK
    narrowed = tuple(v for v in feature_usage.TIP_SOURCE_VALUES if v != "catalog")
    monkeypatch.setattr(feature_usage, "TIP_SOURCE_VALUES", narrowed)
    rows = schema.build_rows([name])
    only = {"metrics": [r for r in manifest["metrics"] if r["name"] == name]}
    problems = schema.check_code(only, rows)
    assert any(name in p and "'source'" in p and '"catalog"' in p for p in problems), problems


def test_renaming_a_metric_in_code_fails_both_ways(manifest, code_rows):
    rows = copy.deepcopy(code_rows)
    old = "kirocrew.turn.completed"
    rows["kirocrew.turn.finished"] = dict(rows.pop(old), name="kirocrew.turn.finished")
    problems = schema.check_code(manifest, rows)
    assert any(p.startswith("kirocrew.turn.finished: exported by the code") for p in problems)
    assert any(p.startswith(f"{old}: has an active manifest row") for p in problems)


# -- the rules, on fixture documents ---------------------------------------------


def _doc(**overrides: Any) -> Dict[str, Any]:
    row = {
        "name": "kirocrew.x.done",
        "kind": "counter",
        "unit": "1",
        "scope": "kiro_crew",
        "temporality": "delta",
        "status": "active",
        "attributes": {"outcome": {"values": ["ok", "error"]}, "agent": {}},
    }
    row.update(overrides)
    return {"metrics": [row]}


def _row(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc["metrics"][0]


def test_is_additive_accepts_the_same_document():
    assert schema.is_additive(_doc(), _doc()) == []


@pytest.mark.parametrize(
    "change",
    [
        "remove_row",
        "rename_row",
        "kind",
        "unit",
        "scope",
        "temporality",
        "remove_key",
        "remove_value",
        "open_a_closed_key",
        "reactivate_row",
        "drop_deprecated_value",
    ],
)
def test_is_additive_rejects(change):
    old, new = _doc(), _doc()
    row = _row(new)
    if change == "remove_row":
        new["metrics"] = []
    elif change == "rename_row":
        row["name"] = "kirocrew.x.finished"
    elif change in schema.FIXED_FIELDS:
        row[change] = {"kind": "observable_gauge", "unit": "ms", "scope": "kiro.crew"}.get(
            change, "cumulative"
        )
    elif change == "remove_key":
        del row["attributes"]["agent"]
    elif change == "remove_value":
        row["attributes"]["outcome"]["values"] = ["ok"]
    elif change == "open_a_closed_key":
        row["attributes"]["outcome"] = {}
    elif change == "reactivate_row":
        _row(old)["status"] = "deprecated"
    elif change == "drop_deprecated_value":
        _row(old)["attributes"]["outcome"]["deprecated_values"] = ["timeout"]
    problems = schema.is_additive(old, new)
    assert problems and all("kirocrew.x." in p for p in problems), (change, problems)


@pytest.mark.parametrize(
    "change",
    ["add_row", "add_key", "add_value", "deprecate_row", "deprecate_key", "deprecate_value"],
)
def test_is_additive_accepts(change):
    old, new = _doc(), _doc()
    row = _row(new)
    if change == "add_row":
        new["metrics"].append(dict(copy.deepcopy(row), name="kirocrew.x.started"))
    elif change == "add_key":
        row["attributes"]["surface"] = {"values": ["page"]}
    elif change == "add_value":
        row["attributes"]["outcome"]["values"].append("timeout")
    elif change == "deprecate_row":
        row["status"] = "deprecated"
    elif change == "deprecate_key":
        row["attributes"]["outcome"]["status"] = "deprecated"
    elif change == "deprecate_value":
        row["attributes"]["outcome"] = {"values": ["ok"], "deprecated_values": ["error"]}
    assert schema.is_additive(old, new) == [], change


def _code(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {r["name"]: r for r in copy.deepcopy(doc)["metrics"]}


@pytest.mark.parametrize(
    "edit, expect",
    [
        (lambda r: r["attributes"].update(surface={}), "attribute 'surface' is sent by the code"),
        (lambda r: r["attributes"]["outcome"]["values"].append("timeout"), '"timeout" is sent'),
        (lambda r: r["attributes"]["outcome"].update(values=["ok"]), '"error" is listed'),
        (lambda r: r["attributes"].pop("agent"), "'agent' is in the manifest"),
        (lambda r: r.update(unit="ms"), "unit is 'ms' in the code"),
    ],
)
def test_check_code_names_the_metric_and_the_change(edit, expect):
    doc = _doc()
    code = _code(doc)
    edit(code["kirocrew.x.done"])
    problems = schema.check_code(doc, code)
    assert any(p.startswith("kirocrew.x.done:") and expect in p for p in problems), problems


def test_check_code_rejects_a_reused_deprecated_value():
    doc = _doc()
    _row(doc)["attributes"]["outcome"] = {"values": ["ok"], "deprecated_values": ["error"]}
    code = _code(_doc())
    problems = schema.check_code(doc, code)
    assert any('"error" is deprecated' in p for p in problems), problems


def test_regenerating_keeps_deprecated_rows_keys_and_values():
    previous = _doc()
    _row(previous)["attributes"]["outcome"]["deprecated_values"] = ["timeout"]
    _row(previous)["attributes"]["legacy"] = {"values": ["a"], "status": "deprecated"}
    gone = dict(copy.deepcopy(_row(_doc())), name="kirocrew.x.old", status="deprecated")
    previous["metrics"].append(gone)
    code = _code(_doc())
    regenerated = schema.build_manifest(previous, code)
    assert schema.is_additive(previous, regenerated) == []
    by_name = {r["name"]: r for r in regenerated["metrics"]}
    assert by_name["kirocrew.x.old"]["status"] == "deprecated"
    outcome = by_name["kirocrew.x.done"]["attributes"]["outcome"]
    assert outcome == {"values": ["ok", "error"], "deprecated_values": ["timeout"]}
    assert by_name["kirocrew.x.done"]["attributes"]["legacy"] == {
        "values": ["a"],
        "status": "deprecated",
    }
