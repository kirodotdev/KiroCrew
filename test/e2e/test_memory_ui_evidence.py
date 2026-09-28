"""Real Memory-tab embedding states, photographed by the browser suite.

One DEDICATED harness gateway per scenario, because every state here is a
gateway-wide fact (its config.json, its download manager, its open stores) that
the shared ``test_playwright_e2e`` gateway must not carry into the other 230
specs. Each gateway is prepared through the production surfaces a user has --
the config file for the config-file-only ``memory.embed_model_path`` knob, the
owner HTTP API for facts and for the dashboard's own Retry call -- and then
``website/playwright/memory-embedding-evidence.spec.ts`` renders the real
dashboard against it and asserts the UI matches ``/api/memory/embedding-status``.

Nothing is mocked: no ``route.fulfill``, no frontend state edit, no patched
download manager. The fake ACP model backend stays the only stand-in.

The download-failure scenario is the expensive one. The production retry policy
(``DOWNLOAD_ATTEMPTS_INTERACTIVE`` = 3, 60s then 120s backoff) is not shortened;
the loopback URL simply refuses each connection at once, so the terminal
``download_step == "failed"`` arrives after ~3 minutes. The boot-time background
download (6 attempts, up to ~31 min) would otherwise hold the manager's lock and
make the dashboard's Retry adopt IT, so that gateway boots with a custom model
path configured (which the background task honours by not downloading), and the
path is removed from config only after boot, exactly as an operator would edit
it. See docs/ci/e2e-gate.md.

Gated like the rest of the browser gate: ``KIROCREW_E2E`` collects it,
``KIROCREW_E2E_REQUIRE`` turns an unresolved toolchain into a failure.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator, NoReturn

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="Set KIROCREW_E2E=1 for the memory UI evidence gateways",
)

SPEC = "playwright/memory-embedding-evidence.spec.ts"
EVIDENCE_DIR_ENV = "KIROCREW_MEMORY_UI_EVIDENCE_DIR"

# Title fragments passed to ``--grep``; one test per scenario in the spec.
# Each fragment must select exactly one spec title (``--grep`` is a regex over
# the full title), which test/test_memory_ui_evidence_driver.py pins against the
# spec file so a new scenario cannot silently run its neighbour.
SCENARIOS: dict[str, str] = {
    "missing-custom-legacy": "missing configured model with inherited vectors",
    "missing-custom-pointer": "missing configured model and no inherited vectors",
    "configured-inactive": "a known configured model that is not serving",
    "deferred-repair": "a standing rebuild reports vectors, open invalidation and deferred stores",
    "download-failed": "a bundled download that exhausts its retries",
}


def _unresolved(msg: str) -> NoReturn:
    if os.environ.get("KIROCREW_E2E_REQUIRE"):
        pytest.fail(msg)
    pytest.skip(msg)


def _client(gw):
    """The boot matrix's cookie-jar client: mints the session cookie once from
    the harness link token and sends only the cookie afterwards."""
    from e2e.test_gateway_boot_matrix import _Client

    client = _Client(gw.port, gw.token)
    client.diagnostics = gw.diagnostics
    return client


def _put_fact(client, key: str, value: str) -> None:
    body = json.dumps({"key": key, "value": value, "source": "user_explicit"}).encode()
    request = urllib.request.Request(
        f"http://localhost:{client._port}/api/memory/semantic?store=default",
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json", "X-Session-Key": "dashboard:ui"},
    )
    client._open(request, timeout=30)


def _await_ready(client, path: str, secs: float = 30) -> None:
    """HTTP readiness precedes recovery; wait on a read before seeding data."""
    deadline = time.monotonic() + secs
    while True:
        try:
            client.get(path)
            return
        except AssertionError as exc:
            cause = exc.__cause__
            if not isinstance(cause, urllib.error.HTTPError) or cause.code != 503:
                raise
            if time.monotonic() >= deadline:
                raise
        time.sleep(0.5)


def _await_status(client, ready: Callable[[dict], bool], what: str, secs: float = 30) -> dict:
    deadline = time.monotonic() + secs
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get("/api/memory/embedding-status")
        if ready(last):
            return last
        time.sleep(0.5)
    pytest.fail(
        f"embedding-status never reached {what}: {json.dumps(last, sort_keys=True)[:800]}\n"
        f"{client.diagnostics()}"
    )


@pytest.fixture()
def closed_loopback_port() -> Iterator[int]:
    """Keep the port bound without listening throughout the download scenario."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        yield probe.getsockname()[1]


