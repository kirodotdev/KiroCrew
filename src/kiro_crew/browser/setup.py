"""Playwright MCP browser setup (OSS stub).

The upstream build wired browser setup to a managed package installer and an
enterprise-SSO cookie/storage-state flow. In the open-source build those steps
are neutralized: every public symbol is preserved so importing modules keep
working, but SSO setup is a no-op and reports "not available in OSS".
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.agent_files import OWNED_CC_AGENT_FILES, OWNED_KIRO_AGENT_FILES
from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser.auth import parse_netscape_cookies
from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger(__name__)

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
    """Check whether the Playwright MCP package is resolvable on PATH (OSS stub).

    The managed package manager that originally backed this check is not
    available in the open-source build, so this returns False gracefully.
    """
    return False


def ensure_playwright_installed() -> None:
    """Browser setup is not available in the open-source build (no-op stub).

    The upstream flow installed Playwright MCP via a managed package manager and
    wired enterprise-SSO cookie injection. Neither is shipped in OSS, so this is
    a no-op rather than raising.
    """
    return None


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


def generate_playwright_config() -> Path:
    """Generate ``<config_dir>/playwright-config.json`` with absolute paths.

    The open-source build ships a generic Chromium config with no
    enterprise auth-server allowlist.
    """
    config_path = config_dir() / "playwright-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    storage_state = str(config_dir() / "playwright-storage-state.json")

    config = {
        "browser": {
            "browserName": "chromium",
            "isolated": True,
            "launchOptions": {
                "channel": "chromium",
                "args": [],
            },
            "contextOptions": {
                "storageState": storage_state,
            },
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
    2. KiroCrew's own ``~/.kirocrew/mcp.json`` — the agent-specific MCP override
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
    """
    _migrate_owned_kiro_registration()
    _converge_kirocrew_mcp_json()
    _converge_playwright_agent_files()


def _migrate_owned_kiro_registration() -> None:
    """Rewrite KiroCrew's browse entry in kiro's ``mcp.json`` to the canonical key."""
    mcp_json = Path.home() / ".kiro" / "settings" / "mcp.json"
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
    # There are two KiroCrew-owned things worth migrating to the canonical proxy:
    #   (a) a superseded PROXY entry (a duplicate proxy under a legacy key), and
    #   (b) KiroCrew's legacy DIRECT ``npm:@playwright/mcp`` entry — the key
    #       earlier KiroCrew installs wrote for a direct npm-launched Playwright
    #       (before the compression proxy existed). Upgrading it to the proxy is
    #       the ORIGINAL purpose of this boot migration; dropping it would leave
    #       existing users on the direct server with no compression.
    # A user-declared *direct* server under the BARE ``@playwright/mcp`` key is
    # NOT KiroCrew's (authorship is by launch target, not key name) and is left
    # untouched — only the ``npm:``-prefixed key is a KiroCrew legacy artifact.
    superseded_proxy_present = any(
        key != canonical and _spec_is_proxy(servers.get(key))
        for key in _SUPERSEDED_PLAYWRIGHT_KEYS
    )
    legacy_direct = servers.get(_LEGACY_DIRECT_PLAYWRIGHT_KEY)
    legacy_direct_present = isinstance(legacy_direct, dict) and not _spec_is_proxy(legacy_direct)
    # Leave the file untouched unless there is a KiroCrew-owned entry to migrate,
    # AND the canonical slot is either empty or already our proxy (safe to
    # (re)write). If the canonical key holds a user-declared *direct* (non-proxy)
    # server, migrating would call patch_mcp_*, which does servers[canonical] =
    # proxy_entry and would clobber that user config on every boot — so skip.
    if not (superseded_proxy_present or legacy_direct_present):
        return
    if canon_entry is not None and not canon_is_proxy:
        return
    if has_playwright_extension():
        token = get_extension_token() or ""
        if token:
            patch_mcp_extension(token)
        else:
            patch_mcp_headless()
    else:
        patch_mcp_headless()


def _converge_kirocrew_mcp_json() -> None:
    """Converge Playwright proxies in KiroCrew's own ``~/.kirocrew/mcp.json``.

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
    path = Path.home() / ".kirocrew" / "mcp.json"
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
        "Converged Playwright proxy entries %s onto %r; dropped specs (env "
        "values redacted): %s",
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


def _converge_playwright_agent_files() -> None:
    """Sweep KiroCrew-generated agent configs, converging Playwright to one
    canonical server. Runs on gateway init so an existing machine self-heals on
    a plain restart. Only KiroCrew-OWNED agent-config files are touched — the
    EXACT filenames in ``_OWNED_KIRO_AGENT_FILES`` under ``~/.kiro/agents/`` and
    ``_OWNED_CC_AGENT_FILES`` under ``~/.claude/agents/``, an explicit allowlist
    (not a ``kirocrew*`` prefix glob). A user's OWN agents — even one they name
    ``kirocrew-custom.json`` — live in the same dirs and may carry intentionally
    distinct Playwright entries; matching exact generated filenames is what keeps
    a restart from rewriting configs KiroCrew did not author. Silently skips
    unreadable/non-dict/absent files.
    """
    agent_files: list[Path] = []
    kiro_dir = Path.home() / ".kiro" / "agents"
    for name in _OWNED_KIRO_AGENT_FILES:
        p = kiro_dir / name
        if p.is_file():
            agent_files.append(p)
    cc_dir = Path.home() / ".claude" / "agents"
    for name in _OWNED_CC_AGENT_FILES:
        p = cc_dir / name
        if p.is_file():
            agent_files.append(p)
    for path in agent_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if converge_playwright_servers(data):
            try:
                # Preserve the file's existing permission bits: an agent config
                # may hold MCP ``env`` credentials and be mode 0600 — atomic_write
                # would otherwise recreate it with the umask default (commonly
                # 0644), exposing secrets to other local users after startup.
                try:
                    prev_mode: int | None = stat.S_IMODE(path.stat().st_mode)
                except OSError:
                    prev_mode = None
                # Atomic write: a live kiro-cli session reads kirocrew.json
                # through the agent-config path, so a torn write (truncated
                # mid-flush) could be parsed as a corrupt config. Rename-based
                # replace makes the swap all-or-nothing.
                atomic_write(path, json.dumps(data, indent=2), mode=prev_mode)
            except OSError:
                pass


# Sidecar manifest recording the MCP server keys KiroCrew itself has written.
# kiro-cli validates ~/.kiro/settings/mcp.json (and agent specs) with
# ``deny_unknown_fields``, so an in-spec ownership sentinel is impossible; the
# manifest lives OUT of band under ~/.kirocrew/ (a dir KiroCrew owns outright).
# It is the FIRST authorship signal for drop/converge decisions — the
# ``mcp-playwright-proxy`` launch-target heuristic remains the fallback for
# entries written by installs that predate this manifest.
_OWNED_MCP_KEYS_MANIFEST = "owned-mcp-keys.json"


def _owned_mcp_keys_path() -> Path:
    return Path.home() / ".kirocrew" / _OWNED_MCP_KEYS_MANIFEST


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


def patch_mcp_extension(token: str) -> None:
    """Update MCP config to use proxy with --extension and token env var."""
    mcp_json = Path.home() / ".kiro" / "settings" / "mcp.json"
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = data.setdefault("mcpServers", {})
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


def patch_mcp_headless() -> None:
    """Update MCP config to use proxy with headless mode config."""
    mcp_json = Path.home() / ".kiro" / "settings" / "mcp.json"
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = data.setdefault("mcpServers", {})
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
