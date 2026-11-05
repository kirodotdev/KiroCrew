"""Capabilities say where a person behind ``tailscale serve`` can be named.

The kernel proof that a loopback client is ``tailscaled`` exists only on Linux
with a daemon running as another user (``tailnet_proxy_proof.proof_support``).
Where it cannot succeed and ``tailscale serve`` is the only way in, the
``attribution`` capability reads ``unavailable`` with the reason, instead of
claiming names it can never produce.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew.dashboard import tailnet_proxy_proof as proof
from kiro_crew.shared_crew import access, capabilities, wiring
from kiro_crew.shared_crew.access import FORWARDED_ATTRIBUTION_REASONS, note_tailnet


@pytest.fixture(autouse=True)
def _clean():
    access._reset_setup_paths_for_tests()
    capabilities._reset_capability_sources_for_tests()
    wiring.install()
    yield
    wiring.uninstall()
    capabilities._reset_capability_sources_for_tests()
    access._reset_setup_paths_for_tests()


def _attribution() -> tuple[str, str]:
    body = capabilities.aggregate_capabilities().to_dict()
    return body["attribution"], body["reasons"].get("attribution", "")


def test_the_reason_codes_match_the_proof_module():
    assert set(FORWARDED_ATTRIBUTION_REASONS) == {
        proof.PROOF_PLATFORM_UNPROVABLE,
        proof.PROOF_DAEMON_SAME_USER,
        proof.PROOF_DAEMON_NOT_FOUND,
    }


@pytest.mark.parametrize(
    ("gap", "state"),
    [
        ("platform_unprovable", "unavailable"),
        ("daemon_same_user", "unavailable"),
        ("daemon_not_found", "degraded"),
    ],
)
def test_serve_only_sharing_without_the_proof_reports_the_gap(gap, state):
    note_tailnet(
        enabled=True,
        host="crew.tail.ts.net",
        identity_trust=True,
        loopback_only=True,
        proof_gap=gap,
    )
    assert _attribution() == (state, gap)


def test_an_unrecognised_gap_reads_unavailable():
    note_tailnet(enabled=True, host="h", identity_trust=True, loopback_only=True, proof_gap="??")
    assert _attribution() == ("unavailable", "platform_unprovable")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"identity_trust": True, "loopback_only": True, "proof_gap": ""},
        {"identity_trust": True, "loopback_only": False, "proof_gap": "platform_unprovable"},
        {"identity_trust": False, "loopback_only": True, "proof_gap": "platform_unprovable"},
    ],
    ids=["proof-works", "direct-bind", "no-sharing"],
)
def test_attribution_stays_available_where_the_gap_does_not_apply(kwargs):
    note_tailnet(enabled=True, host="crew.tail.ts.net", **kwargs)
    assert _attribution() == ("available", "")


def test_the_row_says_not_checked_until_the_post_start_probe_answers():
    note_tailnet(enabled=True, host="h", identity_trust=True, loopback_only=True, proof_gap=None)
    assert _attribution() == ("degraded", "not_checked")
    access.note_proof_gap("daemon_same_user")
    assert _attribution() == ("unavailable", "daemon_same_user")
    access.note_proof_gap("")
    assert _attribution() == ("available", "")


def test_the_boot_path_runs_no_host_probe(monkeypatch):
    """The access set is installed at boot; the laptop and keep-awake probe is not."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import tailnet_direct_listener
    from kiro_crew.shared_crew import access_set, host_power

    def refuse() -> None:
        raise AssertionError("a host probe ran on the boot path")

    monkeypatch.setattr(host_power, "note_host", refuse)
    monkeypatch.setattr(access_set, "install", lambda _seed, **_kw: None)
    tailnet_direct_listener._install_access_set(KiroCrewConfig())


@pytest.mark.asyncio
async def test_the_post_start_probe_fills_both_rows(monkeypatch):
    from kiro_crew.shared_crew import host_power

    note_tailnet(enabled=True, host="h", identity_trust=True, loopback_only=True, proof_gap=None)
    monkeypatch.setattr(proof, "proof_support", lambda: "daemon_not_found")
    noted: list[bool] = []
    monkeypatch.setattr(host_power, "note_host", lambda: noted.append(True) or {})
    await host_power.probe_after_start()
    assert _attribution() == ("degraded", "daemon_not_found")
    assert noted == [True]


@pytest.mark.asyncio
async def test_the_maintenance_starter_kicks_the_host_probe(monkeypatch):
    from aiohttp import web

    from kiro_crew.shared_crew import host_power, lifecycle

    ran: list[bool] = []

    async def probe() -> None:
        ran.append(True)

    monkeypatch.setattr(host_power, "probe_after_start", probe)
    start = lifecycle.install_lifecycle(web.Application())
    start()
    start()
    for _ in range(5):
        await asyncio.sleep(0)
    assert ran == [True]


def test_a_failed_source_still_wins_over_the_gap_reason():
    note_tailnet(enabled=True, host="h", identity_trust=True, loopback_only=True, proof_gap="")

    def boom() -> capabilities.CapabilityContribution:
        raise RuntimeError("x")

    capabilities.register_capability_source("test.boom", ("attribution",), boom)
    assert _attribution() == ("unavailable", "source_failed")


# -- proof_support against a fake /proc -------------------------------------- #


def _daemon(root: Path, pid: int, uid: int) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "comm").write_text("tailscaled\n")
    (d / "status").write_text(f"Name:\ttailscaled\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")


@pytest.fixture()
def fake_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "_PROC", tmp_path)
    monkeypatch.setattr(proof.sys, "platform", "linux")
    monkeypatch.setattr(proof, "_effective_uid", lambda: 1000)
    return tmp_path


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_proof_support_off_linux_is_platform_unprovable(fake_proc, monkeypatch, platform):
    _daemon(fake_proc, 77, 0)
    monkeypatch.setattr(proof.sys, "platform", platform)
    assert proof.proof_support() == "platform_unprovable"


def test_proof_support_with_a_root_daemon_is_provable(fake_proc):
    _daemon(fake_proc, 77, 0)
    assert proof.proof_support() == ""


def test_proof_support_with_a_same_user_daemon_is_not(fake_proc):
    _daemon(fake_proc, 77, 1000)
    assert proof.proof_support() == "daemon_same_user"


def test_proof_support_without_a_daemon(fake_proc):
    assert proof.proof_support() == "daemon_not_found"
