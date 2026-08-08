"""Playwright MCP browser setup (OSS stub).

The upstream build wired browser setup to a managed package installer and an
enterprise-SSO cookie/storage-state flow. In the open-source build those steps
are neutralized: every public symbol is preserved so importing modules keep
working, but SSO setup is a no-op and reports "not available in OSS".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.agent_files import OWNED_CC_AGENT_FILES, OWNED_KIRO_AGENT_FILES
from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser.auth import parse_netscape_cookies
from kiro_crew.config.paths import config_dir, kiro_agents_dir
from kiro_crew.env import ensure_node, find_node_tool, node_augmented_path
from kiro_crew.mcp_playwright_proxy import (
    PUBLIC_NPM_REGISTRY,
    _is_npx_launcher,
    _resolve_playwright_cmd,
)
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger(__name__)

# Browse engines Playwright can LAUNCH (its own browser build). "chromium" is
# the default and the only engine attach/extension mode supports; "firefox" and
# "webkit" are Playwright's own patched builds (not the user's Firefox/Safari)
# and are launch-only. Anything outside this set falls back to "chromium" — the
# value threads through generate_playwright_config() into Playwright's
# ``browserName``/``--browser``, and an unknown engine there would break launch.
BROWSER_ENGINES = ("chromium", "firefox", "webkit")
_DEFAULT_ENGINE = "chromium"

# Durable enable flag for Browser Mode, co-located with the extension-mode flag
# file so it survives a restart/update the same way (the prior per-session chat
# toggle did not persist at all). Presence = enabled.
_BROWSER_ENABLED_FLAG = "browser-mode-enabled"
# Durable record of the selected launch engine (one of BROWSER_ENGINES). Absent
# or unrecognized reads back as the chromium default.
_BROWSER_ENGINE_FILE = "browser-engine"
# Durable record of the exact ``@playwright/mcp`` version the enable-time prime
# resolved, so the runtime proxy launches that pinned version instead of the
# floating ``@latest`` (deterministic offline, no revision drift). Absent → the
# proxy falls back to ``@latest``. A plain semver string (validated on read).
_BROWSER_MCP_VERSION_FILE = "playwright-mcp-version"


def browser_mode_enabled() -> bool:
    """True when the operator has turned Browser Mode on in Settings.

    Durable across restart/update: the flag is a file under the data home, not
    per-session React state. This is the capability gate the chat send path and
    the agent's browse affordance key off — distinct from ``has_playwright_extension``
    (which only chooses the transport once Browser Mode is on).
    """
    return (config_dir() / _BROWSER_ENABLED_FLAG).exists()


def set_browser_mode_enabled(enabled: bool) -> None:
    """Persist the Browser Mode enable flag (durably, off any React state)."""
    flag = config_dir() / _BROWSER_ENABLED_FLAG
    flag.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        flag.touch()
    else:
        flag.unlink(missing_ok=True)


def get_browser_engine() -> str:
    """Return the selected launch engine, defaulting to chromium.

    An absent file or a value outside :data:`BROWSER_ENGINES` reads back as
    ``"chromium"`` so a hand-edited or stale file can never select an engine
    Playwright would reject at launch.
    """
    try:
        raw = (config_dir() / _BROWSER_ENGINE_FILE).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return _DEFAULT_ENGINE
    return raw if raw in BROWSER_ENGINES else _DEFAULT_ENGINE


def set_browser_engine(engine: str) -> None:
    """Persist the launch engine after validating it against BROWSER_ENGINES."""
    if engine not in BROWSER_ENGINES:
        raise ValueError(f"unknown browser engine: {engine!r}")
    path = config_dir() / _BROWSER_ENGINE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, engine)


# Optional test/override hook. Left ``None`` at import — NOT a
# ``config_dir()`` capture — so importing this module never triggers the
# one-time data-home migration as an import side effect (the migration must fire
# only at ``ensure_data_home()`` in the CLI prologue). Internal code resolves the
# cookie path through ``_cookie_path()``; tests that need a fixed path set this
# attribute (``monkeypatch.setattr(setup, "SSO_COOKIE_PATH", tmp)``).
SSO_COOKIE_PATH: "Path | None" = None


def _cookie_path() -> Path:
    """Resolve the cookie-jar path, honoring a test-set ``SSO_COOKIE_PATH``."""
    if SSO_COOKIE_PATH is not None:
        return SSO_COOKIE_PATH
    from kiro_crew.browser import auth as _auth

    return _auth.cookie_path()


# The public npm package name for the Playwright MCP server. Used only as the
# input to ``mcp_server_alias`` to derive the canonical slash-free key
# (``playwright-mcp``) KiroCrew registers the proxy under.
_PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp"

# Key names KiroCrew (or the predecessor install it descends from) historically
# registered the Playwright PROXY under. When KiroCrew (re)writes its own
# registration it converges these to the canonical ``mcp_server_alias`` form.
#
# IMPORTANT — a key name in this set is NOT proof of KiroCrew authorship. A user
# may hand-declare a *direct* (non-proxy) Playwright server under the public
# package name ``@playwright/mcp``. Authorship is decided ONLY by the resolved
# launch target (:func:`_spec_is_proxy` — the entry invokes
# ``mcp-playwright-proxy``); every drop/converge site gates on that, so a
# superseded key whose spec is a direct server is left untouched. This tuple is
# only the set of *candidate* names to inspect, never a standalone authorship
# signal — do not add a name here expecting it to be dropped by name alone.
# ``npm:@playwright/mcp`` is the legacy on-disk key earlier installs wrote; it is
# data to clean up FROM, not a key this module ever emits (both it and
# ``@playwright/mcp`` alias to the same canonical ``playwright-mcp``).
_SUPERSEDED_PLAYWRIGHT_KEYS = (
    "@playwright/mcp",
    "npm:@playwright/mcp",
    "playwright-proxy-mcp",
)

# The on-disk key earlier KiroCrew installs wrote for a DIRECT npm-launched
# Playwright server (before the compression proxy existed). Unlike the bare
# ``@playwright/mcp`` key — which a user may legitimately hand-author for their
# own direct server — the ``npm:`` prefix is a KiroCrew-generated artifact, so a
# *direct* spec under THIS specific key is KiroCrew's legacy entry and is safe to
# upgrade to the proxy and remove. (A proxy spec under any superseded key is
# handled by _drop_superseded_playwright.)
_LEGACY_DIRECT_PLAYWRIGHT_KEY = "npm:@playwright/mcp"

# EXACT filenames KiroCrew generates under ``~/.kiro/agents/`` (kiro specs) and
# ``~/.claude/agents/`` (the CC MCP sidecar). The convergence sweep rewrites ONLY
# these — an explicit allowlist, never a ``kirocrew*`` prefix glob, because a
# user is free to hand-author e.g. ``~/.kiro/agents/kirocrew-custom.json`` and a
# filename prefix does not prove KiroCrew authorship; rewriting it on a restart
# would corrupt the user's own config. Single source of truth is the leaf module
# ``agent_files`` (imported by both ``agent.py``, which WRITES these files, and
# here) so adding a managed spec is a one-line change in one place — no drift.
_OWNED_KIRO_AGENT_FILES = OWNED_KIRO_AGENT_FILES
_OWNED_CC_AGENT_FILES = OWNED_CC_AGENT_FILES


def is_playwright_installed() -> bool:
    """True when a ``@playwright/mcp`` launcher is resolvable on this host.

    Reuses the proxy's own resolution order via :func:`check_playwright_launchable`
    so this agrees with what the proxy would actually spawn (a
    ``mcp-server-playwright``/``playwright-mcp`` binary, a
    ``KIROCREW_PLAYWRIGHT_CMD`` override, or ``npx`` on PATH). Note ``npx`` being
    present means the package can be fetched on first use, not that it is already
    on disk; :func:`ensure_playwright_installed` performs the real install.
    """
    ok, _ = check_playwright_launchable()
    return ok


#: npm registry package name installed globally as the Playwright MCP launcher.
_PLAYWRIGHT_MCP_NPM = "@playwright/mcp@latest"


def _playwright_binary_present(base_path: str) -> bool:
    """True when a STANDALONE @playwright/mcp launcher binary resolves on PATH.

    Distinct from :func:`is_playwright_installed`, which also counts a bare
    ``npx`` (the on-demand fetcher) as resolvable. The installer's package step
    needs the stricter test: an npx-only host has NOT installed the package, so
    the global install must still run. An explicit ``KIROCREW_PLAYWRIGHT_CMD``
    override also counts as present (the operator pointed us at a real launcher).
    """
    if os.environ.get("KIROCREW_PLAYWRIGHT_CMD"):
        return True
    return any(
        find_node_tool(binary, base_path) for binary in ("mcp-server-playwright", "playwright-mcp")
    )


#: How long the npm install / browser provisioning subprocesses may run. Browser
#: binaries are large and fetched over the network, so this is generous.
_INSTALL_TIMEOUT_SECS = 600.0

#: The ONE canonical "browser not yet on disk, but that is fine" message. Every
#: not-yet-ready outcome (deferred here, the dashboard handler's exception
#: fallback) uses it verbatim so the operator never sees three different recovery
#: stories for the same underlying state.
BROWSER_FIRST_USE_NOTE = (
    "Browser Mode is on. The browser downloads automatically the first time the "
    "agent browses — that first visit may take a moment."
)

#: Copy-pasteable manual fallback surfaced when automatic provisioning fails.
#: Installs ONLY the launcher package (``@playwright/mcp``); it does NOT tack on a
#: ``playwright install`` browser fetch. Two reasons: (1) ``@playwright/mcp``
#: downloads the matching browser on first browse through its OWN bundled
#: ``playwright-core``, so a separate step is redundant; (2) a floating
#: ``npx playwright install`` is on an independent release cadence from the
#: launcher's bundled core, so it could fetch a browser revision the just-installed
#: launcher rejects ("Executable doesn't exist") — the exact drift the runtime
#: version pin exists to close. Uses the ``--registry=`` FLAG (not a ``VAR=value``
#: prefix, which native Windows cmd/PowerShell treats as an executable name) so the
#: copy-paste works on every OS from a shell whose ``.npmrc`` points at a private mirror.
MANUAL_INSTALL_CMD = (
    "npm install -g @playwright/mcp@latest --registry=https://registry.npmjs.org/"
)


#: Recognized npm/node/playwright failure codes → a short, human cause. This is
#: an ALLOWLIST by design: surfacing the actual reason must never risk leaking a
#: credential or URL that a free-form stderr line could carry, so we map to a
#: fixed label rather than echo arbitrary subprocess output. Ordered most-specific
#: first (E401 before the generic offline codes).
_KNOWN_INSTALL_ERRORS: tuple[tuple[str, str], ...] = (
    ("E401", "the npm registry rejected the request (E401 — auth/token invalid)"),
    ("E403", "the npm registry forbade the request (E403)"),
    ("EAI_AGAIN", "the network/DNS is unreachable (EAI_AGAIN — likely offline)"),
    ("ENOTFOUND", "the npm registry host could not be resolved (ENOTFOUND — likely offline)"),
    ("ETIMEDOUT", "the connection to the npm registry timed out (ETIMEDOUT)"),
    ("ECONNREFUSED", "the connection to the npm registry was refused (ECONNREFUSED)"),
    ("ENOSPC", "the disk is full (ENOSPC)"),
    ("EACCES", "a permission was denied writing the browser cache (EACCES)"),
    ("ERR_PNPM", "the package manager reported an error"),
)


def _sanitize_reason(stderr: str) -> str:
    """Map a subprocess's stderr to ONE short, SAFE cause label for the operator.

    Surfacing the actual cause is the point — an operator hitting a 401 or an
    offline error should see WHY, not just "it failed". But raw stderr is untrusted
    subprocess output that can carry credentials, tokens, or private-registry URLs
    on ANY line (npm prints `_authToken=`, `https://user:pass@mirror`, etc.), and
    the credential redactor does not catch every such shape. So this does NOT echo
    stderr text: it matches against a fixed ALLOWLIST of known npm/network/disk
    error codes and returns a hardcoded human label. An unrecognized failure
    returns ``""`` (the caller then shows the generic advisory + manual command
    without a specific cause) — never free-form subprocess output.
    """
    if not stderr:
        return ""
    for code, label in _KNOWN_INSTALL_ERRORS:
        if code in stderr:
            return label
    return ""


def ensure_playwright_installed(engine: str = _DEFAULT_ENGINE) -> dict[str, Any]:
    """Provision the public ``@playwright/mcp`` launcher and the engine's browser.

    Best-effort and never raises — returns a structured
    ``{"ok": bool, "step": str, "detail": str, "engine": str}`` so a caller (the
    Browser settings save handler) can report progress or an actionable failure
    in its JSON body rather than 500-ing. Steps, in order:

    1. **node** — ensure a usable Node via :func:`kiro_crew.env.ensure_node`
       (bootstraps it through the bundled ``ensure-node.sh`` when absent). Without
       Node nothing else can run, so this returns ``ok=False, step="node"`` with
       an install hint.
    2. **package** — make ``@playwright/mcp`` launchable the SAME way the proxy
       runs it, matching the ecosystem: no ``npm install -g``. Every MCP client
       (Claude Code, Codex, VS Code) launches it via ``npx @playwright/mcp`` — a
       machine-global install is neither recommended nor needed. So:
         - If a launcher already resolves (a standalone binary, ``npx``, or a
           ``KIROCREW_PLAYWRIGHT_CMD`` override), SKIP entirely — nothing to fetch.
         - Otherwise prime the ``npx`` cache with one pinned fetch so first use is
           fast and offline-safe. If neither ``npx`` nor a binary exists (npm-free
           host), FAIL SOFT: the mode still enables and the detail names the Docker
           image and ``KIROCREW_PLAYWRIGHT_CMD`` escape hatches, because a missing
           npm must not block a capability the operator can wire up another way.
    3. **browser** — ``playwright install <engine>`` to fetch the OS/arch browser
       binary; ``chromium`` is always safe, ``firefox``/``webkit`` pull
       Playwright's own builds. Skipped when the package step failed soft (there is
       no resolved launcher/core to provision against yet).

    Blocking (subprocess + network + disk) — MUST run off the event loop
    (``asyncio.to_thread``). Every spawned command inherits a Node-augmented PATH
    so a version-manager Node the daemon did not inherit is still found, and pins
    the public npm registry so a private/stale-token ``.npmrc`` can't 401 this
    public package.
    """
    if engine not in BROWSER_ENGINES:
        engine = _DEFAULT_ENGINE

    node = ensure_node()
    if not node:
        # Browser Mode is still ON (the proxy is registered); the browser tools
        # just can't run until Node is present. Calm, actionable note — never a
        # raw "install failed" error.
        return {
            "ok": False,
            "step": "node",
            "detail": (
                "Browser Mode is on. To finish setup, install Node.js "
                "(https://nodejs.org) — the agent's browser tools start working "
                "once it is available."
            ),
            "engine": engine,
        }

    aug_path = node_augmented_path(os.environ.get("PATH", ""))
    # Pin the public registry in every child (npm/npx shell out to fetch): a user's
    # default ``.npmrc`` may point at a private mirror with an expiring token, and
    # ``npm_config_registry`` is honored by npm and npx on every OS, so the fetch of
    # this PUBLIC package skips the private default and never 401s.
    run_env = {**os.environ, "PATH": aug_path, "npm_config_registry": PUBLIC_NPM_REGISTRY}

    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        # capture_output keeps npm/playwright chatter off the gateway's stdout;
        # the tail is surfaced only in the failure detail. text mode for logging.
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECS,
            env=run_env,
        )

    # Step 2 — make the launcher available WITHOUT a machine-global install, the
    # way the whole MCP ecosystem runs it (`npx @playwright/mcp`). Detect FIRST: if
    # the proxy can already resolve a launcher (standalone binary, npx, or the
    # KIROCREW_PLAYWRIGHT_CMD override), there is nothing to fetch — a re-enable or
    # a host that already has npx is a fast no-op, and this is the branch that skips
    # the download entirely on the common case that broke before. Resolve on the
    # AUGMENTED path (not the daemon's raw PATH) so a marker-bootstrapped Node
    # toolchain — npx alongside a Node the gateway did not inherit — is seen here
    # too, matching ensure_node / find_node_tool; otherwise this falsely reports
    # "no launcher" on exactly the host node_augmented_path exists to serve.
    launch_cmd = _resolve_playwright_cmd(aug_path)
    if launch_cmd is None:
        # Node is present but neither a launcher binary nor npx resolves (an
        # npm-free Node). Browser Mode stays ON (the proxy is registered); the
        # note offers the npm-free paths. Calm and actionable — never an error.
        return {
            "ok": False,
            "step": "package",
            "detail": (
                "Browser Mode is on. To finish setup, install npm (it ships with "
                "Node.js) so the browser tools can download, run the official "
                "Docker image (mcr.microsoft.com/playwright/mcp), or point "
                "KIROCREW_PLAYWRIGHT_CMD at your own launcher."
            ),
            "engine": engine,
        }
    npx_only = _is_npx_launcher(launch_cmd) and not _playwright_binary_present(aug_path)
    if npx_only:
        # Only npx resolves. Prime its cache with one pinned, public-registry fetch
        # so the package (and its bundled playwright-core, needed by step 3) is on
        # disk — the first real browse is then not a cold download, and it works
        # offline afterward. ``--help`` forces the fetch without starting the
        # long-lived server. A failure is NON-fatal: npx still fetches lazily at
        # launch, so fall through and let step 3 report if the browser is missing.
        try:
            _run([launch_cmd, "--yes", _PLAYWRIGHT_MCP_NPM, "--help"])
        except (subprocess.SubprocessError, OSError):
            pass
        # Pin the EXACT version the prime landed, so the runtime proxy launches
        # ``@playwright/mcp@<that version>`` instead of ``@latest``. Two failure
        # modes this closes, both flagged in review: (1) ``@latest`` re-resolves
        # against the registry on every launch, so an offline launch of a
        # supposedly-primed host fails; a pinned version resolves from the cache
        # with no round-trip. (2) When ``latest`` advances past what was primed,
        # the newer launcher's playwright-core can outrun the browser revision
        # provisioned here ("Executable doesn't exist"); a pinned version can't
        # drift. Best-effort — absent pin falls back to ``@latest`` at launch.
        _record_primed_playwright_version(run_env)

    # Step 3 — the OS/arch browser binary for the selected engine. Provision it
    # through the SAME playwright-core that ``@playwright/mcp`` bundles: Playwright
    # keys its browser cache by a per-version build REVISION, so a floating
    # ``npx playwright@latest`` could fetch a revision the bundled
    # ``playwright-core`` launcher rejects ("Executable doesn't exist") — a false
    # success. Resolving playwright-core relative to the resolved ``@playwright/mcp``
    # (global root OR the npx cache primed above) guarantees the downloaded revision
    # matches the launcher. ``install <engine>`` is idempotent; a present browser is
    # a fast no-op.
    node = find_node_tool("node", aug_path)
    core_cli = _resolve_playwright_core_cli(node, run_env) if node else None
    if not (node and core_cli):
        # The launcher resolves (step 2 passed) but its bundled core is not yet on
        # disk — an npx host where the prime is still warming. This is "not yet
        # fetched", NOT a failed fetch: defer softly (ok=True). A subsequent
        # re-save, once the cache is warm, runs the real install below.
        return _browser_deferred(engine)
    browser_argv = [node, core_cli, "install", engine]
    try:
        proc = _run(browser_argv)
    except (subprocess.SubprocessError, OSError) as exc:
        # The browser install was ATTEMPTED and failed (unwritable/full cache,
        # network drop). Do NOT report this as usable: without the executable a
        # headless browse errors, so a false ok=True would mislead. Surface a calm
        # ok=false advisory + the manual command; the full raw exception stays in
        # the log. The reason is an allowlisted code if the message carries one,
        # else EMPTY — never str(exc) (an OSError can embed a local path) and never
        # the raw exception class name (e.g. "TimeoutExpired" is dev jargon, not
        # operator guidance); an empty reason just omits the parenthetical.
        logger.warning("playwright browser install failed (%s)", exc)
        return _browser_incomplete(engine, reason=_sanitize_reason(str(exc)))
    if proc.returncode != 0:
        logger.warning(
            "playwright install %s exited %s: %s",
            engine, proc.returncode, proc.stderr.strip()[-300:],
        )
        return _browser_incomplete(engine, reason=_sanitize_reason(proc.stderr))

    return {"ok": True, "step": "done", "detail": "", "engine": engine}


def _browser_deferred(engine: str) -> dict[str, Any]:
    """Soft, usable outcome: the launcher works and the browser is not yet fetched.

    Returned when the browser was NOT attempted at enable time (the npx cache is
    still warming). ``ok=True``: the launcher resolves and the browser downloads on
    first use, so the UI shows a calm advisory. Copy is the ONE canonical
    "downloads on first use" line shared with the handler's exception fallback, so
    every not-yet-ready state tells the operator the same thing.
    """
    return {
        "ok": True,
        "step": "browser-deferred",
        "detail": BROWSER_FIRST_USE_NOTE,
        "engine": engine,
    }


def _browser_incomplete(engine: str, reason: str = "") -> dict[str, Any]:
    """Calm not-yet-usable outcome: the browser download was tried and did not finish.

    ``ok=False`` because, without the browser executable, a headless browse would
    fail — reporting it usable would mislead. The message is still calm, but it now
    surfaces the ACTUAL cause and a manual fallback so a determined failure (locked
    network, private-registry-only egress, broken npm) is not a dead end:

    - ``reason`` is a SAFE one-line cause from :func:`_sanitize_reason` (an
      allowlisted error-code label, never echoed stderr) or a short exception type
      — shown inline so the operator sees *why*, not a bare "it failed". Empty when
      the failure is unrecognized, in which case the reason clause is omitted.
    - ``manual_command`` is the copy-pasteable command to install it by hand, with
      the public registry pinned so it works from a shell whose ``.npmrc`` points at
      a private mirror. It is a STRUCTURED field (not embedded in the prose) so the
      dashboard renders it as its own ``<code>`` block; the prose just points at it.

    The full raw stderr stays in the log; only the allowlisted label reaches the UI.
    """
    detail = (
        "Browser Mode is on, but the browser download did not finish. It normally "
        "downloads automatically on the first browse; if it keeps failing, install "
        "it by hand with the command below."
    )
    if reason:
        detail = f"{detail}  (Reason: {reason}.)"
    return {
        "ok": False,
        "step": "browser",
        "detail": detail,
        "reason": reason,
        "manual_command": MANUAL_INSTALL_CMD,
        "engine": engine,
    }


#: Accept only a plain semver-ish token as a pinned version, so a corrupted or
#: hand-edited file can never inject shell/argv content into the launch spec.
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def _record_primed_playwright_version(run_env: dict[str, str]) -> None:
    """Persist the exact ``@playwright/mcp`` version the npx prime just landed.

    Reads the version from the newest primed cache's ``package.json`` and writes
    it to the ``playwright-mcp-version`` flag. Best-effort: any failure leaves no
    pin, and the proxy falls back to ``@latest`` — so this can only make launches
    MORE deterministic, never break one.
    """
    roots = _npx_cache_playwright_roots(run_env)  # newest-first
    for nm in roots:
        pkg = Path(nm) / "@playwright" / "mcp" / "package.json"
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # A cached package.json may parse to a non-object; guard before ``.get``.
        version = data.get("version", "") if isinstance(data, dict) else ""
        if isinstance(version, str) and _SEMVER_RE.match(version):
            try:
                atomic_write(config_dir() / _BROWSER_MCP_VERSION_FILE, version)
            except OSError:
                pass
            return


def get_pinned_playwright_version() -> str | None:
    """Return the pinned ``@playwright/mcp`` version, or ``None`` to use ``@latest``.

    Read by the proxy to build the launch spec. TWO gates, because the flag file is
    agent-writable (it is deliberately NOT a keystone — a wrong value only slows a
    fetch, it grants nothing):

    1. Strict semver on read, so a tampered value can never reach an npx argv as
       injection — a non-semver string degrades to ``None`` (→ ``@latest``).
    2. The version must ACTUALLY be present in a real npx/global cache on disk. A
       prompt-injected shell could write a valid-but-nonexistent semver (e.g.
       ``99.99.99``); launching ``@playwright/mcp@99.99.99`` would fail to fetch
       and break browsing persistently. Gating on on-disk presence neutralizes that
       DoS — a bogus version matches no cache, so we fall back to ``@latest`` and
       the feature keeps working. This is also the pin's whole point: it is only
       useful when that exact version is cached, so the guard and the optimization
       are the same check. The agent cannot fake a cache dir it did not fetch.
    """
    try:
        raw = (config_dir() / _BROWSER_MCP_VERSION_FILE).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if not _SEMVER_RE.match(raw):
        return None
    aug_path = node_augmented_path(os.environ.get("PATH", ""))
    run_env = {**os.environ, "PATH": aug_path}
    for nm in _npx_cache_playwright_roots(run_env):
        try:
            pkg = Path(nm) / "@playwright" / "mcp" / "package.json"
            data = json.loads(pkg.read_text(encoding="utf-8"))
            # A cached package.json is untrusted: it may parse to valid JSON that
            # is NOT an object (``[]``, ``"x"``, ``12``), on which ``.get`` raises
            # AttributeError — which OSError/ValueError would not catch, crashing
            # proxy startup. Guard the type; a non-dict just isn't a match.
            if isinstance(data, dict) and data.get("version") == raw:
                return raw
        except (OSError, ValueError):
            continue
    return None


def _npx_cache_playwright_roots(run_env: dict[str, str]) -> list[str]:
    """Node-module roots under the ``npx`` cache that hold a primed ``@playwright/mcp``.

    ``npx`` installs an on-demand package under ``<npm cache>/_npx/<hash>/
    node_modules``. That path is NEITHER ``npm root -g`` nor the gateway cwd, so
    a require.resolve that only searches those two misses an npx-primed copy —
    which is EXACTLY the npx-only host this rework targets. Return each such
    ``node_modules`` dir so the core resolver can find the package the prime
    fetched. Ordered NEWEST-FIRST by mtime: a host can accumulate several npx
    caches across versions, and the resolver takes the first match — so the most
    recently primed one (the one the just-run prime and the runtime ``@latest``
    launch agree on) must win over a stale older revision, else the browser
    install would fetch a revision the launcher then rejects. Best-effort:
    returns ``[]`` when the cache can't be located.
    """
    npm = find_node_tool("npm", run_env.get("PATH", ""))
    cache = ""
    if npm:
        try:
            cp = subprocess.run(
                [npm, "config", "get", "cache"],
                capture_output=True, text=True, timeout=30, env=run_env,
            )
            if cp.returncode == 0:
                cache = cp.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            cache = ""
    if not cache or cache == "undefined":
        # npm's default cache: ~/.npm on POSIX, %LocalAppData%/npm-cache on Windows.
        cache = os.path.expanduser("~/.npm")
    npx_dir = Path(cache) / "_npx"
    # (node_modules dir, mtime) pairs, so the newest primed cache resolves first.
    # Wrapped in try/except so a permission-denied or vanishing cache dir returns
    # [] rather than propagating — ensure_playwright_installed never raises.
    found: list[tuple[float, str]] = []
    try:
        for hash_dir in npx_dir.iterdir():
            nm = hash_dir / "node_modules"
            pkg = nm / "@playwright" / "mcp" / "package.json"
            if pkg.is_file():
                try:
                    mtime = pkg.stat().st_mtime
                except OSError:
                    mtime = 0.0
                found.append((mtime, str(nm)))
    except OSError:
        return []
    # Newest first: the most recently primed cache is the one the runtime launch
    # (@latest) will resolve, so its core revision matches.
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [nm for _, nm in found]


def _resolve_playwright_core_cli(node: str, run_env: dict[str, str]) -> str | None:
    """Resolve the ``playwright-core`` CLI bundled with the installed ``@playwright/mcp``.

    Asks Node to resolve ``playwright-core/cli`` from the location of the
    resolved ``@playwright/mcp`` package, so the browser install runs through the
    exact core the proxy launcher uses (matching build revisions). Returns the
    absolute ``cli.js`` path, or ``None`` when it cannot be resolved.

    Three resolution roots are searched, covering every way the package reaches
    disk: ``npm root -g`` (a global install, e.g. a user's own
    ``npm i -g @playwright/mcp``), the ``npx`` cache dirs
    (:func:`_npx_cache_playwright_roots` — the npx-only host this rework primes),
    and the local/default module path (a project-local copy).
    """
    roots: list[str] = []
    npm = find_node_tool("npm", run_env.get("PATH", ""))
    if npm:
        try:
            gr = subprocess.run(
                [npm, "root", "-g"], capture_output=True, text=True, timeout=30, env=run_env
            )
            if gr.returncode == 0 and gr.stdout.strip():
                roots.append(gr.stdout.strip())
        except (subprocess.SubprocessError, OSError):
            pass
    roots.extend(_npx_cache_playwright_roots(run_env))

    # From @playwright/mcp's package dir (resolved with the roots on the search
    # path), locate its playwright-core dependency's CLI entry. Stdout is the path
    # or empty. Roots are passed in via argv (JSON), not interpolated into the
    # script, so no path can break the JS string.
    #
    # Two correctness points:
    # 1. Resolve playwright-core's package.json and JOIN ``cli.js`` to its dir,
    #    rather than ``require.resolve('playwright-core/cli.js')`` directly:
    #    playwright-core ships an ``exports`` map that does NOT list ``./cli.js``,
    #    so a direct subpath resolve throws ERR_PACKAGE_PATH_NOT_EXPORTED under
    #    Node's exports enforcement even though the file is right there.
    #    ``./package.json`` is always exported, so resolving it and joining works.
    # 2. Node's ``paths`` option resolves each entry as ``<entry>/node_modules/…``.
    #    Our roots ARE the ``node_modules`` dirs (npm root -g, npx cache), so the
    #    script also adds each root's PARENT — covering both Node's strict
    #    "<parent>/node_modules" contract (needed on Windows) and its lenient
    #    direct-dir lookup (POSIX), so resolution is identical on every OS.
    script = (
        "const path=require('path');"
        "const raw=JSON.parse(process.argv[1]||'[]');"
        "const roots=[];"
        "for(const r of raw){roots.push(r);roots.push(path.dirname(r));}"
        "try{"
        "const mcp=require.resolve('@playwright/mcp/package.json',{paths:[...roots,process.cwd()]});"
        "const pc=require.resolve('playwright-core/package.json',"
        "{paths:[path.dirname(mcp),...roots]});"
        "process.stdout.write(path.join(path.dirname(pc),'cli.js'));"
        "}catch(e){process.stdout.write('');}"
    )
    try:
        proc = subprocess.run(
            [node, "-e", script, json.dumps(roots)],
            capture_output=True,
            text=True,
            timeout=30,
            env=run_env,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    out = proc.stdout.strip()
    return out if out and os.path.isfile(out) else None


def is_headed() -> bool:
    """Return True if browser should run in headed mode.

    Headed on macOS and Windows (a desktop user session is available and a
    visible Chromium window is preferred so users can complete interactive SSO
    prompts). Headless on Linux, where the gateway typically runs on a
    server without an accessible display.
    """
    return platform.system() in ("Darwin", "Windows")


def has_playwright_extension() -> bool:
    """Check if user has opted into Playwright Chrome extension mode.

    Extension mode attaches to the user's running Chrome (with all existing auth)
    instead of launching a separate headless browser, which reuses whatever
    session and extensions the real Chrome already has.
    """
    flag_file = config_dir() / "playwright-extension-mode"
    return flag_file.exists()


def get_extension_token() -> str | None:
    """Read the stored Playwright extension token."""
    token_file = config_dir() / "playwright-extension-token"
    if token_file.exists():
        return token_file.read_text().strip() or None
    return None


def get_playwright_mcp_env() -> dict[str, str]:
    """Return env vars needed for Playwright MCP (extension token if set)."""
    env: dict[str, str] = {}
    if has_playwright_extension():
        token = get_extension_token()
        if token:
            env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = token
    return env


def generate_playwright_config(engine: str | None = None) -> Path:
    """Generate ``<config_dir>/playwright-config.json`` with absolute paths.

    The open-source build ships a generic config with no enterprise
    auth-server allowlist. ``engine`` selects which browser Playwright LAUNCHES
    (one of :data:`BROWSER_ENGINES`); ``None`` reads the persisted selection via
    :func:`get_browser_engine` (default chromium). Only chromium sets a
    ``channel`` — firefox/webkit are Playwright's own builds with no channel.
    """
    engine = engine if engine in BROWSER_ENGINES else get_browser_engine()
    config_path = config_dir() / "playwright-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    storage_state = str(config_dir() / "playwright-storage-state.json")

    launch_options: dict[str, Any] = {
        # Run headless: the live mirror in the dashboard Browser panel is the
        # intended view surface, so a separate visible OS window is redundant
        # (and breaks on display-less Linux hosts). Auth is seeded via
        # ``storageState`` below, so no interactive SSO window is needed.
        "headless": True,
        "args": [],
    }
    # ``channel`` selects a branded Chromium distribution and is only meaningful
    # for the chromium engine; firefox/webkit reject it.
    if engine == "chromium":
        launch_options["channel"] = "chromium"

    # Only attach ``storageState`` when the file actually exists. Playwright
    # raises ENOENT at context creation if ``storageState`` points at a missing
    # file, and the sole writer of it — ``refresh_storage_state()`` — is a no-op
    # in the OSS build (no SSO cookie source), so on a stock install the file is
    # never created. Omitting it yields a clean, empty context instead of a
    # launch failure; if a cookie source is ever wired, the next config regen
    # picks the file up. See #2209.
    context_options: dict[str, Any] = {}
    if Path(storage_state).exists():
        context_options["storageState"] = storage_state

    config = {
        "browser": {
            "browserName": engine,
            "isolated": True,
            "launchOptions": launch_options,
            "contextOptions": context_options,
        },
        "capabilities": ["network", "storage"],
    }

    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def refresh_storage_state() -> dict[str, Any]:
    """Refresh the Playwright storage state from the browser cookie file.

    Reads cookies via the (OSS-stubbed) browser auth layer and writes them to
    a Playwright-compatible storage-state file. Returns a not-available result
    when no cookie source exists, which is the default in the open-source build.
    """
    cookie_path = _cookie_path()
    if not cookie_path.exists():
        return {"ok": False, "error": "browser auth not available in OSS"}

    cookies = parse_netscape_cookies(cookie_path)
    if not cookies:
        return {"ok": False, "error": "no cookies parsed"}

    storage_state_path = config_dir() / "playwright-storage-state.json"
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(storage_state_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"cookies": cookies, "origins": []}, f, indent=2)

    expired = [c for c in cookies if 0 < c.get("expires", -1) < time.time()]
    return {
        "ok": True,
        "path": str(storage_state_path),
        "count": len(cookies),
        "expired": len(expired),
    }


def get_playwright_mcp_args() -> list[str]:
    """Return Playwright MCP launch args based on platform and mode.

    Extension mode (--extension): attaches to user's running Chrome with existing auth.
    Config mode (--config): launches separate Chromium with cookie injection.
    """
    args = ["@playwright/mcp"]
    if has_playwright_extension():
        args.append("--extension")
        return args
    config_path = config_dir() / "playwright-config.json"
    if config_path.exists():
        args.extend(["--config", str(config_path)])
    if is_headed():
        args.append("--headed")
    return args


def _kirocrew_bin() -> str:
    """Resolve path to the kirocrew binary."""
    return shutil.which("kirocrew") or "kirocrew"


def _spec_is_proxy(spec: Any) -> bool:
    """True iff a server spec's resolved launch target is KiroCrew's proxy.

    The ONLY reliable authorship signal: the entry invokes
    ``mcp-playwright-proxy`` (which only KiroCrew registers). A key *name*
    matching a superseded key is NOT proof of authorship — a user may key a
    *direct* (non-proxy) Playwright server under the public package name
    ``@playwright/mcp``. That user entry must never be dropped or rewritten.
    """
    if not isinstance(spec, dict):
        return False
    args = spec.get("args") or []
    if not isinstance(args, list):
        return False
    return "mcp-playwright-proxy" in args


def _drop_superseded_playwright(servers: dict[str, Any], canonical: str) -> None:
    """Drop KiroCrew's own superseded Playwright entries from a servers dict.

    Operates in place; never drops the ``canonical`` key. Removes:

    * any superseded key recorded in the ownership MANIFEST (the authoritative
      "KiroCrew wrote this" signal), or whose spec is actually the KiroCrew proxy
      (``_spec_is_proxy`` — the launch-target fallback for pre-manifest installs);
      and
    * KiroCrew's legacy DIRECT entry under ``_LEGACY_DIRECT_PLAYWRIGHT_KEY``
      (``npm:@playwright/mcp``) even when it is a *direct* (non-proxy) spec —
      that ``npm:``-prefixed key is a KiroCrew install artifact, so once we write
      the canonical proxy the old direct entry is superseded and must be removed
      (otherwise it lingers as a second Playwright backend).

    A user-declared *direct* server under the BARE ``@playwright/mcp`` key (which
    a user may legitimately hand-author) is left untouched — it is neither in the
    manifest, nor a proxy spec, nor the ``npm:``-prefixed legacy key. Used when
    KiroCrew rewrites its own registration so the canonical (slash-free alias)
    entry is the only KiroCrew-authored one left behind.
    """
    owned = _load_owned_mcp_keys()
    for key in _SUPERSEDED_PLAYWRIGHT_KEYS:
        if key == canonical or key not in servers:
            continue
        if key in owned or _spec_is_proxy(servers[key]) or key == _LEGACY_DIRECT_PLAYWRIGHT_KEY:
            del servers[key]


def migrate_owned_playwright_registration() -> None:
    """Converge KiroCrew's own Playwright registration to one canonical server.

    Runs on gateway init. The Playwright proxy must be registered under the
    slash-free ``mcp_server_alias`` form (so kiro-cli can ``@``-reference it and
    the gateway does not derive a second pooled backend). Two KiroCrew-owned
    surfaces are converged, keyed by *resolved launch target* (an entry that
    invokes ``mcp-playwright-proxy``) plus KiroCrew's superseded keys:

    1. kiro's ``~/.kiro/settings/mcp.json`` — the browse entry KiroCrew
       co-manages — is rewritten to the canonical proxy entry. This also upgrades
       KiroCrew's legacy DIRECT ``npm:@playwright/mcp`` entry (written by installs
       that predate the compression proxy) to the proxy, preserving the original
       boot migration's direct-to-proxy behavior.
    2. KiroCrew's own ``~/.kiro/crew/mcp.json`` — the agent-specific MCP override
       merged into the agent config on every rebuild — is converged at the
       SOURCE, so a stale ``playwright-proxy-mcp`` key there is healed once rather
       than re-injected on every rebuild for the per-rebuild
       :func:`converge_playwright_servers` backstop to undo indefinitely.
    3. The KiroCrew-generated agent configs (the exact filenames in
       ``_OWNED_KIRO_AGENT_FILES`` / ``_OWNED_CC_AGENT_FILES``) are swept so any
       duplicate proxy entry (e.g. a legacy ``playwright-proxy-mcp``) collapses
       into the single canonical ``playwright-mcp``. This self-heals an existing
       machine on a plain gateway restart, without waiting for a full agent
       rebuild. Only the exact files KiroCrew writes are touched — a user's own
       custom agents in the same dirs are never rewritten.

    Never adds Playwright where none exists, never rewrites a user-declared
    server, and never mutates the user-owned discovery sources
    (``~/.claude.json``) — those converge for *display* on read (discovery
    canonicalization) and at launch (pool dedupe), not by mutating files.

    When Browser Mode is OFF, this instead REMOVES the proxy from every Kiro Crew
    surface: registration is the authorization, so a stale proxy left by a prior
    enable (or a pre-upgrade install) must not survive a restart into a mounted
    ``browser_*`` tool set while the durable toggle is off.
    """
    if not browser_mode_enabled():
        _remove_playwright_from_kirocrew_mcp_json()
        _remove_playwright_from_agent_files()
        with contextlib.suppress(OSError):
            deregister_playwright_proxy()
        return
    _migrate_owned_kiro_registration()
    _converge_kirocrew_mcp_json()
    _converge_playwright_agent_files()


def _migrate_owned_kiro_registration() -> None:
    """Rewrite KiroCrew's browse entry in kiro's ``mcp.json`` to the canonical key."""
    mcp_json = _kiro_mcp_json_path()
    # Fast-path bail BEFORE taking the lock: this migration never adds Playwright
    # where none exists, and _kiro_mcp_locked would otherwise create the settings
    # dir + lock sidecar on an install that has no kiro config at all.
    if not mcp_json.is_file():
        return
    # The read + decide + write must be ONE critical section: deciding from an
    # unlocked read and then writing lets a concurrent bridge/dashboard update
    # land in between, so the write is computed from a stale snapshot and drops
    # the other writer's entries.
    with _kiro_mcp_locked():
        if not mcp_json.is_file():
            return
        try:
            data = json.loads(mcp_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return
        canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
        canon_entry = servers.get(canonical)
        canon_is_proxy = _spec_is_proxy(canon_entry)
        # There are two KiroCrew-owned things worth migrating to the canonical
        # proxy:
        #   (a) a superseded PROXY entry (a duplicate proxy under a legacy key),
        #       and
        #   (b) KiroCrew's legacy DIRECT ``npm:@playwright/mcp`` entry — the key
        #       earlier KiroCrew installs wrote for a direct npm-launched
        #       Playwright (before the compression proxy existed). Upgrading it to
        #       the proxy is the ORIGINAL purpose of this boot migration; dropping
        #       it would leave existing users on the direct server with no
        #       compression.
        # A user-declared *direct* server under the BARE ``@playwright/mcp`` key
        # is NOT KiroCrew's (authorship is by launch target, not key name) and is
        # left untouched — only the ``npm:``-prefixed key is a KiroCrew legacy
        # artifact.
        superseded_proxy_present = any(
            key != canonical and _spec_is_proxy(servers.get(key))
            for key in _SUPERSEDED_PLAYWRIGHT_KEYS
        )
        legacy_direct = servers.get(_LEGACY_DIRECT_PLAYWRIGHT_KEY)
        legacy_direct_present = isinstance(legacy_direct, dict) and not _spec_is_proxy(
            legacy_direct
        )
        # Leave the file untouched unless there is a KiroCrew-owned entry to
        # migrate, AND the canonical slot is either empty or already our proxy
        # (safe to (re)write). If the canonical key holds a user-declared *direct*
        # (non-proxy) server, migrating would write servers[canonical] =
        # proxy_entry and clobber that user config on every boot — so skip.
        if not (superseded_proxy_present or legacy_direct_present):
            return
        if canon_entry is not None and not canon_is_proxy:
            return
        _patch_mcp_for_mode_unlocked()


def _converge_kirocrew_mcp_json() -> None:
    """Converge Playwright proxies in KiroCrew's own ``<data-home>/mcp.json``.

    ``rebuild_agent_config`` merges every server from this file into the agent
    config, so a stale duplicate proxy key here (e.g. a legacy
    ``playwright-proxy-mcp``) would be re-injected on EVERY rebuild — forcing the
    per-rebuild :func:`converge_playwright_servers` backstop to undo it forever.
    Healing it at the SOURCE here (this file is unambiguously KiroCrew-owned,
    unlike the deliberately-excluded user discovery source ``~/.claude.json``)
    makes the rebuild-time pass a true backstop rather than the primary cure.
    Mode-preserving atomic write; silently skips an unreadable/non-dict/absent
    file and a no-op convergence.
    """
    path = config_dir() / "mcp.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if not converge_playwright_servers(data):
        return
    try:
        prev_mode: int | None = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        prev_mode = None
    try:
        atomic_write(path, json.dumps(data, indent=2), mode=prev_mode)
    except OSError:
        pass


def _remove_playwright_from_kirocrew_mcp_json() -> bool:
    """Remove the Playwright proxy from Kiro Crew's own ``<data-home>/mcp.json``.

    The inverse of :func:`_converge_kirocrew_mcp_json`, for the Browser-Mode-off
    path: since ``rebuild_agent_config`` merges this source into the agent config,
    leaving the proxy here would re-mount the ``browser_*`` tools on the next
    rebuild. Mode-preserving atomic write; returns ``True`` iff it changed the
    file. Silently skips an unreadable/non-dict/absent file.
    """
    path = config_dir() / "mcp.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return False
    if not isinstance(data, dict) or not remove_playwright_servers(data):
        return False
    try:
        prev_mode: int | None = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        prev_mode = None
    try:
        atomic_write(path, json.dumps(data, indent=2), mode=prev_mode)
    except OSError:
        return False
    return True


def _entry_is_playwright_proxy(name: str, spec: Any, canonical: str) -> bool:
    """True iff a server entry is KiroCrew's Playwright proxy.

    Authorship is proven ONLY by the *resolved launch target* — the entry
    invokes ``mcp-playwright-proxy`` (:func:`_spec_is_proxy`). The key name is
    NOT a proof of authorship: a user may hand-declare a *direct* Playwright
    server under the public package name ``@playwright/mcp`` (a superseded key),
    and that entry must never be collapsed or dropped. The single exception is
    the canonical key when it already holds the proxy — but that is covered by
    the launch-target check too, so name matching is unnecessary.
    """
    return _spec_is_proxy(spec)


def _redact_spec_for_log(spec: Any) -> Any:
    """Return a shallow copy of *spec* safe to log: ``env`` VALUES masked, keys
    kept. Leaves ``command``/``args`` intact (an ``--extension``/``--config``
    wiring is diagnostic, not secret) so a dropped entry can be reconstructed
    from the log without exposing a token like ``PLAYWRIGHT_MCP_EXTENSION_TOKEN``.
    """
    if not isinstance(spec, dict):
        return spec
    safe = dict(spec)
    env = safe.get("env")
    if isinstance(env, dict):
        safe["env"] = {k: "***" for k in env}
    return safe


def converge_playwright_servers(config: dict) -> bool:
    """Collapse every KiroCrew Playwright-proxy entry in ``config`` to the single
    canonical ``playwright-mcp`` server. Mutates ``config`` in place; returns
    ``True`` iff anything changed.

    Convergence is keyed by resolved launch target (:func:`_spec_is_proxy` — the
    entry invokes ``mcp-playwright-proxy``), so two entries that launch the same
    proxy under different names (e.g. ``playwright-mcp`` and the legacy
    ``playwright-proxy-mcp``) become one. The survivor keeps the canonical key;
    when no canonical entry exists the most completely-wired proxy entry is
    renamed to it (never dropped). ``@<dropped>`` references in
    ``tools``/``allowedTools`` are rewritten to ``@playwright-mcp`` and
    de-duplicated. Never adds Playwright where none exists, and never touches a
    server whose spec is not the proxy — including a user-declared *direct*
    ``@playwright/mcp`` entry (identity is by launch target, not key name).
    Every collapse/rename is logged so a disappearing entry is diagnosable.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
    proxy_names = [n for n, s in servers.items() if _entry_is_playwright_proxy(n, s, canonical)]
    # Nothing to converge: no proxy entry, or exactly the single canonical one.
    if not proxy_names or proxy_names == [canonical]:
        return False

    # The user's recorded browse mode is the authority for which wiring should
    # win when two proxies are both configured: an ``--extension`` entry and a
    # stale ``--config`` headless entry both score as "wired", but arg-count
    # alone would let the headless one (more args) silently replace the active
    # extension entry and disable extension-mode browsing. Rank an entry matching
    # the current mode first, THEN by generic wired-ness, THEN by arg count.
    want_extension = has_playwright_extension()

    def _completeness(name: str) -> tuple[int, int, int]:
        spec = servers.get(name)
        args = (spec.get("args") or []) if isinstance(spec, dict) else []
        has_ext = "--extension" in args
        has_cfg = "--config" in args
        mode_match = 1 if (has_ext if want_extension else has_cfg) else 0
        wired = 1 if (has_ext or has_cfg) else 0
        return (mode_match, wired, len(args))

    # The SURVIVOR SPEC is the proxy that best matches the user's current mode
    # (falling back to the most completely-wired one) regardless of which key
    # currently owns it — so convergence never discards the active configuration
    # in favor of a stale or bare duplicate.
    survivor_spec = servers[max(proxy_names, key=_completeness)]

    # Pick the SURVIVOR KEY. The canonical key is used only when it is free or
    # already holds KiroCrew's proxy — never when it holds a user-declared
    # *direct* (non-proxy) server, or that user config would be clobbered.
    #   * canonical free / already-proxy  -> survivor lives at ``canonical``;
    #   * canonical occupied by a non-proxy user server -> survivor stays under
    #     the most-complete legacy proxy key so the user's canonical entry is
    #     untouched and we only collapse *duplicate* proxies onto it.
    canon_spec = servers.get(canonical)
    if canonical not in servers or _spec_is_proxy(canon_spec):
        target = canonical
    else:
        target = max(
            (n for n in proxy_names if n != canonical),
            key=_completeness,
        )

    dropped = [n for n in proxy_names if n != target]
    if not dropped:
        # Survivor already sits alone under ``target`` (a lone legacy proxy while
        # a user's direct server holds canonical) — nothing to collapse. Leaving
        # it in place is correct: never delete the last proxy, never move it onto
        # the user's canonical entry.
        return False
    # Log each dropped spec IN FULL (env VALUES redacted, keys kept) BEFORE
    # deleting it. Convergence is a destructive, unattended, every-restart path
    # whose survivor ranking depends on a live ``has_playwright_extension()``
    # probe; if that were ever transiently wrong it could drop a still-wanted
    # entry's args/env. Logging the whole spec (not just the key name) leaves a
    # forensic trail to reconstruct a wrongly-deleted entry — without ever
    # writing a token value to the log.
    dropped_specs = {n: _redact_spec_for_log(servers.get(n)) for n in dropped}
    for n in dropped:
        servers.pop(n, None)
    servers[target] = survivor_spec
    logger.info(
        "Converged Playwright proxy entries %s onto %r; dropped specs (env " "values redacted): %s",
        proxy_names,
        target,
        dropped_specs,
    )

    new_ref = f"@{target}"
    drop_refs = {f"@{n}" for n in dropped}
    for key in ("tools", "allowedTools"):
        lst = config.get(key)
        if isinstance(lst, list):
            config[key] = list(dict.fromkeys(new_ref if t in drop_refs else t for t in lst))
    return True