def _run_scenario(scenario: str, gw, website: Path, node_dir: str, tmp: Path) -> None:
    """Shell the one matching spec test at ``gw`` and require it to have run."""
    report = tmp / f"{scenario}.json"
    configured_output = os.environ.get(EVIDENCE_DIR_ENV)
    root = Path(configured_output) if configured_output else tmp / "evidence"
    assert root.is_absolute(), "evidence output must be an absolute temporary path"
    out_dir = root / scenario
    env = dict(os.environ)
    env.update(
        {
            "PATH": node_dir + os.pathsep + env.get("PATH", ""),
            "PLAYWRIGHT_BASE_URL": f"http://localhost:{gw.port}",
            "PLAYWRIGHT_TOKEN": gw.token,
            # Its own storage state: cookies are bound to one gateway's port.
            "PLAYWRIGHT_STORAGE_STATE": str(tmp / f"state-{scenario}.json"),
            # Opts the @memory-evidence spec in; the shared suite leaves it out.
            "PLAYWRIGHT_RUN_MEMORY_EVIDENCE": "1",
            # Playwright clears this directory per run; keep scenarios separate.
            "PLAYWRIGHT_MEMORY_EVIDENCE_OUTPUT_DIR": str(out_dir),
            "KIROCREW_E2E_MEMORY_SCENARIO": scenario,
            "KIROCREW_E2E_EPHEMERAL": "1",
            "CI": "1",
            "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report),
        }
    )
    pw_bin = website / "node_modules" / ".bin" / "playwright"
    rc = subprocess.call(
        [
            str(pw_bin),
            "test",
            str(website / SPEC),
            "--config",
            str(website / "playwright.config.ts"),
            "--grep",
            SCENARIOS[scenario],
            "--reporter=json",
            "--retries=0",
        ],
        cwd=str(tmp),
        env=env,
        timeout=540,
    )
    assert report.is_file(), f"{scenario}: Playwright wrote no JSON report (rc={rc})"
    parsed = json.loads(report.read_text(encoding="utf-8"))
    stats = parsed.get("stats", {})
    # The report also carries the `setup` project's authenticate test, so count
    # the spec's own passed tests rather than the run total.
    passed = _passed_specs_in_file(parsed.get("suites", []), Path(SPEC).name)
    assert passed == 1, f"{scenario}: expected one passed evidence spec, got {passed}: {stats}"
    assert (
        int(stats.get("skipped", 0)) == 0
    ), f"{scenario}: a skipped spec is a silent pass: {stats}"
    assert rc == 0, f"{scenario}: playwright exited {rc}"
    manifests = list(out_dir.rglob("memory-v2-embedding-evidence.json"))
    assert manifests, f"{scenario}: no evidence manifest under {out_dir}"


def _passed_specs_in_file(suites: list, file_name: str) -> int:
    """Count specs in ``file_name`` that Playwright's JSON report marks ``ok``."""
    count = 0
    for suite in suites:
        if str(suite.get("file", "")).endswith(file_name):
            count += sum(1 for spec in suite.get("specs", []) if spec.get("ok"))
        count += _passed_specs_in_file(suite.get("suites", []), file_name)
    return count


@pytest.fixture(scope="module")
def toolchain() -> tuple[Path, str]:
    from test_playwright_e2e import _resolve_node18_dir, _resolve_website_dir

    website = _resolve_website_dir()
    if website is None:
        _unresolved("website dir not resolvable (no playwright/ dir)")
    if not (website / "node_modules" / ".bin" / "playwright").exists():
        _unresolved("Playwright CLI not found under website/node_modules")
    node_dir = _resolve_node18_dir()
    if node_dir is None:
        _unresolved("No Node.js >=18 found")
    return website, node_dir


@pytest.fixture()
def fake_backend(monkeypatch) -> Iterator[None]:
    from kiro_crew.testing import fake_acp_backend

    monkeypatch.setenv("KIROCREW_KIRO_BIN", str(fake_acp_backend.__file__))
    yield


def _patch_config(home: Path, mutate: Callable[[dict], dict]) -> None:
    from kiro_crew.config.loader import update_config_locked

    update_config_locked(home / "config.json", mutate=mutate)


def test_missing_custom_model_with_legacy_vectors(toolchain, fake_backend, tmp_path) -> None:
    from kiro_crew.testing.harness import spawn_feature_gateway

    website, node_dir = toolchain
    with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
        missing = gw.home / "models" / "moved-away-custom.gguf"

        def _configure(data: dict) -> dict:
            # The config-file-only knob, edited the way an operator edits it.
            data.setdefault("memory", {}).update(
                {
                    "embed_model_path": str(missing),
                    "embed_model_legacy_ids": ["custom:moved-away-custom.gguf:700000000"],
                }
            )
            return data

        _patch_config(gw.home, _configure)
        _await_status(
            _client(gw),
            lambda s: s.get("setup_error_code") == "model_path_not_found"
            and s.get("setup_warning_code") == "legacy_embedding_vectors",
            "model_path_not_found + legacy_embedding_vectors",
        )
        _run_scenario("missing-custom-legacy", gw, website, node_dir, tmp_path)


def test_missing_custom_model_without_legacy_vectors(toolchain, fake_backend, tmp_path) -> None:
    from kiro_crew.testing.harness import spawn_feature_gateway

    website, node_dir = toolchain
    with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
        missing = gw.home / "models" / "moved-away-custom.gguf"

        def _configure(data: dict) -> dict:
            # Same missing file as the legacy scenario, but no inherited ids:
            # the Vector Memory card has no legacy warning to carry the settings
            # link, so it must render the short path-error pointer instead.
            memory = data.setdefault("memory", {})
            memory["embed_model_path"] = str(missing)
            assert "embed_model_legacy_ids" not in memory
            return data

        _patch_config(gw.home, _configure)
        _await_status(
            _client(gw),
            lambda s: s.get("setup_error_code") == "model_path_not_found"
            and not s.get("setup_warning_code"),
            "model_path_not_found without a legacy warning",
        )
        _run_scenario("missing-custom-pointer", gw, website, node_dir, tmp_path)


