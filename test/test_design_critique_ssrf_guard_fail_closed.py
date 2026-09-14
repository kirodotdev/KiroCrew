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
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "src/kiro_crew/apps/builtins/design_critique/skills/design-critique/scripts"
SSRF_GUARD = SCRIPTS / "ssrf-guard.mjs"
ENSURE_PW = SCRIPTS / "ensure-playwright.mjs"


def _run_node(script: str, tmp_path: Path, env: dict[str, str] | None = None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    full_env = None
    if env is not None:
        import os

        full_env = {**os.environ, **env}
    return subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        cwd=tmp_path,
        env=full_env,
    )


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

    def test_rejects_without_route_websocket(self, tmp_path):
        script = f"""
            const {{ installSsrfGuard }} = await import({str(SSRF_GUARD.as_uri())!r})
            const target = {{ route: async () => {{}} }}
            try {{
                await installSsrfGuard(target, null)
                console.error('resolved but must reject')
                process.exit(1)
            }} catch (e) {{
                if (!String(e).includes('routeWebSocket')) {{
                    console.error('rejected with wrong error: ' + e)
                    process.exit(2)
                }}
            }}
        """
        proc = _run_node(script, tmp_path)
        assert proc.returncode == 0, proc.stderr

    def test_resolves_with_route_websocket(self, tmp_path):
        script = f"""
            const {{ installSsrfGuard }} = await import({str(SSRF_GUARD.as_uri())!r})
            const target = {{ route: async () => {{}}, routeWebSocket: async () => {{}} }}
            await installSsrfGuard(target, null)
        """
        proc = _run_node(script, tmp_path)
        assert proc.returncode == 0, proc.stderr


class TestGetPlaywrightVersionGate:
    """A resolvable-but-too-old Playwright must NOT be returned.

    ssrf-guard.mjs refuses builds without routeWebSocket, so getPlaywright
    returning one (e.g. a cache populated under the former 1.47.2 pin) would
    permanently break capture with no self-heal. The gate must fall through to
    the install path instead.
    """

    @staticmethod
    def _fake_cache(root: Path, version: str) -> Path:
        pkg = root / "node_modules" / "playwright"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"name": "playwright", "version": version}))
        (pkg / "index.js").write_text("module.exports = { chromium: {} }\n")
        return root

    def test_stale_cache_is_not_returned(self, tmp_path):
        cache = self._fake_cache(tmp_path / "cache", "1.47.2")
        script = f"""
            const {{ getPlaywright }} = await import({str(ENSURE_PW.as_uri())!r})
            const m = await getPlaywright({{ autoInstall: false }})
            process.exit(m === null ? 0 : 1)
        """
        proc = _run_node(script, tmp_path, env={"DC_PW_DIR": str(cache)})
        assert proc.returncode == 0, (
            "getPlaywright returned a pre-1.48 cached build the SSRF guard must "
            f"refuse; it should fall through to reinstall\n{proc.stderr}"
        )

    def test_usable_cache_is_returned(self, tmp_path):
        cache = self._fake_cache(tmp_path / "cache", "1.58.2")
        script = f"""
            const {{ getPlaywright }} = await import({str(ENSURE_PW.as_uri())!r})
            const m = await getPlaywright({{ autoInstall: false }})
            process.exit(m && m.chromium ? 0 : 1)
        """
        proc = _run_node(script, tmp_path, env={"DC_PW_DIR": str(cache)})
        assert proc.returncode == 0, proc.stderr
