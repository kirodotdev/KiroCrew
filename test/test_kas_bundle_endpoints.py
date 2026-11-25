"""Bundle launch destinations are validated before KAS receives a bearer token."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kiro_crew.acp import kas_bundle
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.kas_transport import build_kas_bundle_argv
from kiro_crew.agent_sdk.kas_bundle import bundle_spawn_argv


@pytest.fixture
def bundle(tmp_path: Path) -> kas_bundle.InstalledKasBundle:
    return kas_bundle.InstalledKasBundle(
        version="0.66.117",
        node_version="22.23.4",
        root=tmp_path / "kas",
        node_bin=tmp_path / "kas" / "node" / "bin" / "node",
        acp_server=tmp_path / "kas" / "acp-server.js",
    )


@pytest.mark.parametrize(
    ("portal", "expected"),
    [
        (None, "https://app.kiro.dev"),
        ("", "https://app.kiro.dev"),
        ("https://app.kiro.dev", "https://app.kiro.dev"),
        ("https://gamma.app.kiro.dev", "https://gamma.app.kiro.dev"),
        ("  https://beta.app.kiro.dev/  ", "https://beta.app.kiro.dev"),
        ("https://gamma.app.kiro.dev:443", "https://app.kiro.dev"),
        ("https://portal.example.test", "https://app.kiro.dev"),
        ("https://gamma.app.kiro.dev.example.test", "https://app.kiro.dev"),
    ],
)
def test_stage_defaults_reach_the_shared_builder_and_diagnostics(
    portal, expected, bundle, monkeypatch
):
    monkeypatch.delenv("CLOUD_CONFIG_ENDPOINT", raising=False)
    monkeypatch.delenv("KIRO_AUTH_PORTAL_URL", raising=False)
    environ = {}
    if portal is not None:
        environ["KIRO_AUTH_PORTAL_URL"] = portal
        monkeypatch.setenv("KIRO_AUTH_PORTAL_URL", portal)
    argv = build_kas_bundle_argv(str(bundle.node_bin), str(bundle.acp_server), environ=environ)
    assert argv[-1] == f"--cloud-config-endpoint={expected}"
    assert bundle_spawn_argv(bundle) == argv


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://app.kiro.dev", "https://app.kiro.dev"),
        ("https://gamma.app.kiro.dev/config", "https://gamma.app.kiro.dev/config"),
        (
            "https://beta.app.kiro.dev:443/config?fixture=1",
            "https://beta.app.kiro.dev:443/config?fixture=1",
        ),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://127.0.0.1:8080/config", "http://127.0.0.1:8080/config"),
        ("https://[::1]:8080", "https://[::1]:8080"),
        ("", "https://gamma.app.kiro.dev"),
        ("https://untrusted.example.test", "https://gamma.app.kiro.dev"),
    ],
)
@pytest.mark.asyncio
async def test_endpoint_choice_reaches_runtime_and_diagnostic_argv(
    endpoint, expected, bundle, tmp_path, monkeypatch
):
    monkeypatch.setenv("CLOUD_CONFIG_ENDPOINT", f"  {endpoint}  ")
    monkeypatch.setenv("KIRO_AUTH_PORTAL_URL", "https://gamma.app.kiro.dev")
    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: True)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: bundle)
    monkeypatch.setattr(kas_harness, "ensure_engine_bundle_cache", lambda argv: None)
    ctx = SpawnContext(
        agent="fixture",
        work_dir=tmp_path,
        model=None,
        home=tmp_path,
        environ={
            "CLOUD_CONFIG_ENDPOINT": endpoint,
            "KIRO_AUTH_PORTAL_URL": "https://gamma.app.kiro.dev",
        },
    )
    diagnostic_argv = bundle_spawn_argv(bundle)
    monkeypatch.setenv("CLOUD_CONFIG_ENDPOINT", "http://localhost:9090")
    plan = await kas_harness.KasHarness().resolve_spawn(ctx)
    assert plan.argv[-1] == f"--cloud-config-endpoint={expected}"
    assert plan.argv == diagnostic_argv
    assert plan.host_auth is True
    assert plan.engine_bundle is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://untrusted.example.test/private?signature=fixture-secret",
        "http://app.kiro.dev",
        "https://app.kiro.dev.example.test",
        "http://localhost.example.test",
        "http://192.0.2.1",
        "http://127.0.0.2",
        "ftp://localhost",
        "https://fixture-user:fixture-secret@app.kiro.dev",
        "https://app.kiro.dev:invalid",
        "https://app.kiro.dev:65536",
        "http://[::1",
        "not a URL",
        "https://app.kiro.dev\\@untrusted.example.test",
        "https://app.kiro.dev\n/untrusted",
    ],
)
def test_invalid_override_falls_back_without_logging_its_value(endpoint, caplog):
    with caplog.at_level(logging.WARNING):
        argv = build_kas_bundle_argv(
            "node",
            "acp-server.js",
            environ={
                "CLOUD_CONFIG_ENDPOINT": endpoint,
                "KIRO_AUTH_PORTAL_URL": "https://beta.app.kiro.dev",
            },
        )
    assert argv[-1] == "--cloud-config-endpoint=https://beta.app.kiro.dev"
    assert "Ignoring CLOUD_CONFIG_ENDPOINT" in caplog.text
    assert endpoint not in caplog.text
    assert "fixture-secret" not in caplog.text


@pytest.mark.parametrize("endpoint", ["", " \t "])
def test_blank_override_keeps_the_stage_default(endpoint, caplog):
    argv = build_kas_bundle_argv(
        "node",
        "acp-server.js",
        environ={
            "CLOUD_CONFIG_ENDPOINT": endpoint,
            "KIRO_AUTH_PORTAL_URL": "https://gamma.app.kiro.dev",
        },
    )
    assert argv[-1] == "--cloud-config-endpoint=https://gamma.app.kiro.dev"
    assert not caplog.records


def test_bundle_environment_drops_raw_endpoint(bundle, monkeypatch):
    monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda home=None: bundle.root)
    monkeypatch.setattr(kas_bundle, "bundled_kas_root", lambda: None)
    env = {"CLOUD_CONFIG_ENDPOINT": "https://untrusted.example.test"}
    kas_harness.KasHarness().apply_spawn_env(env, spawned_binary=str(bundle.node_bin))
    assert "CLOUD_CONFIG_ENDPOINT" not in env