def remove_playwright_servers(config: dict) -> bool:
    """Remove EVERY Kiro Crew Playwright-proxy server from ``config`` and scrub its
    ``@<name>`` references out of ``tools``/``allowedTools``. Mutates in place;
    returns ``True`` iff anything changed.

    The inverse of :func:`converge_playwright_servers`, used when Browser Mode is
    turned OFF: a merged agent config that still carries the proxy would keep the
    ``browser_*`` tools mounted after the ACP loop reloads it via ``--agent``,
    even though the proxy was dropped from kiro's ``mcp.json``. Identity is by
    launch target (:func:`_spec_is_proxy`), so a user's own hand-authored *direct*
    ``@playwright/mcp`` server is left untouched.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
    proxy_names = [n for n, s in servers.items() if _entry_is_playwright_proxy(n, s, canonical)]
    if not proxy_names:
        return False
    for n in proxy_names:
        servers.pop(n, None)
    drop_refs = {f"@{n}" for n in proxy_names}
    for key in ("tools", "allowedTools"):
        lst = config.get(key)
        if isinstance(lst, list):
            config[key] = [t for t in lst if t not in drop_refs]
    logger.info("Removed Playwright proxy entries %s (Browser Mode disabled)", proxy_names)
    return True


def _owned_agent_config_files() -> list[Path]:
    """The EXACT agent-config files Kiro Crew generates that exist on disk.

    An explicit allowlist (``_OWNED_KIRO_AGENT_FILES`` under ``~/.kiro/agents/``,
    ``_OWNED_CC_AGENT_FILES`` under ``~/.claude/agents/``), never a ``kirocrew*``
    prefix glob: a user's OWN agents live in the same dirs and may carry
    intentionally distinct Playwright entries, so matching exact generated
    filenames is what keeps a rewrite from touching configs Kiro Crew did not
    author.
    """
    files: list[Path] = []
    kiro_dir = kiro_agents_dir()
    for name in _OWNED_KIRO_AGENT_FILES:
        p = kiro_dir / name
        if p.is_file():
            files.append(p)
    cc_dir = Path.home() / ".claude" / "agents"
    for name in _OWNED_CC_AGENT_FILES:
        p = cc_dir / name
        if p.is_file():
            files.append(p)
    return files


def _apply_to_owned_agent_files(transform: "Callable[[dict], bool]") -> None:
    """Apply ``transform`` (mutates in place, returns changed?) to each owned
    agent config, persisting only when it reports a change. Shared by the
    converge (Browser Mode on) and remove (Browser Mode off) sweeps so both get
    the same governance-sanitize + mode-preserving atomic write. Silently skips
    unreadable/non-dict/absent files.
    """
    for path in _owned_agent_config_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if not transform(data):
            continue
        try:
            # Governance floor: this rewrites allowedTools, so run the whole map
            # through the shared filter before persisting — a ceiling-governed
            # grant/autoApprove must not survive a sweep of an agent config that
            # Kiro Crew owns. No-op on an ungoverned host.
            try:
                from kiro_crew.platform.governance import sanitize_agent_config_governance

                sanitize_agent_config_governance(data)
            except Exception:  # noqa: BLE001 — never break the sweep on this
                logger.debug("governance sanitize unavailable during sweep", exc_info=True)
            # Preserve the file's existing permission bits: an agent config may
            # hold MCP ``env`` credentials and be mode 0600 — atomic_write would
            # otherwise recreate it with the umask default (commonly 0644),
            # exposing secrets to other local users after startup.
            try:
                prev_mode: int | None = stat.S_IMODE(path.stat().st_mode)
            except OSError:
                prev_mode = None
            # Atomic write: a live kiro-cli session reads kirocrew.json through
            # the agent-config path, so a torn write could be parsed as a corrupt
            # config. Rename-based replace makes the swap all-or-nothing.
            atomic_write(path, json.dumps(data, indent=2), mode=prev_mode)
        except OSError:
            pass


def _converge_playwright_agent_files() -> None:
    """Sweep the agent configs Kiro Crew generates, converging Playwright to one
    canonical server. Runs on gateway init so an existing machine self-heals on
    a plain restart.
    """
    _apply_to_owned_agent_files(converge_playwright_servers)


def _remove_playwright_from_agent_files() -> None:
    """Sweep the agent configs Kiro Crew generates, removing the Playwright proxy
    and its tool references. Runs when Browser Mode is disabled so the ``browser_*``
    tools do not stay mounted when the ACP loop reloads a merged ``--agent``
    config that still carried the proxy.
    """
    _apply_to_owned_agent_files(remove_playwright_servers)


# Sidecar manifest recording the MCP server keys KiroCrew itself has written.
# kiro-cli validates ~/.kiro/settings/mcp.json (and agent specs) with
# ``deny_unknown_fields``, so an in-spec ownership sentinel is impossible; the
# manifest lives OUT of band under the KiroCrew data home (a dir KiroCrew owns
# outright). It is the FIRST authorship signal for drop/converge decisions —
# the ``mcp-playwright-proxy`` launch-target heuristic remains the fallback
# for entries written by installs that predate this manifest.
_OWNED_MCP_KEYS_MANIFEST = "owned-mcp-keys.json"


def _owned_mcp_keys_path() -> Path:
    return config_dir() / _OWNED_MCP_KEYS_MANIFEST


def _load_owned_mcp_keys() -> set[str]:
    """Return the set of MCP keys KiroCrew has recorded writing (empty on any
    read/parse error — a missing/corrupt manifest just means fall back to the
    launch-target heuristic, never a crash)."""
    path = _owned_mcp_keys_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return set()
    keys = data.get("keys") if isinstance(data, dict) else None
    return {k for k in keys if isinstance(k, str)} if isinstance(keys, list) else set()


def _record_owned_mcp_key(key: str) -> None:
    """Record *key* as KiroCrew-written in the sidecar manifest (mode 0600).

    Idempotent and best-effort: a failure to persist the marker must never break
    the MCP write it accompanies (the launch-target heuristic still covers the
    entry), so all errors are swallowed.
    """
    try:
        current = _load_owned_mcp_keys()
        if key in current:
            return
        current.add(key)
        path = _owned_mcp_keys_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({"keys": sorted(current)}, indent=2), mode=0o600)
    except OSError:
        pass


def _kiro_mcp_json_path() -> Path:
    """Path to kiro's global MCP config — the file KiroCrew co-manages."""
    return Path.home() / ".kiro" / "settings" / "mcp.json"