def test_known_model_configured_but_not_serving(toolchain, fake_backend, tmp_path) -> None:
    from kiro_crew.testing.harness import spawn_feature_gateway

    website, node_dir = toolchain
    with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
        # A fresh harness gateway never downloads the bundled file, so the
        # configured identity is known while nothing serves vectors.
        status = _await_status(
            _client(gw),
            lambda s: s.get("model_active") is False
            and bool(s.get("model_id"))
            and s.get("model_dim"),
            "known model identity with model_active=false",
        )
        assert status["model_source"] == "default", status
        # Fault only this isolated gateway's empty checkpoint directory AFTER
        # recovery. Browser-created workflows then exercise real failing I/O.
        client = _client(gw)
        _await_ready(client, "/api/workflows/runs")
        runs = gw.home / "workflows" / "runs"
        runs.parent.mkdir(parents=True, exist_ok=True)
        if runs.exists():
            runs.rmdir()  # Refuses a non-empty directory; never delete run data.
        runs.write_text("evidence checkpoint directory blocker", encoding="utf-8")
        _run_scenario("configured-inactive", gw, website, node_dir, tmp_path)


def test_deferred_repair_reports_three_units(toolchain, fake_backend, tmp_path) -> None:
    from kiro_crew.testing.harness import spawn_feature_gateway

    website, node_dir = toolchain
    with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
        client = _client(gw)
        _await_ready(client, "/api/memory/stats?store=default")

        def _configure(data: dict) -> dict:
            # A managed rebuild request, plus a declared store that is never
            # opened during this gateway's life: at least one deferred store.
            # Other configured stores may also be closed; never pin their total.
            data.setdefault("memory", {})["embed_rebuild_generation"] = "evidence-request-1"
            stores = data.setdefault("memory_stores", {})
            assert "memory-evidence-archive" not in stores
            stores["memory-evidence-archive"] = {}
            return data

        _patch_config(gw.home, _configure)
        # Two real facts; with no model serving, both wait for a vector.
        _put_fact(client, "pref.frontend.framework", "react")
        _put_fact(client, "pref.backend.language", "python")
        status = _await_status(
            client,
            lambda s: (s.get("repair") or {}).get("generation") == "evidence-request-1"
            and (s.get("reembed") or {}).get("step") == "deferred"
            and (s.get("repair") or {}).get("pending_vectors", 0) >= 2
            and (s.get("repair") or {}).get("deferred_stores", 0) >= 1,
            "reembed.step=deferred with pending vectors and a deferred store",
        )
        assert status["repair"]["unknown_scope"] is False, status["repair"]
        _run_scenario("deferred-repair", gw, website, node_dir, tmp_path)


def test_bundled_download_exhausts_retries(
    toolchain, fake_backend, tmp_path, monkeypatch, closed_loopback_port
) -> None:
    from kiro_crew.testing.harness import spawn_feature_gateway

    website, node_dir = toolchain
    port = closed_loopback_port
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    # Supported production knobs, exported to the gateway by the harness env:
    # the mirror override (https only) at a loopback port nothing serves, and
    # an empty Ollama blob store so the legacy-salvage path finds nothing on
    # the runner. No packet leaves the host and no real model is fetched.
    monkeypatch.setenv("KIROCREW_EMBED_MODEL_URL", f"https://127.0.0.1:{port}/never-served.gguf")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "no-ollama-blobs"))

    with spawn_feature_gateway(fixture="minimal", approval="reads") as first:
        missing = first.home / "models" / "moved-away-custom.gguf"

        def _configure(data: dict) -> dict:
            data.setdefault("memory", {})["embed_model_path"] = str(missing)
            return data

        _patch_config(first.home, _configure)
        # Boot with the custom path in place: the background task declines to
        # download, so the manager's lock stays free for the dashboard's Retry.
        gw = first.restart(skip_model_download=False)
        client = _client(gw)
        _await_status(
            client,
            lambda s: s.get("model_source") == "custom" and s.get("download_step") == "idle",
            "custom model configured, no download in flight",
        )

        def _clear(data: dict) -> dict:
            data.setdefault("memory", {}).pop("embed_model_path", None)
            return data

        _patch_config(gw.home, _clear)
        _await_status(
            client,
            lambda s: s.get("model_source") == "default"
            and s.get("download_step") == "idle"
            and s.get("model_available") is False,
            "bundled model configured, not present, no download in flight",
        )
        # The spec itself issues the dashboard's POST /api/memory/enable-embeddings
        # and waits for download_step == "failed" (three refused attempts).
        _run_scenario("download-failed", gw, website, node_dir, tmp_path)
