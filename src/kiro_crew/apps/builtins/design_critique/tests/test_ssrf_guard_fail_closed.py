"""Tests: design-critique SSRF guard fails CLOSED when routeWebSocket is missing.

A silent capability probe (``if (typeof target.routeWebSocket === 'function')``)
around the WebSocket interception leaves the guard skipped on Playwright builds
older than 1.48, so a rendered page can open ``ws://`` to any internal host.
These tests pin the fail-closed contract instead: the silent-skip form stays
out of ``ssrf-guard.mjs``, ``installSsrfGuard`` refuses (throws) when the
capability is absent, the pinned Playwright line provides ``routeWebSocket``
(>= 1.48), and ``getPlaywright`` refuses to resolve a build the guard must
reject — a stale pre-1.48 cache falls through to reinstall rather than
permanently breaking capture.

The four runtime checks share ONE ``node`` child per run of this module, on one
worker. A cold Node start pages in a ~100 MB binary, and four of them at once on
four workers, on a Linux shard near its memory ceiling, stall the whole runner
until every test in flight times out.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.testing import wait
from kiro_crew.testing.wait import default_timeout

# One worker, so the module starts one node, not one per worker its tests land on.
pytestmark = pytest.mark.xdist_group(name="node_probe_ssrf_guard_fail_closed")

# tests/<this file> -> parents[1] is the app's own directory.
APP_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = APP_ROOT / "skills/design-critique/scripts"
SSRF_GUARD = SCRIPTS / "ssrf-guard.mjs"
ENSURE_PW = SCRIPTS / "ensure-playwright.mjs"


def _run_node(
    script: str, cwd: Path, deadline: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run *script* in one node child, bounded at half the running test's timeout.

    Half, so a stalled child fails here, by name, instead of as the runner's bare
    ``Timeout``: 60 s on the Linux lanes, 90 s on Windows, which still covers a
    cold Node ESM start on a hosted Windows runner (more than a minute measured).
    """
    if deadline is None:
        deadline = default_timeout()
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    try:
        return subprocess.run(
            [node, "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=deadline,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"node did not finish the ssrf-guard probe within {deadline:.0f}s; "
            f"stdout so far: {exc.stdout!r}, stderr so far: {exc.stderr!r}"
        )


def _fake_playwright(root: Path, version: str) -> Path:
    """A ``node_modules/playwright`` of *version* under *root*; returns *root*."""
    pkg = root / "node_modules" / "playwright"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": "playwright", "version": version}))
    (pkg / "index.js").write_text("module.exports = { chromium: {} }\n")
    return root


# Each case prints one word. ensure-playwright.mjs reads DC_PW_DIR when it is
# evaluated, so each cache case imports its own copy under its own query string,
# which ESM evaluates as a separate module, after setting the variable.
_PROBE = """
const verdicts = {}
async function probe(name, body) {
  try { verdicts[name] = await body() } catch (e) { verdicts[name] = 'threw: ' + e }
}
const { installSsrfGuard } = await import(%(guard)s)
await probe('without_route_websocket', async () => {
  let routed = false
  try {
    await installSsrfGuard({ route: async () => { routed = true } }, null)
    return 'resolved'
  } catch (e) {
    if (routed) return 'refused after installing the HTTP route'
    return String(e).includes('routeWebSocket') ? 'refused' : 'wrong error: ' + e
  }
})
await probe('with_route_websocket', async () => {
  await installSsrfGuard({ route: async () => {}, routeWebSocket: async () => {} }, null)
  return 'installed'
})
process.env.DC_PW_DIR = %(stale_cache)s
await probe('stale_cache', async () => {
  const { getPlaywright } = await import(%(stale_script)s + '?case=stale')
  return (await getPlaywright({ autoInstall: false })) === null ? 'refused' : 'returned'
})
process.env.DC_PW_DIR = %(usable_cache)s
await probe('usable_cache', async () => {
  const { getPlaywright } = await import(%(usable_script)s + '?case=usable')
  const m = await getPlaywright({ autoInstall: false })
  return m && m.chromium ? 'returned' : 'refused'
})
console.log(JSON.stringify(verdicts))
"""


@pytest.fixture(scope="module")
def node_verdicts(tmp_path_factory: pytest.TempPathFactory):
    """Every runtime case's verdict, from one node child.

    ``getPlaywright`` resolves ``playwright`` by climbing ``node_modules`` from its
    own script's directory, so each cache case runs a copy of the script whose
    climb starts in this fixture's tmp dir: a playwright above the checkout is
    never reached, and the only build either case can find is the planted one.
    The script imports only Node built-ins and playwright, so the one file is
    enough.
    """
    root = tmp_path_factory.mktemp("ssrf_node_probe")
    stale_script = _fake_playwright(root / "stale_script", "1.47.2") / ENSURE_PW.name
    shutil.copy2(ENSURE_PW, stale_script)
    usable_script = root / "usable_script" / ENSURE_PW.name
    usable_script.parent.mkdir()
    shutil.copy2(ENSURE_PW, usable_script)
    script = _PROBE % {
        "guard": json.dumps(SSRF_GUARD.as_uri()),
        "stale_cache": json.dumps(str(_fake_playwright(root / "stale_cache", "1.47.2"))),
        "stale_script": json.dumps(stale_script.as_uri()),
        "usable_cache": json.dumps(str(_fake_playwright(root / "usable_cache", "1.58.2"))),
        "usable_script": json.dumps(usable_script.as_uri()),
    }
    proc = _run_node(script, root)
    assert proc.returncode == 0, f"node exited {proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestSsrfGuardSourcePins:
    def test_silent_capability_skip_is_gone(self):
        """The fail-open wrapper must never come back around the WS guard."""
        src = SSRF_GUARD.read_text(encoding="utf-8")
        assert "routeWebSocket(" in src, "WebSocket guard itself must remain"
        assert "if (typeof target.routeWebSocket === 'function')" not in src, (
            "silent-skip capability probe reintroduced — the guard must refuse, " "not skip"
        )

    def test_missing_capability_raises(self):
        """A missing routeWebSocket must be answered with a thrown Error."""
        src = SSRF_GUARD.read_text(encoding="utf-8")
        throw = re.search(r"throw new Error\(([^;]*)", src, re.DOTALL)
        assert throw is not None, "no throw new Error(...) in ssrf-guard.mjs"
        assert "routeWebSocket" in throw.group(
            1
        ), "the refusal message must name the missing capability"


class TestEnsurePlaywrightPin:
    def test_pinned_version_provides_route_websocket(self):
        """PW_VERSION must be >= 1.48, where routeWebSocket first appeared."""
        src = ENSURE_PW.read_text(encoding="utf-8")
        m = re.search(r"const PW_VERSION = '(\d+)\.(\d+)(?:\.\d+)?'", src)
        assert m is not None, "PW_VERSION literal not found in ensure-playwright.mjs"
        major, minor = int(m.group(1)), int(m.group(2))
        assert (major, minor) >= (1, 48), (
            f"PW_VERSION pins {major}.{minor}, but routeWebSocket needs >= 1.48; "
            "a lower pin makes ssrf-guard.mjs refuse every on-demand capture"
        )


class TestInstallSsrfGuardRuntime:
    """Drive the real installSsrfGuard through node (skipped when node absent)."""

    def test_rejects_without_route_websocket(self, node_verdicts):
        assert node_verdicts["without_route_websocket"] == "refused"

    def test_resolves_with_route_websocket(self, node_verdicts):
        assert node_verdicts["with_route_websocket"] == "installed"


class TestGetPlaywrightVersionGate:
    """A resolvable-but-too-old Playwright must NOT be returned.

    ssrf-guard.mjs refuses builds without routeWebSocket, so getPlaywright
    returning one (e.g. a cache populated under the former 1.47.2 pin) would
    permanently break capture with no self-heal. The gate must fall through to
    the install path instead.
    """

    def test_stale_cache_is_not_returned(self, node_verdicts):
        assert node_verdicts["stale_cache"] == "refused", (
            "getPlaywright returned a pre-1.48 cached build the SSRF guard must "
            "refuse; it should fall through to reinstall"
        )

    def test_usable_cache_is_returned(self, node_verdicts):
        assert node_verdicts["usable_cache"] == "returned"


class TestNodeBudget:
    def test_the_child_gets_half_the_running_test_timeout(self, tmp_path, monkeypatch):
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        monkeypatch.setattr(wait, "runner_timeout_secs", 180.0)
        monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/node")
        monkeypatch.setattr(subprocess, "run", fake_run)
        _run_node("process.exit(0)", tmp_path)
        assert captured["timeout"] == 90.0

    def test_a_child_past_its_deadline_fails_by_name(self, tmp_path, monkeypatch):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/node")
        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(pytest.fail.Exception, match="within 60s"):
            _run_node("process.exit(0)", tmp_path, 60.0)