@contextlib.contextmanager
def _kiro_mcp_locked() -> Iterator[None]:
    """Hold the exclusive advisory lock guarding kiro's global ``mcp.json``.

    Every writer of that file must serialize on the shared ``mcp.lock`` sidecar:
    the dashboard MCP handler (``handlers/mcp.py`` ``_McpFileLock``) and the app
    bridges (``apps/bridges.py``) already do. Writers coordinate ONLY if they all
    take this lock — a lock-free read-modify-write races the others and drops
    whichever side wrote first, losing that writer's server entries (an app's MCP
    server silently disappearing, or the browse entry vanishing).

    Blocking: callers on the event loop must dispatch through
    ``asyncio.to_thread``. Not reentrant — code already inside this block must
    call the ``_unlocked`` write helpers, never the public ``patch_mcp_*``
    wrappers (a second exclusive acquire on a fresh fd deadlocks the process
    against itself).
    """
    mcp_json = _kiro_mcp_json_path()
    mcp_json.parent.mkdir(parents=True, exist_ok=True)
    lock_path = mcp_json.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    # "r+" (not "r"): Windows msvcrt.locking requires a writable fd, and
    # platform_compat swallows the EACCES an "r" fd would raise — which would
    # silently degrade this to a no-op.
    with open(lock_path, "r+") as lf:
        with platform_compat.file_lock(lf.fileno(), exclusive=True):
            yield


