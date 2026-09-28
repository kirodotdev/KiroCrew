"""Offline checks for the dedicated Memory UI evidence driver."""

import json
from types import SimpleNamespace

import pytest
from e2e import test_memory_ui_evidence as evidence


@pytest.mark.parametrize(
    "spec_ok,skipped,rc,has_manifest",
    [
        (False, 0, 0, True),
        (True, 1, 0, True),
        (True, 0, 1, True),
        (True, 0, 0, False),
        (True, 0, 0, True),
    ],
)
@pytest.mark.parametrize("dedicated_output", [False, True])
def test_evidence_requires_executed_passing_spec_and_manifest(
    tmp_path, monkeypatch, spec_ok, skipped, rc, has_manifest, dedicated_output
):
    website = tmp_path / "website"
    website.mkdir()
    scenario = "configured-inactive"
    if dedicated_output:
        root = tmp_path / "ci-artifacts"
        monkeypatch.setenv(evidence.EVIDENCE_DIR_ENV, str(root))
    else:
        root = tmp_path / "evidence"
        monkeypatch.delenv(evidence.EVIDENCE_DIR_ENV, raising=False)
    output = root / scenario
    output.mkdir(parents=True)
    if has_manifest:
        (output / "memory-v2-embedding-evidence.json").write_text("{}", encoding="utf-8")

    def run(argv, *, cwd, env, timeout):
        assert cwd == str(tmp_path)
        assert argv[argv.index("--config") + 1] == str(website / "playwright.config.ts")
        assert str(website / evidence.SPEC) in argv
        assert "--output" not in argv
        assert str(output) not in argv
        assert env["PLAYWRIGHT_MEMORY_EVIDENCE_OUTPUT_DIR"] == str(output)
        assert timeout == 540
        assert "--retries=0" in argv
        assert env["PLAYWRIGHT_RUN_MEMORY_EVIDENCE"] == "1"
        # A passing setup project must not stand in for the evidence test.
        report = {
            "stats": {"skipped": skipped},
            "suites": [
                {"file": "auth.setup.ts", "specs": [{"ok": True}]},
                {"suites": [{"file": evidence.SPEC, "specs": [{"ok": spec_ok}]}]},
            ],
        }
        from pathlib import Path

        Path(env["PLAYWRIGHT_JSON_OUTPUT_NAME"]).write_text(json.dumps(report), encoding="utf-8")
        return rc

    monkeypatch.setattr(evidence.subprocess, "call", run)
    gw = SimpleNamespace(port=12345, token="fixture-token")
    if spec_ok and not skipped and rc == 0 and has_manifest:
        evidence._run_scenario(scenario, gw, website, str(tmp_path), tmp_path)
    else:
        with pytest.raises(AssertionError):
            evidence._run_scenario(scenario, gw, website, str(tmp_path), tmp_path)


def test_bound_failure_port_refuses_connections_and_is_released():
    import socket

    fixture = evidence.closed_loopback_port.__wrapped__()
    port = next(fixture)
    try:
        with socket.socket() as contender:
            with pytest.raises(OSError):
                contender.bind(("127.0.0.1", port))
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
    finally:
        fixture.close()
    with socket.socket() as released:
        released.bind(("127.0.0.1", port))


def test_every_scenario_grep_selects_exactly_one_spec_title():
    """``--grep`` is a regex over the full title: a fragment that matches two
    titles would run both under one gateway, and a fragment that matches none
    would let the passed-spec count catch it only after a real gateway boot.
    Pin the mapping offline against the spec file, one title per scenario."""
    import re
    from pathlib import Path

    spec = Path(evidence.__file__).resolve().parents[2] / "website" / evidence.SPEC
    titles = re.findall(r"^test\('((?:[^'\\]|\\.)*)'", spec.read_text(encoding="utf-8"), re.M)
    assert titles, spec
    assert all("@memory-evidence" in title for title in titles), titles
    for scenario, fragment in evidence.SCENARIOS.items():
        matched = [title for title in titles if re.search(fragment, title)]
        assert len(matched) == 1, (scenario, fragment, matched)
        assert f"toBe('{scenario}')" in spec.read_text(encoding="utf-8"), scenario
    # Every spec title is owned by exactly one scenario; none is orphaned.
    assert len(titles) == len(evidence.SCENARIOS), (titles, list(evidence.SCENARIOS))


@pytest.mark.parametrize("failure", [503, 403, 500, "other"])
def test_readiness_wait_retries_only_service_unavailable(monkeypatch, failure):
    import urllib.error

    calls, sleeps = [], []

    def get(path):
        calls.append(path)
        if len(calls) == 1:
            if failure == "other":
                raise AssertionError("unexpected reply")
            cause = urllib.error.HTTPError("http://localhost/test", failure, "refused", {}, None)
            raise AssertionError("gateway refused") from cause
        return {}

    monkeypatch.setattr(evidence.time, "sleep", sleeps.append)
    client = SimpleNamespace(get=get)
    if failure == 503:
        evidence._await_ready(client, "/api/memory/stats?store=default")
        assert calls == ["/api/memory/stats?store=default"] * 2
        assert sleeps == [0.5]
    else:
        with pytest.raises(AssertionError):
            evidence._await_ready(client, "/api/memory/stats?store=default")
        assert len(calls) == 1 and not sleeps


def test_readiness_wait_has_a_deadline(monkeypatch):
    import urllib.error

    clock, calls = [0.0], []

    def sleep(seconds):
        clock[0] += seconds

    def get(path):
        calls.append(path)
        cause = urllib.error.HTTPError("http://localhost/test", 503, "pending", {}, None)
        raise AssertionError("recovery pending") from cause

    monkeypatch.setattr(evidence.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(evidence.time, "sleep", sleep)
    with pytest.raises(AssertionError, match="recovery pending"):
        evidence._await_ready(SimpleNamespace(get=get), "/api/memory/stats?store=default", secs=1)
    assert len(calls) == 3 and clock[0] == 1


@pytest.mark.parametrize("has_run", [False, True])
def test_checkpoint_evidence_fault_uses_only_an_empty_isolated_directory(
    tmp_path, monkeypatch, has_run
):
    from contextlib import nullcontext

    from kiro_crew.testing import harness

    home = tmp_path / "gateway"
    runs = home / "workflows" / "runs"
    runs.mkdir(parents=True)
    if has_run:
        (runs / "keep.json").write_text("keep", encoding="utf-8")
    client = SimpleNamespace(
        get=lambda _path: {
            "model_active": False,
            "model_id": "configured",
            "model_dim": 2,
            "model_source": "default",
        }
    )
    monkeypatch.setattr(
        harness, "spawn_feature_gateway", lambda **_kw: nullcontext(SimpleNamespace(home=home))
    )
    monkeypatch.setattr(evidence, "_client", lambda _gw: client)
    captured = []
    monkeypatch.setattr(evidence, "_run_scenario", lambda *_args: captured.append(runs.is_file()))
    if has_run:
        with pytest.raises(OSError):
            evidence.test_known_model_configured_but_not_serving((tmp_path, "node"), None, tmp_path)
        assert (runs / "keep.json").read_text(encoding="utf-8") == "keep"
        assert not captured
    else:
        evidence.test_known_model_configured_but_not_serving((tmp_path, "node"), None, tmp_path)
        assert captured == [True]
