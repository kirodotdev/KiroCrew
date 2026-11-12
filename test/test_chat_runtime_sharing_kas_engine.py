"""``kas_chat_sharing_ready``: the engine a KAS route WILL run, against the floor.

Engines before 0.66.104 (kiro-agent#3741) queue the custom-agent turns of every
session on one process behind a single model slot, so chats sharing such a
process would wait for each other. Each test below reddens when the gate it
names is removed:

* bundle below the floor refused -- drop the version compare on the bundle arm;
* bundle at the floor allowed -- go back to refusing the bundle route outright;
* relay with a queueing engine refused -- go back to "kiro-cli >= 3.1.0 is enough";
* unknown engine refused -- treat a missing extract or record as ready.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.acp.chat_runtime_sharing import (
    KAS_CHAT_SHARING_MIN_ENGINE,
    engine_shares_chats,
    kas_chat_sharing_ready,
)


@pytest.fixture
def route(monkeypatch):
    import kiro_crew.acp.kas_route as kas_route

    def set_route(*, bundle: bool) -> None:
        monkeypatch.setattr(
            kas_route,
            "resolve_kas_route",
            lambda **_: kas_route.KasRoute(bundle=bundle, reason="configured" if bundle else ""),
        )

    return set_route


@pytest.fixture
def bundle_engine(monkeypatch):
    import kiro_crew.acp.kas_bundle as kas_bundle

    def set_engine(version: str | None) -> None:
        installed = None if version is None else SimpleNamespace(version=version)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)

    return set_engine


@pytest.fixture
def relay(monkeypatch, tmp_path):
    """kiro-cli at a version, with its data dir (and ``kas/`` extracts) under *tmp_path*."""
    import kiro_crew.identity_stores as identity_stores
    import kiro_crew.kiro_cli as kiro_cli

    data_dir = tmp_path / "kiro-cli"
    data_dir.mkdir()
    monkeypatch.setattr(
        identity_stores,
        "state_db_candidates",
        lambda platform, home, environ: (data_dir / "data.sqlite3",),
    )

    def set_cli(version: tuple[int, int, int] | None, extracts: dict[str, str]) -> None:
        monkeypatch.setattr(kiro_cli, "installed_kiro_cli_version", lambda: version)
        for name, engine in extracts.items():
            pkg = data_dir / "kas" / name / "node_modules" / "@kiro" / "agent"
            pkg.mkdir(parents=True)
            (pkg / "package.json").write_text(
                json.dumps({"name": "@kiro/agent", "version": engine}), encoding="utf-8"
            )

    return set_cli


def test_the_floor_is_the_first_engine_that_runs_chats_in_parallel():
    from kiro_crew.acp.kas_pin import KAS_PIN_VERSION

    assert KAS_CHAT_SHARING_MIN_ENGINE == (0, 66, 104)
    # The stock bundle install must pass, or the preview never shares on it.
    assert engine_shares_chats(KAS_PIN_VERSION)
    assert not engine_shares_chats(None)
    assert not engine_shares_chats("")


@pytest.mark.parametrize(
    "engine,ready",
    [("0.66.104", True), ("0.67.0", True), ("0.66.103", False), ("0.66.33", False)],
)
def test_bundle_route_reads_the_installed_bundle(route, bundle_engine, engine, ready):
    route(bundle=True)
    bundle_engine(engine)
    assert kas_chat_sharing_ready() is ready


def test_bundle_route_with_no_installed_engine_is_refused(route, bundle_engine):
    route(bundle=True)
    bundle_engine(None)
    assert kas_chat_sharing_ready() is False


def test_relay_with_a_queueing_engine_is_refused_whatever_kiro_cli_says(
    route, bundle_engine, relay
):
    """kiro-cli 3.2.1 passes the old ">= 3.1.0" rule but ships 0.66.102, which queues."""
    route(bundle=False)
    bundle_engine("0.66.104")  # the relay must not read the bundle's engine
    relay((3, 2, 1), {"3.2.1-8785b0a4": "0.66.102", "3.0.0-28b8ec0b": "0.66.70"})
    assert kas_chat_sharing_ready() is False


def test_relay_with_a_parallel_engine_is_allowed(route, relay):
    route(bundle=False)
    # Another kiro-cli version's extract is not the one Crew spawns.
    relay((3, 3, 0), {"3.3.0-aaaa": "0.66.104", "3.2.1-bbbb": "0.66.102"})
    assert kas_chat_sharing_ready() is True


def test_relay_answers_the_oldest_of_several_extracts(route, relay):
    route(bundle=False)
    relay((3, 3, 0), {"3.3.0-aaaa": "0.66.104", "3.3.0-bbbb": "0.66.102"})
    assert kas_chat_sharing_ready() is False


@pytest.mark.parametrize(
    "cli,extracts",
    [
        ((3, 3, 0), {}),  # kiro-cli never unpacked its engine here
        (None, {"3.3.0-aaaa": "0.66.104"}),  # kiro-cli version unreadable
        ((3, 3, 0), {"3.3.0-aaaa": "not-a-version"}),
    ],
)
def test_relay_with_an_unknown_engine_is_refused(route, relay, cli, extracts):
    route(bundle=False)
    relay(cli, extracts)
    assert kas_chat_sharing_ready() is False


def test_an_unreadable_route_is_refused(monkeypatch, bundle_engine):
    import kiro_crew.acp.kas_route as kas_route

    def boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(kas_route, "resolve_kas_route", boom)
    bundle_engine("0.66.104")
    assert kas_chat_sharing_ready() is False


def test_the_session_pin_decides_the_route_the_gate_reads(monkeypatch, bundle_engine):
    """A Kiro CLI pin keeps the relay, so the gate must not read the bundle for it."""
    import kiro_crew.acp.kas_route as kas_route

    seen = []

    def route(**kw):
        seen.append(kw.get("pin"))
        return kas_route.KasRoute(bundle=True, reason="configured")

    monkeypatch.setattr(kas_route, "resolve_kas_route", route)
    bundle_engine("0.66.104")
    assert kas_chat_sharing_ready("kiro") is True
    assert seen == ["kiro"]