def _patch_mcp_extension_unlocked(token: str) -> None:
    """Write the ``--extension`` proxy entry. Caller MUST hold ``_kiro_mcp_locked``."""
    mcp_json = _kiro_mcp_json_path()
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        # A user-owned mcp.json may hold valid JSON that isn't an object (e.g.
        # `[]`/`null`/a string after truncation or a hand-edit), or an
        # mcpServers that isn't a dict. data.setdefault / servers[...] would
        # then raise AttributeError/TypeError, which the except below does NOT
        # catch. Reset a bad shape to {} — matches _migrate_playwright_to_proxy.
        if not isinstance(data, dict):
            data = {}
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            servers = data["mcpServers"] = {}
        entry = {
            "command": _kirocrew_bin(),
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": token},
        }
        canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
        _drop_superseded_playwright(servers, canonical)
        servers[canonical] = entry
        mcp_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        platform_compat.chmod_safe(str(mcp_json), 0o600)
        _record_owned_mcp_key(canonical)
    except (json.JSONDecodeError, OSError):
        pass


def _patch_mcp_headless_unlocked() -> None:
    """Write the headless-config proxy entry. Caller MUST hold ``_kiro_mcp_locked``."""
    mcp_json = _kiro_mcp_json_path()
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        # See _patch_mcp_extension_unlocked: guard a non-object mcp.json /
        # non-dict mcpServers so setdefault/servers[...] can't raise an uncaught
        # AttributeError/TypeError.
        if not isinstance(data, dict):
            data = {}
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            servers = data["mcpServers"] = {}
        config_path = str(config_dir() / "playwright-config.json")
        entry = {
            "command": _kirocrew_bin(),
            "args": ["mcp-playwright-proxy", "--config", config_path],
        }
        canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
        _drop_superseded_playwright(servers, canonical)
        servers[canonical] = entry
        mcp_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _record_owned_mcp_key(canonical)
    except (json.JSONDecodeError, OSError):
        pass


