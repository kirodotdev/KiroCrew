"""Incident kc-46d84a — real-Chromium E2E for the same-origin Remote Crew pane relay.

Boots the real topology (see ``instance_pane_relay_fixture/topology.py``): one
published HTTPS hub origin serving the parent AND the REAL ``InstancePaneRelay``,
forwarding to a REAL loopback peer gateway (real built SPA bytes, real
``/api/status``, real ``/api/ws``) over the manager seam — the only stand-in for
the SSH transport. Then it runs ``website/playwright-fixtures/instance-pane-relay.spec.ts``
in a real Chromium against that origin, TWICE, and requires each run to pass.

The spec owns the incident contract: the pane rides the single HTTPS origin under
``/instance-pane/<capability>/`` (no raw loopback port, no remote token), the
sandbox omits ``allow-same-origin``, the real SPA and its built module graph load,
``/api/status`` and ``/api/ws`` complete THROUGH the relay, the opaque-origin
storage shims are usable, ``mc-embedded-ready`` fires and the dashboard renders
before the 15s watchdog, one remote→parent action crosses the exact frame+channel
boundary while a stale/wrong channel is rejected, the raw peer port is unreachable
from the browser, and no mixed-content / CSP / auth failure occurs.

Gated like the rest of the browser gate: ``KIROCREW_E2E`` collects it (it is wired
into ``python setup.py test_e2e`` alongside the smoke and dashboard Playwright
suites), and ``KIROCREW_E2E_REQUIRE`` turns an unresolved toolchain into a failure.

Runner-owned paths, no author host baked in. Heavy temp trees (the peer's
``KIROCREW_HOME``, its TLS cert, the production-source pane-host bundle) are
allocated under the RUNNER's temporary root — ``KC46_ARTIFACT_ROOT`` if set (a
disk-backed override for a host that keeps ``/tmp`` small), else ``RUNNER_TEMP``
or ``TMPDIR`` (the public CI job sets these), else the platform temp dir. Chromium
is resolved from ``PLAYWRIGHT_BROWSERS_PATH`` when the runner points there, and
otherwise from the default Playwright cache. Every run directory is removed on
exit unless ``KC46_RETAIN_ARTIFACTS`` asks to keep the evidence.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn

import pytest

from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="Set KIROCREW_E2E=1 for the kc-46d84a pane-relay real-Chromium E2E",
)

_HERE = Path(__file__).resolve().parent
WORKTREE = _HERE.parents[1]
SPEC = "playwright-fixtures/instance-pane-relay.spec.ts"
CONFIG = "pane-relay.playwright.config.ts"
# The production-source parent host: a dedicated Vite build that mounts the
# UNCHANGED production InstancesViewport + relay authorities. Built here into a
# throwaway dir the topology hub serves as the parent origin — never part of the
# stock `npm run build`.
PANE_HOST_CONFIG = "pane-host.vite.config.ts"


def _artifact_root() -> Path:
    """The runner-owned root for this test's heavy temp trees.

    Precedence: ``KC46_ARTIFACT_ROOT`` (an explicit disk-backed override, for a
    host whose ``/tmp`` is too small or memory-backed) → ``RUNNER_TEMP`` →
    ``TMPDIR`` (the public CI job and most runners set one of these) → the
    platform temp dir. No author home directory is ever assumed.
    """
    override = os.environ.get("KC46_ARTIFACT_ROOT")
    if override:
        return Path(override)
    runner = os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR")
    return Path(runner) if runner else Path(tempfile.gettempdir())


ARTIFACT_ROOT = _artifact_root()
# Keep run directories (and the pane-host bundle) after the test for evidence.
RETAIN_ARTIFACTS = bool(os.environ.get("KC46_RETAIN_ARTIFACTS"))
RUNS = 2  # determinism: the spec must pass on every run


def _unresolved(msg: str) -> NoReturn:
    if os.environ.get("KIROCREW_E2E_REQUIRE"):
        pytest.fail(msg)
    pytest.skip(msg)


def _binary_is_busy(binary: Path) -> bool:
    """True if any process holds *binary* open (mid-download), so a launch would ETXTBSY."""
    target = str(binary)
    for pid_dir in Path("/proc").glob("[0-9]*"):
        try:
            for fd in (pid_dir / "fd").iterdir():
                try:
                    if os.readlink(fd) == target:
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


def _resolve_chromium() -> str:
    """A complete, non-busy Chromium / headless-shell binary from Playwright's cache.

    Prefers a full ``chrome`` build; falls back to the ``chrome-headless-shell``
    (which the config runs headless). Skips any binary a process still holds open
    for writing (a stalled ``playwright install`` leaves the pinned build busy).
    ``PANE_CHROMIUM_EXECUTABLE`` overrides.
    """
    override = os.environ.get("PANE_CHROMIUM_EXECUTABLE")
    if override and os.access(override, os.X_OK) and not _binary_is_busy(Path(override)):
        return override
    # The runner's configured browser cache first (the CI job sets
    # PLAYWRIGHT_BROWSERS_PATH so `playwright install` and this resolver agree),
    # falling back to Playwright's default per-user cache for a plain dev host.
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    cache = Path(browsers_path) if browsers_path else Path.home() / ".cache" / "ms-playwright"
    # The config runs headless, so prefer the purpose-built headless-shell (the
    # binary Playwright itself launches for headless). Preferring it also dodges a
    # host quirk where a stalled `playwright install` leaves the pinned FULL chrome
    # build held open for writing (execve -> ETXTBSY). Fall back to a full build.
    patterns = [
        str(cache / "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"),
        str(cache / "chromium_headless_shell-*/chrome-linux/headless_shell"),
        str(cache / "chromium-*/chrome-linux/chrome"),
    ]
    for pat in patterns:
        for path in sorted(glob.glob(pat), reverse=True):
            p = Path(path)
            if p.is_file() and os.access(p, os.X_OK) and not _binary_is_busy(p):
                return str(p)
    return ""


@pytest.fixture(scope="module")
def toolchain():
    from test_playwright_e2e import _resolve_node18_dir, _resolve_website_dir

    website = _resolve_website_dir()
    if website is None:
        _unresolved("website dir not resolvable (no playwright/ dir)")
    if not (website / "node_modules" / ".bin" / "playwright").exists():
        _unresolved("Playwright CLI not found under website/node_modules")
    if not (website / CONFIG).exists():
        _unresolved(f"{CONFIG} not found under website/")
    node_dir = _resolve_node18_dir()
    if node_dir is None:
        _unresolved("No Node.js >=18 found")
    chromium = _resolve_chromium()
    if not chromium:
        _unresolved("No complete, non-busy Chromium found (run: npx playwright install chromium)")
    pane_host_dist = _build_pane_host(website, node_dir)
    try:
        yield website, node_dir, chromium
    finally:
        # The production-source pane-host bundle is a heavy tree under the runner
        # temp root; remove it unless evidence retention was requested.
        if not RETAIN_ARTIFACTS:
            shutil.rmtree(pane_host_dist, ignore_errors=True)


def _build_pane_host(website: Path, node_dir: str) -> Path:
    """Build the production-source parent host bundle and point the topology at it.

    A dedicated Vite production build of ``playwright-fixtures/pane-host`` (which
    imports the UNCHANGED ``InstancesViewport`` + relay authorities) into a
    throwaway dir under the artifact root. Sets ``KC46_PANE_HOST_DIST`` so
    :class:`PaneRelayTopology` serves the real built SPA at the hub root. A
    missing Vite CLI is an unresolved toolchain (skip/require); a build that RUNS
    and fails is a hard failure — the host bundle is part of what this E2E proves.
    """
    vite = website / "node_modules" / ".bin" / "vite"
    if not (website / PANE_HOST_CONFIG).exists():
        _unresolved(f"{PANE_HOST_CONFIG} not found under website/")
    if not vite.exists():
        _unresolved("Vite CLI not found under website/node_modules")
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    out_dir = ARTIFACT_ROOT / "kc46-pane-host-dist"
    env = dict(os.environ)
    env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    env["PANE_HOST_OUT"] = str(out_dir)
    # Node 22+ can persist a V8 compile cache under $TMPDIR/node-compile-cache
    # that outlives the run; disable it for the build subprocess so this test
    # leaves no compile-cache residue on the runner.
    env["NODE_DISABLE_COMPILE_CACHE"] = "1"
    proc = subprocess.run(
        [str(vite), "build", "--config", str(website / PANE_HOST_CONFIG)],
        cwd=str(website),
        env=env,
        timeout=300,
        capture_output=True,
        **UTF8_TEXT,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or "") + "\n" + (proc.stderr or "")
        pytest.fail(f"pane-host bundle build failed (rc={proc.returncode}):\n{tail[-4000:]}")
    if not (out_dir / "index.html").is_file():
        pytest.fail(f"pane-host build produced no index.html at {out_dir}")
    os.environ["KC46_PANE_HOST_DIST"] = str(out_dir)
    return out_dir


def _passed_specs_in_file(suites: list, file_name: str) -> int:
    count = 0
    for suite in suites:
        if str(suite.get("file", "")).endswith(file_name):
            count += sum(1 for spec in suite.get("specs", []) if spec.get("ok"))
        count += _passed_specs_in_file(suite.get("suites", []), file_name)
    return count


def _run_spec(
    run_idx: int,
    hub_url: str,
    peer_port: int,
    website: Path,
    node_dir: str,
    chromium: str,
    run_dir: Path,
) -> None:
    report = run_dir / f"report-{run_idx}.json"
    env = dict(os.environ)
    env.update(
        {
            "PATH": node_dir + os.pathsep + env.get("PATH", ""),
            "PANE_HUB_URL": hub_url,
            "PANE_PEER_PORT": str(peer_port),
            "PANE_OUT_DIR": str(run_dir / f"evidence-{run_idx}"),
            "PANE_JSON_REPORT": str(report),
            "PANE_CHROMIUM_EXECUTABLE": chromium,
            "CI": "1",
            # No V8 compile-cache residue from the Playwright node subprocess.
            "NODE_DISABLE_COMPILE_CACHE": "1",
        }
    )
    pw_bin = website / "node_modules" / ".bin" / "playwright"
    rc = subprocess.call(
        # No --reporter override: the config's JSON reporter writes to
        # PANE_JSON_REPORT (a CLI --reporter would replace it and lose the file).
        [str(pw_bin), "test", "--config", str(website / CONFIG), "--retries=0"],
        cwd=str(website),
        env=env,
        timeout=300,
    )
    assert report.is_file(), f"run {run_idx}: Playwright wrote no JSON report (rc={rc})"
    parsed = json.loads(report.read_text(encoding="utf-8"))
    stats = parsed.get("stats", {})
    passed = _passed_specs_in_file(parsed.get("suites", []), Path(SPEC).name)
    # Five spec tests: the first-load boot+rotation contract, the
    # subsequent-document (navigation + reload) reseed contract, the delayed
    # bootstrap handshake (fail-closed timing: a reply past the old 2s fallback
    # still boots with the real bank), the off-capability replacement (a document
    # replacing the pane in the same iframe gets no channel / host model), and the
    # asynchronous chained-crew refusal (held past the announcing document's
    # navigation, it reaches the replacement neither).
    assert passed == 5, f"run {run_idx}: expected 5 passed incident specs, got {passed}: {stats}"
    assert (
        int(stats.get("skipped", 0)) == 0
    ), f"run {run_idx}: a skipped spec is a silent pass: {stats}"
    assert rc == 0, f"run {run_idx}: playwright exited {rc}"
    evidence = list((run_dir / f"evidence-{run_idx}").glob("instance-pane-relay-evidence.json"))
    assert evidence, f"run {run_idx}: no evidence manifest written"


def test_relay_pane_boots_over_one_https_origin(toolchain) -> None:
    """Boot the real topology and require the incident spec to pass on every run."""
    from e2e.instance_pane_relay_fixture.topology import PaneRelayTopology

    website, node_dir, chromium = toolchain
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="kc46-e2e-", dir=str(ARTIFACT_ROOT)))
    try:
        for run_idx in range(1, RUNS + 1):
            # A fresh topology per run (fresh peer gateway + hub + capability) so
            # the determinism check exercises the whole flow end to end each time.
            with PaneRelayTopology(artifacts=run_dir / f"topo-{run_idx}") as topo:
                _run_spec(
                    run_idx, topo.hub_url, topo.peer_port, website, node_dir, chromium, run_dir
                )
    finally:
        # Never leak the run tree (peer KIROCREW_HOME, TLS cert, evidence,
        # per-run TMPDIR) onto the runner. KC46_RETAIN_ARTIFACTS keeps it for a
        # post-mortem or a named CI artifact upload.
        if not RETAIN_ARTIFACTS:
            shutil.rmtree(run_dir, ignore_errors=True)
