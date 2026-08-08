"""Tests for kiro_crew.browser.setup — Playwright MCP setup.

``is_playwright_installed`` probes whether a launcher resolves (the same
resolution the proxy uses) and ``ensure_playwright_installed`` performs a real,
best-effort install of the public ``@playwright/mcp`` package plus the selected
engine's browser binary, bootstrapping Node when absent. The generic Netscape
cookie parsing, Playwright config generation and storage-state refresh work
regardless. The enterprise-SSO cookie/storage-state flow remains OSS-neutralized.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

import kiro_crew.browser.setup as setup_mod
from kiro_crew.browser.setup import (
    _converge_playwright_agent_files,
    _drop_superseded_playwright,
    _entry_is_playwright_proxy,
    check_playwright_launchable,
    converge_playwright_servers,
    deregister_playwright_proxy,
    ensure_playwright_installed,
    generate_playwright_config,
    get_playwright_mcp_args,
    inject_cookies_via_playwright,
    is_headed,
    is_playwright_installed,
    migrate_owned_playwright_registration,
    patch_mcp_extension,
    patch_mcp_headless,
    refresh_storage_state,
    register_playwright_proxy,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.platform_compat import IS_POSIX

# Canonical slash-free key KiroCrew registers the Playwright proxy under.
_CANONICAL = mcp_server_alias("@playwright/mcp")  # "playwright-mcp"

# ── Sample cookie data ────────────────────────────────────────────────────────

SAMPLE_COOKIES = """\
# Netscape HTTP Cookie File
sso.example.com\tFALSE\t/\tTRUE\t9999999999\tuser_name\ttestuser
#HttpOnly_.sso.example.com\tTRUE\t/\tTRUE\t9999999999\ttpm_metrics\teyJTdHVmZg==
"""


# ── TestIsPlaywrightInstalled ────────────────────────────────────────────────


class TestIsPlaywrightInstalled:
    def test_true_when_launcher_resolves(self, monkeypatch):
        # is_playwright_installed reflects check_playwright_launchable, which
        # reuses the proxy's own resolution — so a resolvable launcher reads True.
        monkeypatch.setattr(setup_mod, "check_playwright_launchable", lambda: (True, "/x/npx"))
        assert is_playwright_installed() is True

    def test_false_when_no_launcher(self, monkeypatch):
        monkeypatch.setattr(
            setup_mod, "check_playwright_launchable", lambda: (False, "not found")
        )
        assert is_playwright_installed() is False


# ── TestEnsurePlaywrightInstalled ────────────────────────────────────────────


class TestEnsurePlaywrightInstalled:
    def test_reports_node_missing(self, monkeypatch):
        # No Node and no bootstrap available -> a structured, non-raising failure
        # at the "node" step with an actionable hint (never a bare exception).
        monkeypatch.setattr(setup_mod, "ensure_node", lambda: None)
        result = ensure_playwright_installed()
        assert result["ok"] is False
        assert result["step"] == "node"
        assert result["engine"] == "chromium"

    def test_unknown_engine_falls_back_to_chromium(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "ensure_node", lambda: None)
        result = ensure_playwright_installed("netscape-navigator")
        assert result["engine"] == "chromium"

    def test_no_launcher_and_no_npx_fails_soft_with_docker_hint(self, monkeypatch):
        # Node present but neither a launcher binary nor npx resolves (an npm-free
        # Node). Do NOT hard-fail with a bare npm error: report the package step
        # with the two npm-free escape hatches (Docker image + KIROCREW_PLAYWRIGHT_CMD).
        monkeypatch.setattr(setup_mod, "ensure_node", lambda: "/usr/bin/node")
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda *a: None)
        result = ensure_playwright_installed("chromium")
        assert result["ok"] is False
        assert result["step"] == "package"
        assert "Docker" in result["detail"] and "KIROCREW_PLAYWRIGHT_CMD" in result["detail"]

    def test_never_runs_global_npm_install(self, monkeypatch):
        # The ecosystem launches @playwright/mcp via npx; a machine-global
        # `npm install -g` is neither needed nor run. Even on an npx-only host the
        # package step must prime via npx, never `npm install -g`.
        monkeypatch.setattr(setup_mod, "ensure_node", lambda: "/usr/bin/node")
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda *a: "/usr/bin/npx")
        monkeypatch.setattr(setup_mod, "_is_npx_launcher", lambda cmd: True)
        monkeypatch.setattr(setup_mod, "_playwright_binary_present", lambda base: False)
        monkeypatch.setattr(
            setup_mod, "find_node_tool", lambda name, path=None: f"/usr/bin/{name}"
        )
        monkeypatch.setattr(
            setup_mod, "_resolve_playwright_core_cli", lambda node, env: "/core/cli.js"
        )
        calls: list[list[str]] = []

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        monkeypatch.setattr(
            setup_mod.subprocess, "run", lambda argv, **k: calls.append(argv) or _Proc()
        )
        ensure_playwright_installed("chromium")
        assert not any("-g" in c for c in calls), "must never `npm install -g`"

    def test_npx_only_primes_cache_with_public_registry(self, monkeypatch):
        # On an npx-only host the cache is primed with one pinned fetch so the
        # first browse is not cold. The prime AND the browser install run with the
        # public registry pinned in the child env, so a private/stale-token .npmrc
        # can't 401 this public package.
        monkeypatch.setattr(setup_mod, "ensure_node", lambda: "/usr/bin/node")
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda *a: "/usr/bin/npx")
        monkeypatch.setattr(setup_mod, "_is_npx_launcher", lambda cmd: True)
        monkeypatch.setattr(setup_mod, "_playwright_binary_present", lambda base: False)
        monkeypatch.setattr(
            setup_mod, "find_node_tool", lambda name, path=None: f"/usr/bin/{name}"
        )
        monkeypatch.setattr(
            setup_mod, "_resolve_playwright_core_cli", lambda node, env: "/core/cli.js"
        )
        calls: list[list[str]] = []
        envs: list[dict] = []

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        def _fake_run(argv, **kwargs):
            calls.append(argv)
            envs.append(kwargs.get("env") or {})
            return _Proc()

        monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)
        result = ensure_playwright_installed("firefox")
        assert result["ok"] is True and result["engine"] == "firefox"
        # A prime fetch of @playwright/mcp@latest ran through npx.
        assert any(setup_mod._PLAYWRIGHT_MCP_NPM in c for c in calls if "/npx" in c[0])
        # Browser install ran node <core cli.js> install firefox.
        assert ["/usr/bin/node", "/core/cli.js", "install", "firefox"] in calls
        # Every child pinned the public registry.
        assert all(e.get("npm_config_registry") == setup_mod.PUBLIC_NPM_REGISTRY for e in envs)

    def test_already_launchable_binary_skips_all_fetching(self, monkeypatch):
        # A standalone binary already resolves -> detect-first skips the prime
        # fetch entirely; only the browser install runs through the bundled core.
        monkeypatch.setattr(setup_mod, "ensure_node", lambda: "/usr/bin/node")
        monkeypatch.setattr(
            setup_mod, "_resolve_playwright_cmd", lambda *a: "/usr/bin/mcp-server-playwright"
        )
        monkeypatch.setattr(setup_mod, "_is_npx_launcher", lambda cmd: False)
        monkeypatch.setattr(setup_mod, "_playwright_binary_present", lambda base: True)
        monkeypatch.setattr(
            setup_mod, "find_node_tool", lambda name, path=None: f"/usr/bin/{name}"
        )
        monkeypatch.setattr(
            setup_mod, "_resolve_playwright_core_cli", lambda node, env: "/core/cli.js"
        )
        calls: list[list[str]] = []

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        monkeypatch.setattr(
            setup_mod.subprocess, "run", lambda argv, **k: calls.append(argv) or _Proc()
        )
        result = ensure_playwright_installed("chromium")
        assert result["ok"] is True
        # No npx prime fetch; only the browser install through the bundled core.
        assert not any("/npx" in c[0] for c in calls)
        assert ["/usr/bin/node", "/core/cli.js", "install", "chromium"] in calls

    def test_browser_step_deferred_when_core_unresolvable(self, monkeypatch):
        # Launcher resolves but its bundled core is not yet on disk (npx host where
        # the prime was skipped/failed). Fail SOFT: @playwright/mcp downloads the
        # browser on first use, so the mode is still usable — ok=True with an
        # advisory browser-deferred step, NOT a hard failure.
        monkeypatch.setattr(setup_mod, "ensure_node", lambda: "/usr/bin/node")
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda *a: "/usr/bin/npx")
        monkeypatch.setattr(setup_mod, "_is_npx_launcher", lambda cmd: True)
        monkeypatch.setattr(setup_mod, "_playwright_binary_present", lambda base: False)
        monkeypatch.setattr(
            setup_mod, "find_node_tool", lambda name, path=None: f"/usr/bin/{name}"
        )
        monkeypatch.setattr(setup_mod, "_resolve_playwright_core_cli", lambda node, env: None)

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        monkeypatch.setattr(setup_mod.subprocess, "run", lambda argv, **k: _Proc())
        result = ensure_playwright_installed("chromium")
        assert result["ok"] is True
        assert result["step"] == "browser-deferred"

    def test_failed_browser_install_surfaces_cause_and_manual_command(self, monkeypatch):
        # The browser install exits NONZERO (offline, unwritable/full cache). Since
        # a headless browse then finds no executable, this is honestly ok=False. The
        # operator gets: (1) the ACTUAL cause (the error CODE is surfaced, not just
        # "it failed"), (2) a copy-pasteable manual command, and (3) sanitization —
        # credentials and local paths in stderr are scrubbed, never shown.
        monkeypatch.setattr(setup_mod, "ensure_node", lambda: "/usr/bin/node")
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda *a: "/usr/bin/npx")
        monkeypatch.setattr(setup_mod, "_is_npx_launcher", lambda cmd: True)
        monkeypatch.setattr(setup_mod, "_playwright_binary_present", lambda base: False)
        monkeypatch.setattr(
            setup_mod, "find_node_tool", lambda name, path=None: f"/usr/bin/{name}"
        )
        monkeypatch.setattr(
            setup_mod, "_resolve_playwright_core_cli", lambda node, env: "/core/cli.js"
        )

        class _Proc:
            returncode = 1
            # Stderr carrying a credential + local path alongside the E401 code. The
            # allowlist maps E401 to a fixed label; the raw text is NEVER echoed, so
            # the token/path/private-registry URL cannot leak regardless of line.
            stderr = (
                "npm verbose cwd /home/alice/proj\n"
                "npm error _authToken=npm_SECRETTOKEN1234567890abcd\n"
                "npm error https://user:pass@corp.jfrog.io/npm\n"
                "npm error code E401 Unable to authenticate; token may be invalid"
            )
            stdout = ""

        monkeypatch.setattr(setup_mod.subprocess, "run", lambda argv, **k: _Proc())
        result = ensure_playwright_installed("chromium")
        assert result["ok"] is False
        assert result["step"] == "browser"
        # (1) The actual cause is surfaced — the E401 code the operator must act on,
        #     via the fixed allowlist label (not echoed stderr).
        assert "E401" in result["detail"]
        assert result["reason"] and "E401" in result["reason"]
        # (2) The manual fallback command is offered as a structured field, with the
        #     public-registry pin; the prose points at it. Assert the EXACT expected
        #     command by equality — not a hostname/URL substring check, whose
        #     `host in string` shape CodeQL flags as incomplete-URL-sanitization.
        expected_cmd = (
            "npm install -g @playwright/mcp@latest --registry=https://registry.npmjs.org/"
        )
        assert setup_mod.MANUAL_INSTALL_CMD == expected_cmd  # exact command pinned
        assert result["manual_command"] == expected_cmd
        # Cross-platform --registry= FLAG (not a POSIX-only VAR= env prefix), and no
        # floating `playwright install` that could drift from the launcher's core.
        assert "npm_config_registry=" not in expected_cmd
        assert "playwright install" not in expected_cmd
        assert "command below" in result["detail"]
        # (3) NOTHING from raw stderr leaks: no token, no creds-URL, no local path.
        #     (Checks are absence-of-secret, not URL validation — the raw stderr is
        #     never echoed, so none of these fixture strings can appear.)
        for surfaced in (result["detail"], result["reason"]):
            assert "npm_SECRETTOKEN1234567890abcd" not in surfaced
            assert "user:pass@corp" not in surfaced  # the private-registry creds URL
            assert "/home/alice" not in surfaced

    def test_degraded_paths_never_dump_raw_secrets(self, monkeypatch):
        # No reachable install outcome dumps a raw secret/stacktrace. The no-Node /
        # no-launcher paths carry calm guidance; none leaks a credential or path.
        for setup_state in ("no_node", "no_launcher"):
            if setup_state == "no_node":
                monkeypatch.setattr(setup_mod, "ensure_node", lambda: None)
            else:
                monkeypatch.setattr(setup_mod, "ensure_node", lambda: "/usr/bin/node")
                monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda *a: None)
            result = ensure_playwright_installed("chromium")
            detail = result["detail"].lower()
            assert "traceback" not in detail and "_authtoken" not in detail
            assert "browser mode is on" in detail


class TestNpxCachePlaywrightRoots:
    """The core resolver must find an npx-primed @playwright/mcp, which lives under
    ``<npm cache>/_npx/<hash>/node_modules`` — neither ``npm root -g`` nor cwd."""

    def test_finds_primed_package_under_npx_cache(self, monkeypatch, tmp_path: Path):
        # Lay out a realistic npx cache: one hash dir holds @playwright/mcp, another
        # holds an unrelated package (must be ignored).
        cache = tmp_path / "npm-cache"
        good_nm = cache / "_npx" / "abc123" / "node_modules"
        (good_nm / "@playwright" / "mcp").mkdir(parents=True)
        (good_nm / "@playwright" / "mcp" / "package.json").write_text("{}")
        other_nm = cache / "_npx" / "def456" / "node_modules"
        (other_nm / "cowsay").mkdir(parents=True)

        class _Proc:
            returncode = 0
            stdout = str(cache)
            stderr = ""

        monkeypatch.setattr(
            setup_mod, "find_node_tool", lambda name, path=None: "/usr/bin/npm"
        )
        monkeypatch.setattr(setup_mod.subprocess, "run", lambda argv, **k: _Proc())
        roots = setup_mod._npx_cache_playwright_roots({"PATH": "/usr/bin"})
        assert roots == [str(good_nm)]

    def test_returns_empty_when_no_npx_cache(self, monkeypatch, tmp_path: Path):
        # No _npx dir under the cache -> empty, never raises.
        class _Proc:
            returncode = 0
            stdout = str(tmp_path / "empty-cache")
            stderr = ""

        monkeypatch.setattr(
            setup_mod, "find_node_tool", lambda name, path=None: "/usr/bin/npm"
        )
        monkeypatch.setattr(setup_mod.subprocess, "run", lambda argv, **k: _Proc())
        assert setup_mod._npx_cache_playwright_roots({"PATH": "/usr/bin"}) == []

    def test_orders_newest_cache_first(self, monkeypatch, tmp_path: Path):
        # Two primed caches; the resolver takes the first match, so the most
        # recently primed (newest package.json mtime) must sort first — else a
        # stale old revision could win over the one the runtime @latest uses.
        cache = tmp_path / "npm-cache"
        import os as _os

        def _prime(hash_name: str, mtime: float) -> str:
            nm = cache / "_npx" / hash_name / "node_modules"
            (nm / "@playwright" / "mcp").mkdir(parents=True)
            pkg = nm / "@playwright" / "mcp" / "package.json"
            pkg.write_text("{}")
            _os.utime(pkg, (mtime, mtime))
            return str(nm)

        old_nm = _prime("old111", 1_000.0)
        new_nm = _prime("new222", 2_000.0)

        class _Proc:
            returncode = 0
            stdout = str(cache)
            stderr = ""

        monkeypatch.setattr(
            setup_mod, "find_node_tool", lambda name, path=None: "/usr/bin/npm"
        )
        monkeypatch.setattr(setup_mod.subprocess, "run", lambda argv, **k: _Proc())
        roots = setup_mod._npx_cache_playwright_roots({"PATH": "/usr/bin"})
        assert roots == [new_nm, old_nm]


class TestResolvePlaywrightCoreCli:
    """Exercises the REAL Node ``require.resolve`` (not monkeypatched), because the
    bug it guards — playwright-core's ``exports`` map omits ``./cli.js`` so a direct
    subpath resolve throws ERR_PACKAGE_PATH_NOT_EXPORTED — is invisible to every
    test that stubs the resolver."""

    def test_resolves_core_cli_despite_exports_map(self, monkeypatch, tmp_path: Path):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        if not IS_POSIX:
            # Node's require.resolve over a SYNTHETIC scoped-package layout resolves
            # differently on native Windows (this fixture returns None there). The
            # production path is real npx/global caches that Node itself created,
            # which resolve fine cross-platform; the exports-map regression this
            # guards is OS-independent, so POSIX coverage is sufficient.
            pytest.skip("synthetic require.resolve layout is POSIX-only")
        # Synthetic node_modules mirroring the real hoisted npx layout: a
        # playwright-core whose exports EXCLUDE ./cli.js (the exact shape that
        # broke the old resolver), a sibling @playwright/mcp that depends on it.
        nm = tmp_path / "node_modules"
        core = nm / "playwright-core"
        core.mkdir(parents=True)
        (core / "package.json").write_text(
            json.dumps({"name": "playwright-core", "version": "1.0.0",
                        "exports": {"./package.json": "./package.json", ".": "./index.js"}})
        )
        (core / "cli.js").write_text("#!/usr/bin/env node\n")
        (core / "index.js").write_text("")
        mcp = nm / "@playwright" / "mcp"
        mcp.mkdir(parents=True)
        (mcp / "package.json").write_text(
            json.dumps({"name": "@playwright/mcp", "version": "0.0.1",
                        "exports": {"./package.json": "./package.json", ".": "./index.js"}})
        )
        (mcp / "index.js").write_text("")

        monkeypatch.setattr(setup_mod, "find_node_tool", lambda name, path=None: None)
        monkeypatch.setattr(setup_mod, "_npx_cache_playwright_roots", lambda env: [str(nm)])
        resolved = setup_mod._resolve_playwright_core_cli(node, {"PATH": os.environ.get("PATH", "")})
        # The old resolver returned None here (playwright-core's exports omit
        # ./cli.js). The fix resolves via package.json + join, so a real path comes
        # back. Compare by normalized identity, not raw string: Node emits native
        # separators and may canonicalize (e.g. Windows short/long paths, symlinked
        # temp dirs), so a byte compare is falsely brittle across platforms.
        assert resolved is not None, "core cli.js must resolve despite the exports map"
        assert os.path.samefile(resolved, str(core / "cli.js"))


class TestPinnedPlaywrightVersion:
    """The runtime proxy launches the version the prime recorded (not @latest), so
    launches are offline-deterministic and cannot drift past the provisioned
    browser revision."""

    def test_record_then_read_round_trips(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        nm = tmp_path / "cache" / "_npx" / "h1" / "node_modules"
        (nm / "@playwright" / "mcp").mkdir(parents=True)
        (nm / "@playwright" / "mcp" / "package.json").write_text(
            json.dumps({"name": "@playwright/mcp", "version": "0.0.78"})
        )
        monkeypatch.setattr(setup_mod, "_npx_cache_playwright_roots", lambda env: [str(nm)])
        setup_mod._record_primed_playwright_version({"PATH": "/usr/bin"})
        assert setup_mod.get_pinned_playwright_version() == "0.0.78"

    def test_absent_pin_reads_none(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        assert setup_mod.get_pinned_playwright_version() is None

    def test_tampered_version_is_rejected(self, monkeypatch, tmp_path: Path):
        # A non-semver / injected value must never reach an npx argv — degrade to
        # None (→ @latest) rather than launch attacker-controlled content.
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_npx_cache_playwright_roots", lambda env: [])
        (tmp_path / "playwright-mcp-version").write_text("latest; rm -rf /")
        assert setup_mod.get_pinned_playwright_version() is None

    def test_valid_but_uncached_version_is_rejected(self, monkeypatch, tmp_path: Path):
        # The flag file is agent-writable, so a prompt-injected shell could write a
        # valid-FORMAT but nonexistent semver (99.99.99). Launching it would fail to
        # fetch and break browsing persistently — a DoS. The on-disk-presence gate
        # rejects it (no cache holds that version) and falls back to @latest, so the
        # feature keeps working. The agent cannot fabricate a real cache dir.
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        nm = tmp_path / "cache" / "_npx" / "h1" / "node_modules"
        (nm / "@playwright" / "mcp").mkdir(parents=True)
        (nm / "@playwright" / "mcp" / "package.json").write_text(
            json.dumps({"name": "@playwright/mcp", "version": "0.0.78"})  # real cache = 0.0.78
        )
        monkeypatch.setattr(setup_mod, "_npx_cache_playwright_roots", lambda env: [str(nm)])
        (tmp_path / "playwright-mcp-version").write_text("99.99.99")  # attacker-written
        assert setup_mod.get_pinned_playwright_version() is None

    def test_non_object_cached_package_json_does_not_crash(self, monkeypatch, tmp_path: Path):
        # A cached package.json can be valid JSON that is NOT an object ([], "x", 12);
        # ``.get`` on it raises AttributeError, which OSError/ValueError would not
        # catch — crashing proxy startup. The type guard must degrade to None, not raise.
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        nm = tmp_path / "cache" / "_npx" / "h1" / "node_modules"
        (nm / "@playwright" / "mcp").mkdir(parents=True)
        (nm / "@playwright" / "mcp" / "package.json").write_text("[]")  # valid JSON, not a dict
        monkeypatch.setattr(setup_mod, "_npx_cache_playwright_roots", lambda env: [str(nm)])
        (tmp_path / "playwright-mcp-version").write_text("0.0.78")
        # Must return None (no match), never raise.
        assert setup_mod.get_pinned_playwright_version() is None
        # The recorder walks the same untrusted package.json; it must not raise either.
        setup_mod._record_primed_playwright_version({"PATH": "/usr/bin"})


# ── TestBrowserModePersistence ───────────────────────────────────────────────


class TestBrowserModePersistence:
    def test_enable_flag_round_trips(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        assert setup_mod.browser_mode_enabled() is False
        setup_mod.set_browser_mode_enabled(True)
        assert setup_mod.browser_mode_enabled() is True
        setup_mod.set_browser_mode_enabled(False)
        assert setup_mod.browser_mode_enabled() is False

    def test_engine_defaults_to_chromium(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        assert setup_mod.get_browser_engine() == "chromium"

    def test_engine_round_trips(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        setup_mod.set_browser_engine("webkit")
        assert setup_mod.get_browser_engine() == "webkit"

    def test_unknown_stored_engine_reads_chromium(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        (tmp_path / "browser-engine").write_text("mosaic")
        assert setup_mod.get_browser_engine() == "chromium"

    def test_set_unknown_engine_rejected(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(setup_mod, "config_dir", lambda: tmp_path)
        with pytest.raises(ValueError):
            setup_mod.set_browser_engine("mosaic")


# ── TestIsHeaded / TestGetPlaywrightMcpArgs ──────────────────────────────────


class TestIsHeaded:
    def test_headed_on_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        assert is_headed() is True

    def test_headless_on_linux(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        assert is_headed() is False

    def test_headed_on_windows(self, monkeypatch):
        # Windows has a desktop session and interactive SSO — run a visible
        # Chromium window like macOS, not the Linux headless mode.
        monkeypatch.setattr("platform.system", lambda: "Windows")
        assert is_headed() is True


class TestGetPlaywrightMcpArgs:
    def test_includes_headed_on_macos(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "is_headed", lambda: True)
        args = get_playwright_mcp_args()
        assert "--headed" in args
        assert "@playwright/mcp" in args

    def test_no_headed_on_linux(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "is_headed", lambda: False)
        args = get_playwright_mcp_args()
        assert "--headed" not in args
        assert "@playwright/mcp" in args


# ── TestInjectCookiesViaPlaywright ───────────────────────────────────────────


class TestInjectCookiesViaPlaywright:
    def test_returns_dict_with_cookies_and_count(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        assert "cookies" in result
        assert "count" in result

    def test_count_matches_cookies_length(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        assert result["count"] == len(result["cookies"])
        assert result["count"] == 2

    def test_parses_cookie_fields(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        names = {c["name"] for c in result["cookies"]}
        assert "user_name" in names
        assert "tpm_metrics" in names

    def test_default_path_used_when_no_cookie_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        result = inject_cookies_via_playwright()
        assert result["count"] == 2

    def test_missing_file_returns_empty_cookies(self, tmp_path: Path):
        missing = tmp_path / "no_such_cookie"
        result = inject_cookies_via_playwright(str(missing))
        assert result["cookies"] == []
        assert result["count"] == 0

    def test_httponly_cookie_parsed_correctly(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        httponly_cookies = [c for c in result["cookies"] if c.get("httpOnly")]
        assert len(httponly_cookies) == 1
        assert httponly_cookies[0]["name"] == "tpm_metrics"

    def test_empty_cookie_file_returns_zero_count(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text("# Netscape HTTP Cookie File\n# just comments\n")
        result = inject_cookies_via_playwright(str(p))
        assert result["count"] == 0
        assert result["cookies"] == []


# ── TestGeneratePlaywrightConfig ─────────────────────────────────────────────


class TestGeneratePlaywrightConfig:
    def test_creates_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        assert config_path.exists()

    def test_does_not_write_remote_debugging_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # B-minus dropped the CDP debug port — the live mirror now rides the
        # proxy's existing screenshot path, so no remote-debugging port is opened.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(generate_playwright_config().read_text(encoding="utf-8"))
        args = config["browser"]["launchOptions"]["args"]
        assert not any("remote-debugging-port" in a for a in args)

    def test_config_has_correct_structure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "browser" in config
        assert "capabilities" in config
        assert config["browser"]["browserName"] == "chromium"
        # A fresh home has no storage-state file, so storageState is omitted:
        # attaching a path to a missing file makes Playwright raise ENOENT at
        # context creation. contextOptions is still present (as an empty dict).
        # See #2209.
        assert "contextOptions" in config["browser"]
        assert "storageState" not in config["browser"]["contextOptions"]

    def test_storage_state_absolute_and_attached_when_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The config path now derives from config_dir(), which reads KIROCREW_HOME
        # first (the conftest autouse fixture pins it). Clear it so config_dir()
        # resolves from the patched Path.home -> ~/.kiro/crew under tmp_path.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # storageState is only attached when the file exists (#2209); seed it so
        # this exercises the present-branch and the path is absolute.
        state_file = config_dir() / "playwright-storage-state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        config_path = generate_playwright_config()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        storage_state = config["browser"]["contextOptions"]["storageState"]
        assert storage_state.startswith(str(tmp_path))
        assert "playwright-storage-state.json" in storage_state

    def test_storage_state_omitted_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Regression for #2209: with no storage-state file on disk (the OSS
        # default — refresh_storage_state() is a no-op), the generated config must
        # NOT reference it. Playwright raises ENOENT at context creation for a
        # storageState pointing at a missing file, which broke every browser_*
        # call on a stock install.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert not (config_dir() / "playwright-storage-state.json").exists()
        config = json.loads(generate_playwright_config().read_text(encoding="utf-8"))
        assert "storageState" not in config["browser"]["contextOptions"]

    def test_config_written_to_kirocrew_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Data home moved from top-level ~/.kirocrew to ~/.kiro/crew (config_dir()).
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        assert ".kiro/crew" in str(config_path)
        assert config_path.name == "playwright-config.json"

    def test_parent_dir_created_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        crew_dir = tmp_path / ".kiro" / "crew"
        assert not crew_dir.exists()
        generate_playwright_config()
        assert crew_dir.exists()

    def test_config_pins_chromium_channel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Without this pin @playwright/mcp defaults launchOptions.channel to the
        # branded "chrome" channel, which overrides browserName and is absent on
        # headless/Cloud Desktop hosts; pin it to bundled "chromium".
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(generate_playwright_config().read_text(encoding="utf-8"))
        assert config["browser"]["launchOptions"]["channel"] == "chromium"

    def test_config_runs_headless(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # The dashboard Browser panel mirror is the view surface, so the browser
        # runs headless — no visible OS window (and works on display-less Linux).
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(generate_playwright_config().read_text(encoding="utf-8"))
        assert config["browser"]["launchOptions"]["headless"] is True

    def test_engine_arg_selects_firefox_without_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A non-chromium engine sets browserName and omits ``channel`` entirely —
        # firefox/webkit are Playwright's own builds and reject a channel.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(
            generate_playwright_config("firefox").read_text(encoding="utf-8")
        )
        assert config["browser"]["browserName"] == "firefox"
        assert "channel" not in config["browser"]["launchOptions"]

    def test_engine_none_reads_persisted_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "get_browser_engine", lambda: "webkit")
        config = json.loads(generate_playwright_config().read_text(encoding="utf-8"))
        assert config["browser"]["browserName"] == "webkit"


# ── TestBrowseSetupHelpers (guided one-command setup) ────────────────────────


class TestCheckPlaywrightLaunchable:
    def test_ok_when_resolver_returns_cmd(self, monkeypatch: pytest.MonkeyPatch):
        # setup.py imports _resolve_playwright_cmd at module scope, so patch the
        # name where it is looked up (setup_mod), not on the origin module.
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda *a: "/usr/bin/npx")
        ok, detail = check_playwright_launchable()
        assert ok is True
        assert detail == "/usr/bin/npx"

    def test_not_ok_with_install_hint_when_unresolvable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda *a: None)
        ok, detail = check_playwright_launchable()
        assert ok is False
        assert "@playwright/mcp" in detail


class TestRegisterPlaywrightProxy:
    def test_creates_mcp_json_and_registers_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A fresh user has no ~/.kiro/settings/mcp.json; register creates it and
        # writes the canonical proxy entry so one command fully wires the panel.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: True)
        mcp_json = tmp_path / ".kiro" / "settings" / "mcp.json"
        assert not mcp_json.exists()
        returned, status = register_playwright_proxy()
        assert returned == mcp_json and mcp_json.exists()
        assert status == "registered"
        servers = json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]
        assert _CANONICAL in servers
        assert "mcp-playwright-proxy" in servers[_CANONICAL]["args"]

    def test_registers_into_existing_mcp_json_without_clobbering_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: True)
        mcp_json = _write_mcp_json(tmp_path, {"other-mcp": {"command": "foo"}})
        _, status = register_playwright_proxy()
        assert status == "registered"
        servers = json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]
        assert _CANONICAL in servers
        assert servers["other-mcp"] == {"command": "foo"}

    def test_keeps_user_direct_server_under_canonical_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A user hand-authored their OWN direct (non-proxy) server under the
        # canonical `playwright-mcp` key. `browse setup` must NOT overwrite it —
        # authorship is by launch target, not key name. Leave it byte-identical
        # and report kept-user-entry.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: True)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        mcp_json = _write_mcp_json(tmp_path, {_CANONICAL: dict(direct)})
        before = mcp_json.read_text(encoding="utf-8")
        _, status = register_playwright_proxy()
        assert status == "kept-user-entry"
        assert mcp_json.read_text(encoding="utf-8") == before


class TestDeregisterPlaywrightProxy:
    """Disabling Browser Mode removes the proxy so the browser_* tools disappear
    (tool availability is the gate now that there is no [BROWSE] marker)."""

    def test_removes_own_proxy_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        proxy = {"command": "kirocrew", "args": ["mcp-playwright-proxy", "--config", "/x"]}
        mcp_json = _write_mcp_json(tmp_path, {_CANONICAL: dict(proxy), "other": {"command": "f"}})
        _, status = deregister_playwright_proxy()
        assert status == "deregistered"
        servers = json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]
        assert _CANONICAL not in servers
        assert servers["other"] == {"command": "f"}, "unrelated server must survive"

    def test_keeps_user_direct_server(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # A user's own non-proxy server under the canonical key is left untouched.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        mcp_json = _write_mcp_json(tmp_path, {_CANONICAL: dict(direct)})
        before = mcp_json.read_text(encoding="utf-8")
        _, status = deregister_playwright_proxy()
        assert status == "kept-user-entry"
        assert mcp_json.read_text(encoding="utf-8") == before

    def test_absent_when_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        _, status = deregister_playwright_proxy()
        assert status == "absent"

    def test_remove_playwright_servers_scrubs_config_and_tool_refs(self):
        # remove_playwright_servers drops the proxy server AND its @<name> tool
        # references, but leaves other servers/tools intact.
        cfg = {
            "mcpServers": {
                _CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "other": {"command": "foo"},
            },
            "tools": ["@" + _CANONICAL, "@other", "web_fetch"],
            "allowedTools": ["@" + _CANONICAL],
        }
        assert setup_mod.remove_playwright_servers(cfg) is True
        assert _CANONICAL not in cfg["mcpServers"]
        assert cfg["mcpServers"]["other"] == {"command": "foo"}
        assert cfg["tools"] == ["@other", "web_fetch"]
        assert cfg["allowedTools"] == []

    def test_remove_playwright_servers_keeps_user_direct(self):
        # A user's own direct (non-proxy) server is not a proxy, so it survives.
        cfg = {"mcpServers": {_CANONICAL: {"command": "npx", "args": ["@playwright/mcp@latest"]}}}
        assert setup_mod.remove_playwright_servers(cfg) is False
        assert _CANONICAL in cfg["mcpServers"]


# ── TestRefreshStorageState ──────────────────────────────────────────────────


class TestRefreshStorageState:
    def test_returns_error_when_cookie_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        missing = tmp_path / "no_cookie"
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", missing)
        result = refresh_storage_state()
        assert result["ok"] is False
        # OSS build has no bundled browser-auth cookie source.
        assert "not available in OSS" in result["error"]

    def test_returns_error_when_no_cookies_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = tmp_path / "cookie"
        p.write_text("# Netscape HTTP Cookie File\n# just comments\n")
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        result = refresh_storage_state()
        assert result["ok"] is False
        assert "no cookies" in result["error"]

    def test_success_creates_storage_state_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        sso_dir = tmp_path / ".sso"
        sso_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        assert result["ok"] is True
        assert result["count"] == 2
        storage_path = Path(result["path"])
        assert storage_path.exists()

    def test_success_storage_state_valid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        sso_dir = tmp_path / ".sso"
        sso_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        storage_path = Path(result["path"])
        data = json.loads(storage_path.read_text(encoding="utf-8"))
        assert "cookies" in data
        assert "origins" in data
        assert len(data["cookies"]) == 2

    def test_success_returns_expired_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        sso_dir = tmp_path / ".sso"
        sso_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        assert "expired" in result
        assert isinstance(result["expired"], int)


# ── TestGetPlaywrightMcpArgsWithConfig ───────────────────────────────────────


class TestGetPlaywrightMcpArgsWithConfig:
    def test_includes_config_flag_when_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Data home moved to ~/.kiro/crew (config_dir()); clear KIROCREW_HOME so
        # config_dir() resolves from the patched Path.home under tmp_path.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "is_headed", lambda: False)
        # Create the config file
        config_path = tmp_path / ".kiro" / "crew" / "playwright-config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}")
        args = get_playwright_mcp_args()
        assert "--config" in args
        assert str(config_path) in args

    def test_no_config_flag_when_file_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "is_headed", lambda: False)
        args = get_playwright_mcp_args()
        assert "--config" not in args
        assert "@playwright/mcp" in args


# ── TestPatchWritesCanonicalKey ──────────────────────────────────────────────


def _write_mcp_json(tmp_path: Path, servers: dict) -> Path:
    """Seed ~/.kiro/settings/mcp.json under a monkeypatched home."""
    mcp_json = tmp_path / ".kiro" / "settings" / "mcp.json"
    mcp_json.parent.mkdir(parents=True, exist_ok=True)
    mcp_json.write_text(json.dumps({"mcpServers": servers}, indent=2))
    return mcp_json


def _read_servers(mcp_json: Path) -> dict:
    return json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]


class TestPatchWritesCanonicalKey:
    """patch_mcp_* register under the canonical alias and drop superseded keys."""

    def test_headless_writes_canonical_and_drops_superseded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                "@playwright/mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "npm:@playwright/mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "other-mcp": {"command": "foo"},
            },
        )
        patch_mcp_headless()
        servers = _read_servers(mcp_json)
        # Exactly the canonical key + the untouched user server survive.
        assert set(servers) == {_CANONICAL, "other-mcp"}
        assert "mcp-playwright-proxy" in servers[_CANONICAL]["args"]

    def test_extension_writes_canonical_and_drops_superseded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod.platform_compat, "chmod_safe", lambda *a, **k: None)
        mcp_json = _write_mcp_json(
            tmp_path,
            {"npm:@playwright/mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]}},
        )
        patch_mcp_extension("tok-123")
        servers = _read_servers(mcp_json)
        assert set(servers) == {_CANONICAL}
        assert servers[_CANONICAL]["env"]["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] == "tok-123"

    def test_drop_superseded_never_drops_canonical(self, tmp_path, monkeypatch):
        # A superseded key is dropped ONLY when its spec is actually the proxy.
        # (home -> tmp_path so no real ownership manifest colors the decision.)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        servers = {
            _CANONICAL: {"command": "kirocrew"},
            "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        assert _CANONICAL in servers
        assert "playwright-proxy-mcp" not in servers

    def test_drop_superseded_preserves_user_direct_server(self, tmp_path, monkeypatch):
        # A user-declared DIRECT (non-proxy) server keyed under a superseded name
        # (@playwright/mcp pointing at the real npm package) is NOT KiroCrew's and
        # must survive — authorship is by launch target, not key name. No manifest
        # marks it, so it is preserved.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        servers = {
            "@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]},
            "npm:@playwright/mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        # The proxy-spec superseded key is dropped; the user's direct one stays.
        assert "npm:@playwright/mcp" not in servers
        assert servers["@playwright/mcp"] == {
            "command": "npx",
            "args": ["@playwright/mcp@latest"],
        }

    def test_patch_records_owned_key_in_manifest(self, tmp_path, monkeypatch):
        # Arbiter regression (manifest-on-write): patch_mcp_* records the canonical
        # key it wrote in the KiroCrew-owned ownership manifest, so future
        # migrations have an explicit authorship signal (not just the argv
        # heuristic).
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        _write_mcp_json(tmp_path, {"other-mcp": {"command": "foo"}})
        patch_mcp_headless()
        assert _CANONICAL in setup_mod._load_owned_mcp_keys()
        # Manifest file is owner-only.
        if IS_POSIX:
            mode = stat.S_IMODE(setup_mod._owned_mcp_keys_path().stat().st_mode)
            assert mode == 0o600

    def test_drop_superseded_uses_manifest_over_mutated_spec(self, tmp_path, monkeypatch):
        # A superseded key recorded in the manifest is KiroCrew's even if its spec
        # was later mutated to no longer look like the proxy — the explicit
        # ownership marker wins over the argv heuristic.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        setup_mod._record_owned_mcp_key("playwright-proxy-mcp")
        servers = {
            _CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            # Spec no longer looks like the proxy, but the manifest says it's ours.
            "playwright-proxy-mcp": {"command": "wrapper", "args": ["--opaque"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        assert "playwright-proxy-mcp" not in servers

    def test_manifest_never_drops_unrecorded_user_direct_key(self, tmp_path, monkeypatch):
        # Defense: a manifest recording OTHER keys must not cause a user's direct
        # @playwright/mcp (not in the manifest, not a proxy) to be dropped.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        setup_mod._record_owned_mcp_key("playwright-proxy-mcp")
        servers = {
            "@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        assert servers["@playwright/mcp"] == {"command": "npx", "args": ["@playwright/mcp@latest"]}


class TestPatchMalformedMcpJson:
    """A user-owned mcp.json may hold valid JSON that isn't an object, or an
    mcpServers that isn't a dict. patch_mcp_* guarded only JSONDecodeError/OSError,
    so data.setdefault / servers[...] raised an uncaught AttributeError/TypeError.
    The patcher must reset the bad shape and still register the canonical key."""

    @pytest.mark.parametrize("patcher", ["extension", "headless"])
    @pytest.mark.parametrize("bad", ["[]", "null", '"hi"', "42", '{"mcpServers": []}'])
    def test_non_object_shape_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patcher: str, bad: str
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        mcp_json = tmp_path / ".kiro" / "settings" / "mcp.json"
        mcp_json.parent.mkdir(parents=True, exist_ok=True)
        mcp_json.write_text(bad)
        if patcher == "extension":
            patch_mcp_extension("tok-123")  # must not raise
        else:
            patch_mcp_headless()  # must not raise
        # The bad shape was reset and the canonical proxy key registered.
        servers = _read_servers(mcp_json)
        assert _CANONICAL in servers


# ── TestMigrateOwnedPlaywrightRegistration ───────────────────────────────────


class TestMigrateOwnedPlaywrightRegistration:
    def test_migrates_legacy_key_to_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: True)
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                "@playwright/mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                }
            },
        )
        migrate_owned_playwright_registration()
        servers = _read_servers(mcp_json)
        assert set(servers) == {_CANONICAL}

    def test_migrates_legacy_direct_npm_entry_to_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT 5.6 MEDIUM regression: KiroCrew's ORIGINAL boot migration upgraded a
        # legacy DIRECT npm-launched Playwright (key `npm:@playwright/mcp`, command
        # not the proxy) to the compression proxy. That direct->proxy upgrade must
        # still happen — the `npm:` key is a KiroCrew install artifact, so a direct
        # spec under it is ours to migrate (and remove), not left behind.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: True)
        mcp_json = _write_mcp_json(
            tmp_path,
            {"npm:@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]}},
        )
        migrate_owned_playwright_registration()
        servers = _read_servers(mcp_json)
        # Upgraded: canonical proxy present, the legacy direct entry removed.
        assert set(servers) == {_CANONICAL}
        assert "mcp-playwright-proxy" in servers[_CANONICAL]["args"]

    def test_drop_superseded_removes_legacy_direct_npm_key(self):
        # The `npm:@playwright/mcp` key is a KiroCrew artifact: its DIRECT spec is
        # dropped when KiroCrew rewrites its registration (so no second backend
        # lingers). The bare `@playwright/mcp` direct key is NOT (user may own it).
        servers = {
            "npm:@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]},
            "@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        assert "npm:@playwright/mcp" not in servers
        assert servers["@playwright/mcp"] == {"command": "npx", "args": ["@playwright/mcp@latest"]}

    def test_noop_when_already_canonical_no_churn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: True)
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                }
            },
        )
        before = mcp_json.read_text(encoding="utf-8")
        migrate_owned_playwright_registration()
        # Byte-identical: an already-canonical proxy is left untouched (no churn).
        assert mcp_json.read_text(encoding="utf-8") == before

    def test_does_not_add_when_no_playwright(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: True)
        mcp_json = _write_mcp_json(tmp_path, {"some-user-mcp": {"command": "foo"}})
        migrate_owned_playwright_registration()
        servers = _read_servers(mcp_json)
        assert set(servers) == {"some-user-mcp"}
        assert _CANONICAL not in servers

    def test_removes_proxy_on_boot_when_mode_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Browser Mode off at boot: a stale proxy left by a prior enable (or a
        # pre-upgrade install) must be removed, since registration is the
        # authorization and there is no [BROWSE] marker to gate it.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: False)
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                _CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy", "--config", "x"]},
                "some-user-mcp": {"command": "foo"},
            },
        )
        migrate_owned_playwright_registration()
        servers = _read_servers(mcp_json)
        assert _CANONICAL not in servers
        assert set(servers) == {"some-user-mcp"}

    def test_noop_when_mcp_json_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # No mcp.json, no agent dirs — must not raise.
        migrate_owned_playwright_registration()

    def test_leaves_user_direct_playwright_server_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # BLOCK-finding regression: a user's DIRECT @playwright/mcp entry in
        # kiro's mcp.json (a superseded KEY, but a non-proxy spec) must not be
        # rewritten or dropped by the boot-time convergence — its key name is
        # not proof of KiroCrew authorship.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        mcp_json = _write_mcp_json(tmp_path, {"@playwright/mcp": dict(direct)})
        before = mcp_json.read_text(encoding="utf-8")
        migrate_owned_playwright_registration()
        # Byte-identical: the user's direct server was left exactly as-is.
        assert mcp_json.read_text(encoding="utf-8") == before
        assert _read_servers(mcp_json)["@playwright/mcp"] == direct

    def test_leaves_user_direct_server_under_canonical_key_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT-review regression: a user's DIRECT (non-proxy) server keyed under
        # the CANONICAL `playwright-mcp` key must not be clobbered on boot. With
        # no superseded proxy to migrate, and canonical held by a user entry,
        # the guard must return before calling patch_mcp_* (which would do
        # servers[canonical] = proxy_entry and destroy the config every restart).
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}
        mcp_json = _write_mcp_json(tmp_path, {_CANONICAL: dict(direct)})
        before = mcp_json.read_text(encoding="utf-8")
        migrate_owned_playwright_registration()
        assert mcp_json.read_text(encoding="utf-8") == before
        assert _read_servers(mcp_json)[_CANONICAL] == direct

    def test_leaves_user_canonical_even_when_superseded_proxy_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Defense in depth: a user owns the canonical key with a direct server
        # AND a legacy proxy also exists. Migrating would overwrite the user's
        # canonical entry, so the guard leaves mcp.json untouched (the legacy
        # proxy is still folded for display/launch by the read/pool layers).
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "browser_mode_enabled", lambda: True)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                _CANONICAL: dict(direct),
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            },
        )
        before = mcp_json.read_text(encoding="utf-8")
        migrate_owned_playwright_registration()
        assert mcp_json.read_text(encoding="utf-8") == before


# ── TestConvergePlaywrightServers ────────────────────────────────────────────


class TestConvergePlaywrightServers:
    def test_collapses_canonical_and_legacy_by_target(self):
        cfg = {
            "mcpServers": {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                },
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "user-helper": {"command": "node", "args": ["h.js"]},
            },
            "tools": ["@playwright-proxy-mcp", "@other"],
            "allowedTools": [f"@{_CANONICAL}", "@playwright-proxy-mcp"],
        }
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL, "user-helper"}
        # Dropped @ref rewritten to canonical; allowedTools de-duped.
        assert cfg["tools"] == [f"@{_CANONICAL}", "@other"]
        assert cfg["allowedTools"] == [f"@{_CANONICAL}"]

    def test_renames_sole_legacy_to_canonical(self):
        cfg = {
            "mcpServers": {
                "playwright-proxy-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                },
            }
        }
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL}
        assert "--config" in cfg["mcpServers"][_CANONICAL]["args"]

    def test_noop_when_only_canonical(self):
        cfg = {
            "mcpServers": {_CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]}}
        }
        assert converge_playwright_servers(cfg) is False

    def test_noop_when_no_playwright(self):
        cfg = {"mcpServers": {"foo": {"command": "x"}}}
        assert converge_playwright_servers(cfg) is False

    def test_ignores_non_proxy_server_named_playwright(self):
        # A user server whose name contains "playwright" but does NOT launch the
        # proxy must not be matched or rewritten.
        spec = {"command": "node", "args": ["my-playwright-helper.js"]}
        assert _entry_is_playwright_proxy("my-playwright-helper", spec, _CANONICAL) is False
        cfg = {"mcpServers": {"my-playwright-helper": spec}}
        assert converge_playwright_servers(cfg) is False

    def test_preserves_user_direct_playwright_under_superseded_key(self):
        # BLOCK-finding regression: a user hand-declares a DIRECT (non-proxy)
        # @playwright/mcp server (the real npm package, a superseded KEY name).
        # Authorship is by launch target, so this is NOT collapsed/dropped even
        # though its key is in _SUPERSEDED_PLAYWRIGHT_KEYS.
        direct = {"command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}
        assert _entry_is_playwright_proxy("@playwright/mcp", direct, _CANONICAL) is False
        cfg = {"mcpServers": {"@playwright/mcp": direct}}
        assert converge_playwright_servers(cfg) is False
        assert cfg["mcpServers"]["@playwright/mcp"] == direct

    def test_collapses_proxy_but_keeps_user_direct_alongside(self):
        # A real proxy (legacy key) AND a user's direct @playwright/mcp coexist:
        # the proxy converges onto the canonical key; the user's direct server is
        # untouched.
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        cfg = {
            "mcpServers": {
                "@playwright/mcp": direct,
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            }
        }
        assert converge_playwright_servers(cfg) is True
        assert cfg["mcpServers"]["@playwright/mcp"] == direct
        assert _CANONICAL in cfg["mcpServers"]
        assert "playwright-proxy-mcp" not in cfg["mcpServers"]

    def test_user_direct_under_canonical_key_never_clobbered_by_legacy_proxy(self):
        # GPT 5.6 HIGH regression: a user's DIRECT (non-proxy) server occupies the
        # canonical `playwright-mcp` key while a legacy KiroCrew proxy sits under
        # another key. The survivor selection must NOT pick the non-proxy canonical
        # entry and delete the real proxy — that would silently destroy KiroCrew's
        # proxy. Since there is only one proxy and it can't move onto the user's
        # canonical slot, nothing collapses and the config is left untouched.
        user_direct = {"command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}
        proxy = {"command": "kirocrew", "args": ["mcp-playwright-proxy", "--config", "x"]}
        cfg = {
            "mcpServers": {
                _CANONICAL: user_direct,
                "playwright-proxy-mcp": proxy,
            }
        }
        assert converge_playwright_servers(cfg) is False
        # Both entries survive byte-identical: the user's canonical direct server
        # AND KiroCrew's proxy under its own legacy key.
        assert cfg["mcpServers"][_CANONICAL] == user_direct
        assert cfg["mcpServers"]["playwright-proxy-mcp"] == proxy

    def test_extension_survivor_wins_over_headless_when_extension_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT 5.6 MEDIUM regression: an active --extension entry and a stale
        # --config headless entry coexist. The headless entry has MORE args, so
        # arg-count alone would let it win and silently disable extension mode.
        # With extension mode enabled, the --extension entry must survive.
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: True)
        ext = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
        }
        headless = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/x/pw.json"],
        }
        cfg = {"mcpServers": {_CANONICAL: headless, "playwright-proxy-mcp": ext}}
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL}
        # The extension entry won despite having fewer args.
        assert "--extension" in cfg["mcpServers"][_CANONICAL]["args"]

    def test_headless_survivor_wins_over_extension_when_config_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Mirror: with extension mode OFF, the --config headless entry is the one
        # that matches the current mode and must survive.
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        ext = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--extension", "--foo", "--bar"],
        }
        headless = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/x/pw.json"],
        }
        cfg = {"mcpServers": {_CANONICAL: ext, "playwright-proxy-mcp": headless}}
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL}
        assert "--config" in cfg["mcpServers"][_CANONICAL]["args"]
        assert "--extension" not in cfg["mcpServers"][_CANONICAL]["args"]

    def test_survivor_is_most_complete_even_when_canonical_is_bare(self):
        # GPT 5.6 HIGH regression: a BARE canonical proxy coexists with a
        # fully-wired legacy proxy (--config/token). The survivor must be the
        # WIRED spec (never discard a working configuration for a bare
        # duplicate), stored under the canonical key.
        bare_canon = {"command": "kirocrew", "args": ["mcp-playwright-proxy"]}
        wired_legacy = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/x/pw.json"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
        }
        cfg = {
            "mcpServers": {
                _CANONICAL: bare_canon,
                "playwright-proxy-mcp": wired_legacy,
            }
        }
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL}
        # The wired spec survived under the canonical key; the bare one is gone.
        assert cfg["mcpServers"][_CANONICAL] == wired_legacy

    def test_dedupes_multiple_legacy_proxies_without_touching_user_canonical(self):
        # Two legacy proxies coexist with a user's direct server under canonical.
        # The two proxies must collapse to one (still under a legacy key, never
        # onto the user's canonical slot); the user's canonical entry is untouched.
        user_direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        proxy_full = {"command": "kirocrew", "args": ["mcp-playwright-proxy", "--config", "x"]}
        proxy_bare = {"command": "kirocrew", "args": ["mcp-playwright-proxy"]}
        cfg = {
            "mcpServers": {
                _CANONICAL: user_direct,
                "playwright-proxy-mcp": proxy_full,
                "npm:@playwright/mcp": proxy_bare,
            }
        }
        assert converge_playwright_servers(cfg) is True
        assert cfg["mcpServers"][_CANONICAL] == user_direct
        # Exactly one proxy survives (the more completely-wired one); the user's
        # canonical direct server is preserved alongside it.
        surviving_proxies = [
            n for n, s in cfg["mcpServers"].items() if "mcp-playwright-proxy" in s.get("args", [])
        ]
        assert len(surviving_proxies) == 1
        assert cfg["mcpServers"][surviving_proxies[0]] == proxy_full


# ── TestConvergePlaywrightAgentFiles ─────────────────────────────────────────


class TestConvergePlaywrightAgentFiles:
    def test_sweeps_kiro_and_cc_agent_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kiro_dir = tmp_path / ".kiro" / "agents"
        cc_dir = tmp_path / ".claude" / "agents"
        kiro_dir.mkdir(parents=True)
        cc_dir.mkdir(parents=True)
        dup = {
            "mcpServers": {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                },
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            }
        }
        kiro_file = kiro_dir / "kirocrew.json"
        cc_file = cc_dir / "kirocrew.mcp.json"
        kiro_file.write_text(json.dumps(dup))
        cc_file.write_text(json.dumps(dup))
        # A .bak file must be skipped (only the exact owned filenames are swept).
        (kiro_dir / "kirocrew.json.bak.123").write_text(json.dumps(dup))

        _converge_playwright_agent_files()

        assert set(json.loads(kiro_file.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}
        assert set(json.loads(cc_file.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}
        # The .bak file was NOT swept (still holds the duplicate).
        assert (
            "playwright-proxy-mcp"
            in json.loads((kiro_dir / "kirocrew.json.bak.123").read_text(encoding="utf-8"))[
                "mcpServers"
            ]
        )

    def test_no_error_when_dirs_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _converge_playwright_agent_files()  # must not raise

    def test_leaves_user_owned_agent_files_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT 5.6 HIGH regression: the sweep rewrites ONLY the EXACT filenames
        # KiroCrew generates (an explicit allowlist, not a ``kirocrew*`` prefix
        # glob). A user's own agent — including one they named
        # ``kirocrew-custom.json`` — may carry intentionally distinct proxy
        # entries; a restart must not collapse them and overwrite the user's file.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kiro_dir = tmp_path / ".kiro" / "agents"
        cc_dir = tmp_path / ".claude" / "agents"
        kiro_dir.mkdir(parents=True)
        cc_dir.mkdir(parents=True)
        dup = {
            "mcpServers": {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                },
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            }
        }
        # KiroCrew-owned (exact generated names): swept.
        owned = kiro_dir / "kirocrew.json"
        owned_variant = kiro_dir / "kirocrew-research.json"
        # User-owned: left alone — incl. a ``kirocrew``-PREFIXED custom name that
        # a prefix glob would wrongly have matched, and unrelated names.
        user_prefixed = kiro_dir / "kirocrew-custom.json"
        user_kiro = kiro_dir / "my-custom-agent.json"
        user_cc = cc_dir / "my-agent.mcp.json"
        for f in (owned, owned_variant, user_prefixed, user_kiro, user_cc):
            f.write_text(json.dumps(dup))

        _converge_playwright_agent_files()

        # KiroCrew-owned files converged to one server.
        assert set(json.loads(owned.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}
        assert set(json.loads(owned_variant.read_text(encoding="utf-8"))["mcpServers"]) == {
            _CANONICAL
        }
        # User-owned files byte-identical (both proxies preserved).
        assert (
            "playwright-proxy-mcp"
            in json.loads(user_prefixed.read_text(encoding="utf-8"))["mcpServers"]
        )
        assert (
            "playwright-proxy-mcp"
            in json.loads(user_kiro.read_text(encoding="utf-8"))["mcpServers"]
        )
        assert (
            "playwright-proxy-mcp" in json.loads(user_cc.read_text(encoding="utf-8"))["mcpServers"]
        )

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX permission bits only")
    def test_preserves_0600_file_mode_on_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT 5.6 HIGH regression: an agent config holding MCP env credentials may
        # be mode 0600. The convergence sweep's atomic write must NOT recreate it
        # with the umask default (0644) and expose secrets to other local users.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        dup = {
            "mcpServers": {
                _CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "playwright-proxy-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                    "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "secret"},
                },
            }
        }
        secret_file = kiro_dir / "kirocrew.json"
        secret_file.write_text(json.dumps(dup))
        os.chmod(secret_file, 0o600)

        _converge_playwright_agent_files()

        # Converged (one server left) AND still owner-only readable.
        assert set(json.loads(secret_file.read_text(encoding="utf-8"))["mcpServers"]) == {
            _CANONICAL
        }
        assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600


# ── TestConvergeKirocrewMcpJson ──────────────────────────────────────────────


class TestConvergeKirocrewMcpJson:
    """Arbiter regression: KiroCrew's own <data-home>/mcp.json is healed at the
    source, so a stale proxy key there isn't re-injected into the agent config on
    every rebuild for the per-rebuild backstop to undo forever."""

    def test_converges_stale_proxy_key_at_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        d = config_dir()
        f = d / "mcp.json"
        f.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        _CANONICAL: {
                            "command": "kirocrew",
                            "args": ["mcp-playwright-proxy", "--config", "x"],
                        },
                        "playwright-proxy-mcp": {
                            "command": "kirocrew",
                            "args": ["mcp-playwright-proxy"],
                        },
                    }
                }
            )
        )
        setup_mod._converge_kirocrew_mcp_json()
        # The stale duplicate proxy is gone at the source.
        assert set(json.loads(f.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}

    def test_noop_when_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        setup_mod._converge_kirocrew_mcp_json()  # must not raise

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX permission bits only")
    def test_preserves_file_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        d = config_dir()
        f = d / "mcp.json"
        f.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        _CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                        "playwright-proxy-mcp": {
                            "command": "kirocrew",
                            "args": ["mcp-playwright-proxy", "--config", "x"],
                        },
                    }
                }
            )
        )
        os.chmod(f, 0o600)
        setup_mod._converge_kirocrew_mcp_json()
        assert set(json.loads(f.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}
        assert stat.S_IMODE(f.stat().st_mode) == 0o600

    def test_leaves_user_direct_server_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A user's direct (non-proxy) server in <data-home>/mcp.json is not a
        # proxy, so convergence is a no-op and the file is left byte-identical.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        d = config_dir()
        f = d / "mcp.json"
        original = json.dumps(
            {"mcpServers": {"@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp"]}}}
        )
        f.write_text(original)
        setup_mod._converge_kirocrew_mcp_json()
        assert f.read_text(encoding="utf-8") == original


# ── TestConvergeDropLogging / owned-filename source of truth ──────────────────


class TestConvergeForensics:
    def test_dropped_spec_logged_with_env_values_redacted(self, caplog):
        # Arbiter regression: a dropped proxy's spec is logged in full (so a
        # wrongly-deleted entry can be reconstructed) but its env VALUES are
        # masked — a token like PLAYWRIGHT_MCP_EXTENSION_TOKEN never hits the log.
        import logging as _logging

        cfg = {
            "mcpServers": {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--extension"],
                },
                "playwright-proxy-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy"],
                    "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "super-secret-token"},
                },
            }
        }
        with caplog.at_level(_logging.INFO, logger="kiro_crew.browser.setup"):
            assert converge_playwright_servers(cfg) is True
        log_text = caplog.text
        # The dropped key + its arg wiring are diagnosable; the token is not.
        assert "playwright-proxy-mcp" in log_text
        assert "super-secret-token" not in log_text
        assert "PLAYWRIGHT_MCP_EXTENSION_TOKEN" in log_text  # key kept, value masked

    def test_redact_spec_for_log_masks_env_values_only(self):
        spec = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"TOK": "secret", "OTHER": "also-secret"},
        }
        safe = setup_mod._redact_spec_for_log(spec)
        assert safe["command"] == "kirocrew"
        assert safe["args"] == ["mcp-playwright-proxy", "--extension"]
        assert safe["env"] == {"TOK": "***", "OTHER": "***"}
        # Original spec is not mutated.
        assert spec["env"]["TOK"] == "secret"

    def test_owned_allowlist_is_the_leaf_module_source_of_truth(self):
        # Item 2 regression: the sweep's allowlist IS the agent_files leaf module
        # (not a hand-copied literal), so adding a managed spec in one place is
        # picked up here with no drift.
        from kiro_crew import agent_files

        assert setup_mod._OWNED_KIRO_AGENT_FILES is agent_files.OWNED_KIRO_AGENT_FILES
        assert setup_mod._OWNED_CC_AGENT_FILES is agent_files.OWNED_CC_AGENT_FILES
        # And agent.py's own filename constants come from the same leaf module.
        from kiro_crew import agent as agent_mod

        assert agent_mod.AGENT_FILENAME == agent_files.AGENT_FILENAME
        assert agent_mod.AGENT_FILENAME in agent_files.OWNED_KIRO_AGENT_FILES