def _patch_mcp_for_mode_unlocked() -> None:
    """Write the proxy entry matching the configured browse mode.

    Extension mode needs a token to be usable, so a flagged-but-tokenless install
    falls back to the headless config rather than writing an entry whose
    ``PLAYWRIGHT_MCP_EXTENSION_TOKEN`` is empty. Every registration path shares
    this dispatch so the mode decision cannot drift between them.

    Caller MUST hold ``_kiro_mcp_locked``.
    """
    if has_playwright_extension():
        token = get_extension_token() or ""
        if token:
            _patch_mcp_extension_unlocked(token)
            return
    _patch_mcp_headless_unlocked()


def patch_mcp_extension(token: str) -> None:
    """Update MCP config to use proxy with --extension and token env var.

    Takes the shared mcp.json lock. Blocking — do not call on the event loop.
    """
    with _kiro_mcp_locked():
        _patch_mcp_extension_unlocked(token)


def patch_mcp_headless() -> None:
    """Update MCP config to use proxy with headless mode config.

    Takes the shared mcp.json lock. Blocking — do not call on the event loop.
    """
    with _kiro_mcp_locked():
        _patch_mcp_headless_unlocked()


def check_playwright_launchable() -> tuple[bool, str]:
    """Best-effort check that a Playwright MCP launcher is resolvable.

    Reuses the proxy's own resolution order (``KIROCREW_PLAYWRIGHT_CMD`` →
    a ``mcp-server-playwright``/``playwright-mcp`` binary → ``npx``), so the
    check agrees with what the proxy would actually spawn. Returns
    ``(ok, detail)`` where ``detail`` is the resolved launcher, or an install
    hint when nothing is resolvable (e.g. Node/npm absent).
    """
    cmd = _resolve_playwright_cmd()
    if cmd is None:
        return (
            False,
            "not found — install Node.js then `npm i -g @playwright/mcp` "
            "(or ensure `npx` is on PATH)",
        )
    return True, cmd


