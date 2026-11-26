"""App ids from registries an edition names, from install record to destination batch.

Install records are written by the real install path, the public ids are read by
the real scan, ``kirocrew.app.present`` comes from the daily pass and
``kirocrew.app.opened`` from the real ``/api/telemetry/app-view`` handler. Both go
through a real ``MetricsRecorder`` on a real ``MeterProvider`` with the local JSONL
sink, a destination that narrows nothing, and one built by ``wrap_if_narrowed``.
What is asserted is the ``Entity`` each exporter was handed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload
from multidict import CIMultiDict
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExportResult, PeriodicExportingMetricReader

from kiro_crew import beacon
from kiro_crew.apps import manager
from kiro_crew.dashboard.handlers import telemetry_app_view
from kiro_crew.metrics import app_present
from kiro_crew.metrics import destination_filter as df
from kiro_crew.metrics import feature_usage as fu
from kiro_crew.metrics.behavioral_metrics import BEHAVIORAL_SCOPE
from kiro_crew.metrics.local_exporter import JsonlMetricExporter
from kiro_crew.metrics.recorder import MetricsRecorder
from kiro_crew.metrics.temporality import delta_preference
from kiro_crew.platform.interfaces import OtlpDestination

APP_URL = "https://forge.example.com/org/an-app.git"
REGISTRY_REPO = "https://forge.example.com/org/app-registry.git"

#: name -> the registry its record names (``""`` the store, ``None`` a local tree).
APPS = {
    "weather-app": "",
    "acme-tool": "acme-index",
    "beta-tool": "beta-index",
    "long-tool": "long-index",
    "local-tool": None,
}

EDITION_ROWS = [
    {"name": "acme-index", "repo": REGISTRY_REPO, "trust": "owner", "appNamespace": "acme"},
    # Declared, but "<60 x>.long-tool" is longer than the store accepts.
    {"name": "long-index", "repo": REGISTRY_REPO, "trust": "owner", "appNamespace": "x" * 60},
]


class _Recording:
    def __init__(self) -> None:
        self._preferred_temporality = delta_preference()
        self._preferred_aggregation = None
        self.batches: list = []

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs):
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        return None

    def entities(self, name: str) -> "dict[str, float]":
        out: "dict[str, float]" = {}
        for batch in self.batches:
            for rm in batch.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        if m.name != name:
                            continue
                        for p in m.data.data_points:
                            key = str(dict(p.attributes)["Entity"])
                            out[key] = out.get(key, 0) + p.value
        return out


def _edition(rows) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.defaults import DefaultAppsLoader

    class _Loader(DefaultAppsLoader):
        def default_registries(self):
            return rows

    base = build_default_context(KiroCrewConfig())
    ctx_mod.set_context(dataclasses.replace(base, apps_loader=_Loader()))
    from kiro_crew.apps import registry
    from kiro_crew.apps.registry_pipeline.caches import (
        _external_registry_cache_identity,
        _write_external_registry_cache,
    )

    # Each named registry's index lists our apps at the clone target they record.
    for reg in registry._pinned_registries():
        _write_external_registry_cache(
            _external_registry_cache_identity(reg),
            [{"name": name, "gitUrl": APP_URL} for name in APPS],
        )


def _install_all(tmp_path: Path) -> None:
    from kiro_crew.apps.manager_install import registry_source_repository

    for name, registry_id in APPS.items():
        src = tmp_path / "src" / name
        src.mkdir(parents=True)
        manifest = {"name": name, "version": "1.0.0", "displayName": name, "author": "t"}
        manifest["description"] = "registry id fixture"
        (src / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
        if registry_id is None:
            assert manager.install_app(src).ok
            continue
        with registry_source_repository(APP_URL):
            assert manager.install_app(src).ok
        assert manager.set_app_provenance(
            name, source=f"registry:{name}", url=APP_URL, registry=registry_id
        )


def _open(app: str) -> None:
    raw = json.dumps({"app": app, "surface": "page", "event": "opened"}).encode()
    request = make_mocked_request(
        "POST",
        "/api/telemetry/app-view",
        headers=CIMultiDict({"Content-Length": str(len(raw)), "Content-Type": "application/json"}),
        payload=BodyStreamPayload(raw),
    )
    with (
        patch.object(
            telemetry_app_view, "require_owner_dashboard_request", AsyncMock(return_value=None)
        ),
        patch(
            "kiro_crew.dashboard.handlers.source_providers.is_host_owner_dashboard_request",
            return_value=True,
        ),
    ):
        assert asyncio.run(telemetry_app_view.api_app_view(request)).status == 204


def _run(tmp_path, monkeypatch, *, rows, namespaces):
    """Install, scan, record a present pass and one open per app, export once.

    Returns the operator's and the product destination's ``Entity`` -> summed value
    per metric, the local sink's for ``app.opened``, the product descriptor and its
    raw exporter. Only our apps are kept; the shipped built-ins are dropped.
    """
    from kiro_crew.apps.discovery import discover_builtin_apps

    if rows is not None:
        _edition(rows)
    _install_all(tmp_path)

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    operator, product, silent = _Recording(), _Recording(), _Recording()
    dest = OtlpDestination(
        "product",
        "https://p.example/v1/metrics",
        frozenset({"metrics"}),
        named_entity_namespaces=namespaces,
    )
    # A destination that says nothing about namespaces.
    nothing = OtlpDestination("silent", "https://s.example/v1/metrics", frozenset({"metrics"}))
    readers = [
        PeriodicExportingMetricReader(e, export_interval_millis=math.inf)
        for e in (
            JsonlMetricExporter(local_dir),
            operator,
            df.wrap_if_narrowed(dest, product),
            df.wrap_if_narrowed(nothing, silent),
        )
    ]
    provider = MeterProvider(metric_readers=readers)
    recorder = MetricsRecorder(
        provider.get_meter("kiro_crew"), provider.get_meter(BEHAVIORAL_SCOPE)
    )
    monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
    monkeypatch.setattr(fu, "_public_app_ids", {})
    monkeypatch.setattr(beacon, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(beacon, "_today", lambda: "2026-11-25")
    monkeypatch.setattr("kiro_crew.apps.backend.list_app_processes", lambda: [])
    try:
        assert app_present.record_today() >= len(APPS)
        for name in APPS:
            _open(name)
        provider.force_flush()
    finally:
        provider.shutdown()

    builtins = {a["name"] for a in discover_builtin_apps()}

    def ours(found: "dict[str, float]") -> "dict[str, float]":
        return {k: v for k, v in found.items() if k not in builtins}

    local: "dict[str, float]" = {}
    for shard in local_dir.glob("metrics-*.jsonl"):
        for line in shard.read_text(encoding="utf-8").splitlines():
            for rm in json.loads(line).get("resource_metrics") or []:
                for sm in rm.get("scope_metrics") or []:
                    for m in sm.get("metrics") or []:
                        if m["name"] == fu.APP_OPENED:
                            for p in m["data"]["data_points"]:
                                key = p["attributes"]["Entity"]
                                local[key] = local.get(key, 0) + p["value"]
    return (  # operator, product, local, dest, product raw, silent, silent raw
        {n: ours(operator.entities(n)) for n in (fu.APP_PRESENT, fu.APP_OPENED)},
        {n: ours(product.entities(n)) for n in (fu.APP_PRESENT, fu.APP_OPENED)},
        ours(local),
        dest,
        product,
        {n: ours(silent.entities(n)) for n in (fu.APP_PRESENT, fu.APP_OPENED)},
        silent,
    )


def test_with_no_registry_declared_every_destination_gets_todays_values(tmp_path, monkeypatch):
    operator, product, local, dest, _, silent, _ = _run(
        tmp_path, monkeypatch, rows=None, namespaces=frozenset()
    )
    today = {"weather-app": 1, "private": 4}
    for metric in (fu.APP_PRESENT, fu.APP_OPENED):
        assert operator[metric] == today, metric
        assert product[metric] == today, metric
        assert silent[metric] == today, metric
    assert local == today
    assert df.narrows_export(dest) is False


def test_a_declared_namespace_is_hidden_from_a_destination_that_does_not_list_it(
    tmp_path, monkeypatch
):
    operator, product, local, _, _, silent, raw = _run(
        tmp_path, monkeypatch, rows=EDITION_ROWS, namespaces=frozenset({"acme"})
    )
    named = {"weather-app": 1, "acme.acme-tool": 1, "private": 3}
    for metric in (fu.APP_PRESENT, fu.APP_OPENED):
        # The undeclared registry, the over-long id and the local tree stay private.
        assert operator[metric] == named, metric
        # The destination that lists the namespace sees the name.
        assert product[metric] == named, metric
        # A destination that says nothing is sent private: the core test that fails
        # if a namespace is declared and a destination without the field gets a name.
        assert silent[metric] == {"weather-app": 1, "private": 4}, metric
    assert local == named
    opened = [
        dict(p.attributes)
        for b in raw.batches
        for rm in b.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == fu.APP_OPENED
        for p in m.data.data_points
        if dict(p.attributes)["Entity"] == "private"
    ]
    assert len(opened) == 1, "the hidden app reached the destination as a second series"
    assert opened[0]["entity_version"] == "private"