def register_playwright_proxy() -> tuple[Path, str]:
    """Register KiroCrew's Playwright proxy in kiro's ``mcp.json``.

    Unlike the boot-time converge helpers, this is the explicit ``browse setup``
    entry point: it CREATES ``~/.kiro/settings/mcp.json`` when absent (so a fresh
    user gets a wired server from one command) and then writes the canonical
    proxy entry via the mode-appropriate patch (extension vs headless config).

    Returns ``(mcp_json_path, status)`` where ``status`` is ``"registered"``
    (KiroCrew's proxy was written/refreshed) or ``"kept-user-entry"`` (a
    user-authored NON-proxy server already holds the canonical ``playwright-mcp``
    key, so we left it untouched rather than clobber their config — authorship is
    by launch target, not key name, mirroring the boot-time migration guard).
    """
    mcp_json = _kiro_mcp_json_path()
    # Registration is the AUTHORIZATION: once the proxy is in mcp.json the
    # browser_* tools appear in the agent's tool list and it may operate a
    # browser. With no per-message marker, that must never happen while Browser
    # Mode is off — otherwise a setup/CLI path that registers unconditionally
    # would let ordinary chat operate a browser despite the durable toggle being
    # off. So this single chokepoint refuses to register when disabled; every
    # caller (dashboard save, `browse setup`, `kirocrew setup`) inherits the gate.
    if not browser_mode_enabled():
        return mcp_json, "mode-disabled"
    canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
    # Serialize with the other writers of this SAME file — see _kiro_mcp_locked.
    # The lock spans our read + create + write so a concurrent gateway/bridge
    # update can't be clobbered (which would drop its server entries).
    with _kiro_mcp_locked():
        if mcp_json.exists():
            try:
                existing = json.loads(mcp_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
            servers = existing.get("mcpServers") if isinstance(existing, dict) else None
            canon = servers.get(canonical) if isinstance(servers, dict) else None
            # A user may hand-author their OWN direct (non-proxy) server under
            # the canonical key. The patch helpers would overwrite it, silently
            # losing their config — so leave it untouched and report back.
            if canon is not None and not _spec_is_proxy(canon):
                return mcp_json, "kept-user-entry"
        else:
            mcp_json.write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")
        _patch_mcp_for_mode_unlocked()
    return mcp_json, "registered"


def deregister_playwright_proxy() -> tuple[Path, str]:
    """Remove the Kiro Crew Playwright proxy from kiro's ``mcp.json``.

    The inverse of :func:`register_playwright_proxy`, called when the operator
    turns Browser Mode OFF. With no per-message marker, tool AVAILABILITY is the
    gate: dropping the proxy entry makes the ``browser_*`` tools disappear from
    the agent's tool list, so "off" actually prevents browser operation. Removes
    ONLY a proxy entry authored by Kiro Crew (identified by launch target via
    :func:`_spec_is_proxy`, plus the superseded legacy keys); a user's own
    hand-authored direct server under the canonical key is left untouched.

    Also removes the proxy (and its ``@playwright-mcp`` tool references) from the
    Kiro Crew ``<data-home>/mcp.json`` source and the generated agent-config
    files, so a restarted ACP loop cannot reload a merged ``--agent`` config that
    still mounts the ``browser_*`` tools.

    Returns ``(mcp_json_path, status)`` where ``status`` is ``"deregistered"``
    (an entry was removed somewhere), ``"absent"`` (nothing anywhere to remove),
    or ``"kept-user-entry"`` (the canonical key in kiro's mcp.json holds a user's
    non-proxy server, left untouched). Takes the shared lock; blocking, so keep
    it off the event loop.
    """
    mcp_json = _kiro_mcp_json_path()
    canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
    status = "absent"
    kept_user_entry = False
    with _kiro_mcp_locked():
        if mcp_json.is_file():
            try:
                data = json.loads(mcp_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            if isinstance(servers, dict):
                canon = servers.get(canonical)
                # A user's own DIRECT server under the canonical key is left
                # untouched, but we must NOT abort here: a Kiro Crew proxy can
                # still sit under a superseded key in this file, and — regardless
                # of this file — in the data-home source and the generated agent
                # configs, all of which must still be swept below. Authorship is
                # by launch target, so the sweeps never touch the user's entry.
                user_owns_canonical = canon is not None and not _spec_is_proxy(canon)
                kept_user_entry = user_owns_canonical
                removed = False
                if _spec_is_proxy(canon):
                    del servers[canonical]
                    removed = True
                before = len(servers)
                _drop_superseded_playwright(servers, canonical)
                if removed or len(servers) != before:
                    try:
                        prev_mode: int | None = stat.S_IMODE(mcp_json.stat().st_mode)
                    except OSError:
                        prev_mode = None
                    atomic_write(mcp_json, json.dumps(data, indent=2), mode=prev_mode)
                    status = "deregistered"
    # Sweep the Kiro Crew mcp.json SOURCE and the generated agent configs too:
    # rebuild_agent_config merges the source proxy into the agent config, and the
    # ACP loop loads that via --agent, so a stale proxy there would keep the
    # tools mounted after a restart even once kiro's mcp.json is clean.
    if _remove_playwright_from_kirocrew_mcp_json():
        status = "deregistered"
    _remove_playwright_from_agent_files()
    # Report the user-entry carve-out only when it was the sole finding — a real
    # Kiro Crew proxy removed anywhere else takes precedence in the status.
    if status == "absent" and kept_user_entry:
        return mcp_json, "kept-user-entry"
    return mcp_json, status


def inject_cookies_via_playwright(cookie_file: str | None = None) -> dict[str, Any]:
    """Parse the browser cookie file and return cookies in Playwright format.

    Args:
        cookie_file: Path to Netscape cookie file. Defaults to the resolved
            browser cookie path (``_cookie_path()``).

    Returns:
        Dict with "cookies" list and "count" integer.
    """
    path = Path(cookie_file) if cookie_file is not None else _cookie_path()
    cookies = parse_netscape_cookies(path)
    return {"cookies": cookies, "count": len(cookies)}
