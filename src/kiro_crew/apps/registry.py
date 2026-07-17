"""App registry — curated list of available KiroCrew apps.

The registry JSON (``app-registry.json``) is a minimal index: just app name,
git URL, branch, and install metadata.  All display information (description,
screenshots, highlights, tags, platform) comes from each app's own
``app.json``, fetched on demand and cached locally.

This "single source of truth" design means app authors only maintain their
own ``app.json`` — they never need to update the KiroCrew registry JSON
when changing descriptions, screenshots, or versions.

Each registry entry identifies the source repository via a ``gitUrl`` field
(any git-cloneable URL — ``https://github.com/...``, ``git@host:...``, etc.).
The legacy ``repo`` field is still accepted and, when no ``gitUrl`` is given,
is used as a clone target directly (so a full URL may be placed in ``repo``).

SECURITY — Trust model:
  registry JSON (gitUrl + branch) → ``git clone`` from the configured host →
  read app.json → execute setup.onInstall script.

The registry entry itself is curated/reviewed before being shipped, and the
install script in app.json has the same trust level as any code you clone
and build locally.  Install scripts run sandboxed via ``wrap_argv`` with a
minimal environment that excludes process secrets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform as _platform
import re
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.manager import (
    get_app,
    install_app,
)
from kiro_crew.apps.manager import list_apps as list_installed_apps
from kiro_crew.apps.manager import (
    set_app_source,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.sandbox import wrap_argv
from kiro_crew.sel import sel

try:
    from kiro_crew.sel import sel as _sel_fn
except ImportError:
    _sel_fn = None  # type: ignore[assignment]
from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.platform import PlatformCompositionError, current_context

logger = logging.getLogger(__name__)

# Source type prefix for registry-installed apps.
SOURCE_REGISTRY_PREFIX = "registry:"


class StreamingLogLines(list):
    """Drop-in replacement for ``list[str]`` that also pushes to an asyncio.Queue.

    Used by the streaming install endpoint to forward log lines in real-time
    without changing the signature of ``install_from_registry`` or any of its
    callees.  All existing ``log_lines.append()`` / ``.extend()`` calls work
    unchanged — the queue receives each line as it's added.
    """

    def __init__(self, queue: asyncio.Queue[str | None]) -> None:
        super().__init__()
        self._queue = queue

    def append(self, line: str) -> None:  # type: ignore[override]
        super().append(line)
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            pass  # drop if consumer is too slow

    def extend(self, lines) -> None:  # type: ignore[override]
        for line in lines:
            self.append(line)


# Timeout limits (seconds)
_CLONE_TIMEOUT = 60
_SCRIPT_TIMEOUT = 300

# Minimal environment for install/uninstall scripts.
# Only pass through variables needed for git, build tools, and shell operation.
# This prevents leaking secrets (API keys, tokens, AWS credentials) from the
# gateway process into app install scripts.
_SAFE_ENV_KEYS = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "JAVA_HOME",
        "NODE_PATH",
        "NVM_DIR",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        # JVM build tools (optional, for apps that build with gradle/maven)
        "ANT_HOME",
        "GRADLE_USER_HOME",
        "MAVEN_OPTS",
        # Git
        "GIT_SSH",
        "GIT_SSH_COMMAND",
    }
)


def minimal_env(**extra: str) -> dict[str, str]:
    """Build a minimal environment dict from the current process env.

    Only passes through safe keys (PATH, HOME, SSH_AUTH_SOCK, etc.)
    plus any explicit *extra* overrides.  Used by both registry install
    and route-level uninstall handlers.
    """
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    env.update(extra)
    return env


# Manifest cache: fetched app.json files from repos
def _manifest_cache_dir() -> Path:
    return config_dir() / "cache" / "app-manifests"


_MANIFEST_CACHE_TTL = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------

_REGISTRY_FILE = Path(__file__).parent / "app-registry.json"


def _entry_git_url(entry: dict[str, Any]) -> str:
    """Resolve the clone URL for a registry entry.

    Prefers an explicit ``gitUrl`` field.  Falls back to the legacy ``repo``
    field (which may itself contain a full URL).  Returns an empty string if
    neither yields something that looks cloneable.
    """
    url = (entry.get("gitUrl") or entry.get("repo") or "").strip()
    return url


def _looks_like_git_url(url: str) -> bool:
    """Heuristic: does *url* look like a git-cloneable remote?

    Accepts ``https://``/``http://``/``ssh://``/``git://`` URLs and
    ``user@host:path`` scp-style remotes.  A bare token (no scheme, no
    ``@host:``) is treated as a local/name reference, not cloneable.
    """
    if not url:
        return False
    if url.startswith(("https://", "http://", "ssh://", "git://", "git+")):
        return True
    # scp-style: user@host:path
    if re.match(r"^[^/@]+@[^/:]+:.+", url):
        return True
    return False


# Well-known public git forges that legitimately serve repos over SSH. Cloning
# from one of these may need ~/.ssh exposed for key auth (private repos), so the
# sandbox is loosened from "strict" to "standard" ONLY for these hosts plus any
# host the user explicitly configured as an external registry. Everything else
# stays "strict" (~/.ssh hidden) so a typo'd/hostile remote can never be offered
# the owner's SSH keys. https remotes never need ~/.ssh and always stay strict.
_PUBLIC_GIT_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "ssh.github.com",
        "gitlab.com",
        "bitbucket.org",
        "git.sr.ht",
        "codeberg.org",
    }
)


def _git_url_host(url: str) -> str:
    """Extract the lowercase host from a git URL, or '' if not parseable.

    Handles ``ssh://[user@]host[:port]/path``, scp-style ``user@host:path``,
    and ``scheme://[user@]host/path`` forms.
    """
    url = (url or "").strip()
    if not url:
        return ""
    # scheme://[user@]host[:port]/path  (ssh, git, https, http, git+ssh, ...)
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://(?:[^/@]+@)?([^/:]+)", url)
    if m:
        return m.group(1).lower()
    # scp-style: [user@]host:path
    m = re.match(r"^(?:[^/@]+@)?([^/:]+):", url)
    if m:
        return m.group(1).lower()
    return ""


def _is_ssh_git_url(url: str) -> bool:
    """True when *url* clones over SSH (and would need ~/.ssh for key auth)."""
    url = (url or "").strip()
    return url.startswith(("ssh://", "git+ssh://")) or bool(re.match(r"^[^/@]+@[^/:]+:.+", url))


def _clone_sandbox_mode(git_url: str, trusted_hosts: frozenset[str] | None = None) -> str:
    """Pick the sandbox mode for cloning *git_url*.

    Returns ``"standard"`` (exposes ~/.ssh so git can offer the owner's SSH
    keys) ONLY for an SSH/scp remote whose host is trusted — a well-known
    public forge or a host the user explicitly configured as an external
    registry. All other cases return ``"strict"`` (~/.ssh hidden): https/git
    remotes never need SSH keys, and an untrusted SSH host fails closed rather
    than being offered the owner's private keys.
    """
    if not _is_ssh_git_url(git_url):
        return "strict"
    host = _git_url_host(git_url)
    if not host:
        return "strict"
    allowed = _PUBLIC_GIT_HOSTS | (trusted_hosts or frozenset())
    return "standard" if host in allowed else "strict"


def _configured_registry_hosts() -> frozenset[str]:
    """Hosts of the user-configured external registries (trusted for SSH).

    A registry the owner deliberately added to their config is a host they
    intend to authenticate to, so its SSH clones are allowed ~/.ssh access even
    if it is not a well-known public forge (e.g. a self-hosted Gitea/GitLab).
    """
    from kiro_crew.config.loader import (
        KiroCrewConfig,  # deferred: loader imports apps/ at module level
    )

    try:
        config = KiroCrewConfig.load()
    except Exception as exc:  # config load is best-effort for this gate
        logger.debug("Could not load config for registry host allowlist: %s", exc)
        return frozenset()
    hosts = {
        _git_url_host(reg.repo) for reg in (config.registries or []) if _git_url_host(reg.repo)
    }
    return frozenset(hosts)


def _context_clone_sandbox_mode(git_url: str) -> str:
    """Pick the clone sandbox mode for *git_url* via the active PlatformContext.

    Routes the trusted-host + clone-sandbox-mode decision through
    ``current_context().registry``.  The Default ``AppRegistryPolicy`` delegates
    to this module's ``_clone_sandbox_mode`` / ``_PUBLIC_GIT_HOSTS``, so
    standalone is byte-for-byte today's decision (public forges + user-configured
    registry hosts allowed for SSH, everything else strict).  A companion can add
    further internal git hosts to the trusted set.  Any failure falls back to the
    bare module decision so the security gate never disappears.
    """
    try:
        policy = current_context().registry
        trusted = frozenset(policy.public_git_hosts()) | _configured_registry_hosts()
        return policy.clone_sandbox_mode(git_url, trusted)
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("registry clone-sandbox-mode via context failed; using default", exc_info=True)
        return _clone_sandbox_mode(git_url, _configured_registry_hosts())


def _load_registry_file() -> list[dict[str, Any]]:
    """Load and parse the bundled app-registry.json."""
    if not _REGISTRY_FILE.is_file():
        logger.warning("Registry file not found: %s", _REGISTRY_FILE)
        return []
    try:
        data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            logger.warning("Registry file is not a JSON array")
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load registry: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Remote manifest fetching + caching
# ---------------------------------------------------------------------------


def _manifest_cache_path(name: str) -> Path:
    return _manifest_cache_dir() / f"{name}.json"


def _read_manifest_cache(name: str) -> dict[str, Any] | None:
    """Read cached app.json for a registry app. Returns None if missing or stale."""
    path = _manifest_cache_path(name)
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > _MANIFEST_CACHE_TTL:
            return None  # stale
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_manifest_cache(name: str, data: dict[str, Any]) -> None:
    """Write app.json to the manifest cache (atomic)."""
    _manifest_cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(
            _manifest_cache_path(name),
            json.dumps(data, indent=2) + "\n",
        )
    except OSError as exc:
        logger.warning("Failed to cache manifest for %s: %s", name, exc)


async def _fetch_app_manifest(
    repo: str,
    branch: str,
    subdirectory: str = "",
    app_name: str = "",
    git_url: str = "",
) -> dict[str, Any] | None:
    """Fetch app.json for an app from its source repo (lightweight).

    Tries, in order:
      1. The persistent clone under ``~/.kirocrew/app-sources/{app_name}/``
         (if the app was already cloned by a previous install).
      2. A throwaway shallow clone of *git_url* into a temp directory, from
         which only ``app.json`` is read (the clone is then discarded).

    Returns the parsed app.json dict, or None on failure.  All failures are
    swallowed (returns None) so a missing/unreachable repo never crashes the
    listing path on a vanilla machine.
    """
    # Try persistent clone first (already installed)
    if app_name:
        clone_dir = app_source_dir(app_name)
        local_manifest = (
            clone_dir / subdirectory / "app.json" if subdirectory else clone_dir / "app.json"
        )
        if local_manifest.is_file():
            try:
                content = await asyncio.to_thread(local_manifest.read_text, "utf-8")
                return json.loads(content)
            except (json.JSONDecodeError, OSError):
                pass

    if not git_url:
        git_url = repo
    if not _looks_like_git_url(git_url):
        # Not a cloneable URL (e.g. empty or a bare name on a public machine).
        return None

    file_path = f"{subdirectory}/app.json" if subdirectory else "app.json"
    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-manifest-")
        clone_cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            "--single-branch",
            git_url,
            tmp_root,
        ]
        sandboxed_cmd, _cleanup = wrap_argv(clone_cmd, mode=_context_clone_sandbox_mode(git_url))
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_env(),
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
        if proc.returncode != 0:
            logger.debug(
                "manifest clone failed for %s: %s",
                git_url,
                stderr.decode(errors="replace").strip(),
            )
            return None
        manifest_path = Path(tmp_root) / file_path
        if not manifest_path.is_file():
            return None
        content = await asyncio.to_thread(manifest_path.read_text, "utf-8")
        return json.loads(content)
    except (asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to fetch app.json from %s: %s", git_url, exc)
        return None
    finally:
        if tmp_root:
            await asyncio.to_thread(shutil.rmtree, tmp_root, ignore_errors=True)


async def _resolve_manifest(entry: dict[str, Any]) -> dict[str, Any]:
    """Merge registry entry with its remote app.json manifest.

    Returns the entry enriched with display fields from app.json.
    Registry fields (name, repo, branch, managed, detectInstalled) take
    precedence; everything else comes from app.json.
    """
    name = entry.get("name", "")
    repo = entry.get("repo", "")
    branch = entry.get("branch", "mainline")
    subdirectory = entry.get("subdirectory", "")
    git_url = _entry_git_url(entry)

    if not git_url:
        return entry

    # Try cache first
    cached = await asyncio.to_thread(_read_manifest_cache, name)
    if cached:
        return _merge_manifest(entry, cached)

    # Fetch from repo
    manifest = await _fetch_app_manifest(
        repo, branch, subdirectory, app_name=name, git_url=git_url
    )
    if manifest:
        await asyncio.to_thread(_write_manifest_cache, name, manifest)
        return _merge_manifest(entry, manifest)

    # No manifest available — return entry as-is (minimal info)
    logger.info("Could not fetch app.json for %s — showing minimal info", name)
    return entry


def _merge_manifest(entry: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Merge app.json fields into a registry entry.

    Registry-only fields (name, repo, branch, managed, detectInstalled)
    are preserved from the entry. Everything else comes from app.json,
    with the blob proxy URL pattern applied to image paths.
    """
    repo = entry.get("repo", "")
    result = dict(entry)  # start with registry fields

    # Top-level display fields from app.json
    for key in (
        "displayName",
        "description",
        "version",
        "author",
        "tags",
        "highlights",
        "license",
        "minKiroCrewVersion",
    ):
        if key in manifest:
            result[key] = manifest[key]

    # Runtime fields go under "manifest" — matches the installed app
    # data structure so the frontend can always read app.manifest.*
    manifest_fields: dict[str, Any] = {}
    for key in (
        "agents",
        "skills",
        "crons",
        "mcpServers",
        "permissions",
        "setup",
        "ui",
        "openCommand",
    ):
        if key in manifest:
            manifest_fields[key] = manifest[key]
    if manifest_fields:
        result["manifest"] = manifest_fields

    # Platform config from app.json
    if "platform" in manifest:
        result["platform"] = manifest["platform"]

    # Icon — convert repo-relative path to blob proxy URL
    icon_path = manifest.get("iconPath", "")
    if icon_path and repo:
        result["iconUrl"] = f"/api/apps/blob?repo={repo}&path={icon_path}"
    # Lucide fallback icon from manifest extra fields
    if manifest.get("icon"):
        result["icon"] = manifest["icon"]

    # Screenshots — convert repo-relative paths to blob proxy URLs
    screenshots = manifest.get("screenshots", [])
    if screenshots and repo:
        result["screenshots"] = [f"/api/apps/blob?repo={repo}&path={p}" for p in screenshots]

    # Screenshots dark — convert repo-relative paths to blob proxy URLs
    screenshots_dark = manifest.get("screenshotsDark", [])
    if screenshots_dark and repo:
        result["screenshotsDark"] = [f"/api/apps/blob?repo={repo}&path={p}" for p in screenshots_dark]

    # Hero images — convert repo-relative paths to blob proxy URLs
    hero = manifest.get("heroImage", "")
    if hero and repo:
        result["heroImage"] = f"/api/apps/blob?repo={repo}&path={hero}"
    hero_dark = manifest.get("heroImageDark", "")
    if hero_dark and repo:
        result["heroImageDark"] = f"/api/apps/blob?repo={repo}&path={hero_dark}"

    return result


def _enrich_with_install_status(
    entries: list[dict[str, Any]],
    installed_map: dict[str, dict[str, Any]],
    detected: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Add ``installed``, ``installedVersion``, ``enabled``, ``updateAvailable``.

    *detected* is a set of app names that were found via ``detectInstalled``
    shell commands (installed outside KiroCrew's app manager).
    """
    detected = detected or set()
    for entry in entries:
        name = entry.get("name", "")
        existing = installed_map.get(name)
        externally_detected = name in detected

        entry["installed"] = existing is not None or externally_detected
        if existing:
            entry["installedVersion"] = existing.get("version", "")
            entry["enabled"] = existing.get("enabled", False)
            entry["origin"] = existing.get("origin", "registry")
            entry["resources"] = existing.get("resources", "gateway")
            entry["lifecycle"] = existing.get("lifecycle", "gateway")
            entry["updateAvailable"] = _version_newer(
                entry.get("version", ""),
                existing.get("version", ""),
            )
        elif externally_detected:
            entry["installedVersion"] = "unknown"
            entry["enabled"] = True
            entry["origin"] = "external"
            entry["resources"] = "app"
            entry["lifecycle"] = "app"
            entry["updateAvailable"] = False
        else:
            entry["updateAvailable"] = False
    return entries


def _version_newer(registry_ver: str, installed_ver: str) -> bool:
    """Return True if registry version is strictly newer than installed.

    Compares semver-style version strings (major.minor.patch).
    Pre-release suffixes (e.g. ``-beta.1``) and build metadata
    (e.g. ``+build.123``) are stripped before comparison.
    Falls back to False if parsing fails (conservative).
    """

    def _parse(v: str) -> tuple[int, ...]:
        # Strip pre-release and build metadata: "1.2.3-beta.1+build" → "1.2.3"
        base = v.split("-", 1)[0].split("+", 1)[0]
        parts = [int(x) for x in base.split(".")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)

    try:
        return _parse(registry_ver) > _parse(installed_ver)
    except (ValueError, AttributeError):
        return False  # Conservative: don't flag update on parse failure


# ---------------------------------------------------------------------------
# External (federated) registries
# ---------------------------------------------------------------------------

_EXTERNAL_REGISTRY_CACHE_TTL = 3600  # 1 hour


def _external_registry_cache_path(name: str) -> Path:

    # Sanitize name to prevent path traversal in cache file paths
    if not re.match(r"^[A-Za-z0-9_\-]+$", name):
        name = "invalid"
    return _manifest_cache_dir() / f"_registry_{name}.json"


def _read_external_registry_cache(
    name: str,
    *,
    ignore_ttl: bool = False,
) -> list[dict[str, Any]] | None:
    """Read cached external registry entries. Returns None if missing or stale.

    When *ignore_ttl* is True, returns data regardless of age — used by
    synchronous callers that cannot refresh the cache themselves.
    """
    path = _external_registry_cache_path(name)
    if not path.is_file():
        return None
    try:
        if not ignore_ttl:
            age = time.time() - path.stat().st_mtime
            if age > _EXTERNAL_REGISTRY_CACHE_TTL:
                return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, OSError):
        return None


def _write_external_registry_cache(name: str, entries: list[dict[str, Any]]) -> None:
    """Write external registry entries to cache."""
    _manifest_cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(
            _external_registry_cache_path(name),
            json.dumps(entries, indent=2) + "\n",
        )
    except OSError as exc:
        logger.warning("Failed to cache external registry %s: %s", name, exc)


async def _communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    timeout: float,
) -> tuple[bytes, bytes]:
    """Communicate with a subprocess, killing it on timeout to prevent leaks."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise


async def _fetch_external_registry_index(
    repo: str,
    branch: str,
) -> list[dict[str, Any]] | None:
    """Fetch app-registry.json from an external repo via a shallow git clone.

    *repo* is a git-cloneable URL (https/ssh/git/scp-style).  The repo is
    shallow-cloned into a throwaway temp directory.  If it contains an
    ``app-registry.json`` index, that is parsed and returned.  Otherwise the
    clone is scanned for ``apps/*/app.json`` and a synthetic index is built.

    Returns None on any failure (unreachable repo, invalid input, etc.) so a
    misconfigured external registry never crashes the listing path.

    Security controls:
    - Input validation: branch is regex-validated; only cloneable URLs accepted.
    - OS-level sandbox: wrap_argv with a trusted-host-gated mode
      (_clone_sandbox_mode). An SSH/scp remote on a well-known public forge or a
      user-configured registry host clones in "standard" mode (~/.ssh exposed so
      git can offer the owner's keys); any other remote stays "strict" (~/.ssh
      hidden) so a typo'd/hostile host is never offered the owner's SSH keys.
      https remotes never need ~/.ssh and always stay strict. Both modes unshare
      the user/mount namespaces and hide sensitive config dirs (.gnupg,
      .config/gcloud, ...).
    - Timeout + kill: _communicate_with_timeout() kills on timeout.
    - Read-only: only ``git clone`` (no write operations to the remote).
    - SEL audit (best-effort): start/outcome events logged when SEL is present.
    """
    # Input validation — reject values that could be used for command injection.
    if not _looks_like_git_url(repo):
        logger.warning("Rejecting non-cloneable external registry repo: %r", repo)
        return None
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-./]*$", branch) or ".." in branch:
        logger.warning("Rejecting invalid branch name: %r", branch)
        return None

    git_url = repo

    # SEL audit: log external subprocess invocation for traceability (best-effort).
    def _sel_outcome(outcome: str) -> None:
        if _sel_fn is None:
            return
        try:
            _sel_fn().log_api_access(
                caller="registry",
                operation="fetch_external_registry",
                outcome=outcome,
                resources=f"repo={repo} branch={branch}",
            )
        except Exception as exc:
            logger.debug("SEL audit log failed for fetch_external_registry: %s", exc)

    _sel_outcome("started")

    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-registry-")
        clone_cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            "--single-branch",
            git_url,
            tmp_root,
        ]
        sandboxed_cmd, _ = wrap_argv(clone_cmd, mode=_context_clone_sandbox_mode(git_url))
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_env(),
        )
        _, _ = await _communicate_with_timeout(proc, timeout=_CLONE_TIMEOUT)
        if proc.returncode != 0:
            _sel_outcome("failed")
            return None

        clone_path = Path(tmp_root)

        # Prefer an explicit app-registry.json index.
        index_path = clone_path / "app-registry.json"
        if index_path.is_file():
            try:
                data = json.loads(await asyncio.to_thread(index_path.read_text, "utf-8"))
                if isinstance(data, list):
                    _sel_outcome("success")
                    return data
            except (json.JSONDecodeError, OSError):
                pass

        # Fallback: scan for apps/*/app.json
        entries: list[dict[str, Any]] = []
        apps_dir = clone_path / "apps"
        if apps_dir.is_dir():
            for app_dir in sorted(apps_dir.iterdir()):
                if not app_dir.is_dir():
                    continue
                if not (app_dir / "app.json").is_file():
                    continue
                app_name = app_dir.name
                if not app_name or app_name in (".", ".."):
                    continue
                entries.append(
                    {
                        "name": app_name,
                        "repo": repo,
                        "branch": branch,
                        "subdirectory": f"apps/{app_name}",
                    }
                )
        result = entries if entries else None
        _sel_outcome("success" if result else "failed")
        return result

    except (asyncio.TimeoutError, OSError) as exc:
        logger.debug("Failed to fetch external registry from %s: %s", git_url, exc)
        _sel_outcome("failed")
        return None
    finally:
        if tmp_root:
            await asyncio.to_thread(shutil.rmtree, tmp_root, ignore_errors=True)


async def _load_external_registries() -> list[dict[str, Any]]:
    """Load app entries from all configured external registries.

    Reads the ``registries`` config field and fetches each repo's index.
    Results are cached for 1 hour. Each entry is tagged with its registry
    source for UI grouping.
    """
    from kiro_crew.config.loader import (
        KiroCrewConfig,  # circular import: loader.py imports from apps/ at module level; deferring avoids ImportError
    )

    config = await asyncio.to_thread(KiroCrewConfig.load)
    if not config.registries:
        return []

    all_entries: list[dict[str, Any]] = []

    async def _load_one(reg) -> list[dict[str, Any]]:
        name = reg.name or reg.repo
        repo = reg.repo
        branch = reg.branch

        # Try cache first
        cached = await asyncio.to_thread(_read_external_registry_cache, name)
        if cached is not None:
            for entry in cached:
                entry["_registry"] = name
            return cached

        # Fetch from repo
        entries = await _fetch_external_registry_index(repo, branch)
        if entries is None:
            # Fall back to stale cache (stale > missing)
            stale = await asyncio.to_thread(
                _read_external_registry_cache,
                name,
                ignore_ttl=True,
            )
            if stale is not None:
                for entry in stale:
                    entry["_registry"] = name
                return stale
            logger.warning("Failed to load external registry %s from %s", name, repo)
            return []

        # Ensure each entry has gitUrl/repo/branch set (for install_from_registry)
        for entry in entries:
            entry.setdefault("gitUrl", repo)
            entry.setdefault("repo", repo)
            entry.setdefault("branch", branch)
            entry["_registry"] = name

        # Cache the results
        await asyncio.to_thread(_write_external_registry_cache, name, entries)
        return entries

    results = await asyncio.gather(
        *[_load_one(reg) for reg in config.registries],
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, list):
            all_entries.extend(result)
        elif isinstance(result, Exception):
            logger.warning("External registry load failed: %s", result)

    return all_entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_registry() -> list[dict[str, Any]]:
    """Return all registry apps with display info and install status.

    1. Load minimal registry JSON (name, repo, branch)
    2. Load external registries from user config
    3. Fetch each app's app.json (cached, 24h TTL) for display info
    4. Run detectInstalled commands for external installs
    5. Enrich with install status from KiroCrew's app manager
    """
    entries = await asyncio.to_thread(_load_registry_file)

    # Load external registries from config, deduplicating against core and each other
    external_entries = await _load_external_registries()
    seen_names = {e.get("name") for e in entries}
    for e in external_entries:
        name = e.get("name")
        if name not in seen_names:
            seen_names.add(name)
            entries.append(e)

    installed = await asyncio.to_thread(list_installed_apps)
    installed_map = {a["name"]: a for a in installed}

    # Fetch manifests in parallel for all entries
    resolved = await asyncio.gather(
        *[_resolve_manifest(e) for e in entries],
        return_exceptions=True,
    )
    entries = [r if isinstance(r, dict) else entries[i] for i, r in enumerate(resolved)]

    # Run detectInstalled commands for apps not already in installed_map
    detected: set[str] = set()
    for entry in entries:
        name = entry.get("name", "")
        if name in installed_map:
            continue  # already known, skip detection
        detect_cmd = entry.get("detectInstalled", "")
        if not detect_cmd:
            continue
        try:
            from kiro_crew.sandbox import wrap_argv

            base_cmd = ["/bin/sh", "-c", detect_cmd]
            sandboxed_cmd, _cleanup = wrap_argv(base_cmd, mode="strict")
            proc = await asyncio.create_subprocess_exec(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                detected.add(name)
                logger.info("Detected external install: %s", name)
        except (asyncio.TimeoutError, OSError):
            pass  # detection failed, treat as not installed

    return _enrich_with_install_status(entries, installed_map, detected)


def get_server_platform() -> dict[str, str]:
    """Return the server's platform info for frontend compatibility checks."""
    from kiro_crew.apps.manifest import PlatformConfig

    return {"os": PlatformConfig.current_os(), "arch": _platform.machine()}


def get_registry_app(name: str) -> dict[str, Any] | None:
    """Look up a registry app by name (synchronous, for internal use).

    Searches the bundled registry first, then external registry caches.
    """
    for entry in _load_registry_file():
        if entry.get("name") == name:
            return entry
    # Search external registry caches
    from kiro_crew.config.loader import (
        KiroCrewConfig,  # circular import: loader.py imports from apps/ at module level; deferring avoids ImportError
    )

    config = KiroCrewConfig.load()
    for reg in config.registries:
        reg_name = reg.name or reg.repo
        cached = _read_external_registry_cache(reg_name, ignore_ttl=True)
        if cached:
            for entry in cached:
                if entry.get("name") == name:
                    return entry
    return None


def get_registry_app_by_repo(repo: str) -> dict[str, Any] | None:
    """Look up a registry app by repo name (for blob proxy branch lookup)."""
    for entry in _load_registry_file():
        if entry.get("repo") == repo:
            return entry
    return None


def is_registry_source(source: str) -> bool:
    """Check if a source string indicates a registry-installed app."""
    return source.startswith(SOURCE_REGISTRY_PREFIX)


def registry_name_from_source(source: str) -> str:
    """Extract the app name from a ``registry:<name>`` source string."""
    return source[len(SOURCE_REGISTRY_PREFIX) :]


def _external_registry_repos() -> set[str]:
    """Repo names of apps in the user's configured external (federated) registries.

    Reads each registry index from the local sync cache only (``ignore_ttl`` so a
    stale index still resolves) — never fetches, so it is safe to call from the
    per-request blob-proxy worker thread. Fails open to an empty set; the caller
    treats these as additive to the bundled allowlist.
    """
    repos: set[str] = set()
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: loader.py imports from apps/ at module level; deferring avoids ImportError
        )

        for reg in KiroCrewConfig.load().registries:
            cached = _read_external_registry_cache(reg.name or reg.repo, ignore_ttl=True)
            for entry in cached or []:
                if isinstance(entry, dict) and entry.get("repo"):
                    repos.add(entry["repo"])
    except Exception:  # fail open: the allowlist must never break blob serving
        logger.debug("_external_registry_repos: read failed", exc_info=True)
    return repos


def known_registry_repos() -> set[str]:
    """Repo names trusted by the ``/api/apps/blob`` SSRF gate.

    Union of the bundled registry and the user's external (federated)
    registries — external-registry apps resolve an ``/api/apps/blob`` iconUrl,
    so their repos must be allowlisted here or the App Store icon 403s.
    """
    bundled = {e["repo"] for e in _load_registry_file() if e.get("repo")}
    return bundled | _external_registry_repos()


# ---------------------------------------------------------------------------
# Install from registry
# ---------------------------------------------------------------------------


def _app_sources_dir() -> Path:
    return config_dir() / "app-sources"


def app_source_dir(name: str) -> Path:
    """Return ~/.kirocrew/app-sources/{name}/ — persistent clone directory."""
    return _app_sources_dir() / name


# ---------------------------------------------------------------------------
# Git clone + build support for App Store installs
# ---------------------------------------------------------------------------

_BUILD_TIMEOUT = 600  # 10 minutes — frontend bundlers / packagers can be slow
_APP_LOCKS: dict[str, asyncio.Lock] = {}  # per-app serialization
_KILL_GRACE_PERIOD = 5  # seconds to wait after SIGTERM before SIGKILL


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Send SIGTERM to the process group, escalate to SIGKILL if needed.

    Routed through platform_compat (killpg on POSIX, taskkill /T on Windows) so
    the Brazil app-build timeout path doesn't AttributeError on win32.
    """
    try:
        platform_compat.kill_process_tree(proc.pid, platform_compat.SIGTERM)
    except OSError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_PERIOD)
    except asyncio.TimeoutError:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except OSError:
            proc.kill()
        await proc.wait()


async def _git_clone_or_pull(
    git_url: str,
    branch: str,
    dest: Path,
    log_lines: list[str],
) -> dict[str, Any] | None:
    """Clone *git_url* into *dest*, or fast-forward it if already present.

    Returns None on success, or a ``{"ok": False, ...}`` error dict on failure.
    """
    if dest.is_dir() and (dest / ".git").is_dir():
        # Already cloned — fetch and fast-forward the target branch.
        log_lines.append(f"Updating {git_url} (branch: {branch})...")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "pull",
            "--ff-only",
            "origin",
            branch,
            cwd=str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=minimal_env(),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            log_lines.append(stdout.decode(errors="replace").strip())
            if proc.returncode != 0:
                log_lines.append(
                    f"git pull failed (exit {proc.returncode}), building with existing code"
                )
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            log_lines.append("git pull timed out, building with existing code")
        return None

    # Fresh clone.
    log_lines.append(f"Cloning {git_url} (branch: {branch})...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    clone_cmd = [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        branch,
        "--single-branch",
        git_url,
        str(dest),
    ]
    sandboxed_cmd, _cleanup = wrap_argv(clone_cmd, mode=_context_clone_sandbox_mode(git_url))
    proc = await asyncio.create_subprocess_exec(
        *sandboxed_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        env=minimal_env(),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
        log_lines.append(stdout.decode(errors="replace").strip())
    except asyncio.TimeoutError:
        await _kill_process_group(proc)
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "name": dest.name, "error": "git clone timed out"}
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "name": dest.name, "error": "git clone failed"}
    return None


async def _clone_build_app(
    git_url: str,
    app_name: str,
    log_lines: list[str],
    branch: str = "mainline",
) -> dict[str, Any]:
    """Clone an app repo and run its build, returning the source directory.

    Source is cloned to ``~/.kirocrew/app-sources/{app_name}/`` (persistent;
    survives reboots and is reused for updates).  After clone/pull, the app's
    declared build commands run via :func:`_run_app_build`.

    Returns ``{"ok": True, "pkg_dir": <Path>}`` on success or
    ``{"ok": False, "error": ...}`` on failure.
    """
    # Per-app lock — prevents two concurrent installs of the same app from
    # racing on clone / build. Different apps can install in parallel.
    if app_name not in _APP_LOCKS:
        _APP_LOCKS[app_name] = asyncio.Lock()

    async with _APP_LOCKS[app_name]:
        return await _clone_build_app_locked(git_url, app_name, log_lines, branch=branch)


async def _clone_build_app_locked(
    git_url: str,
    app_name: str,
    log_lines: list[str],
    branch: str = "mainline",
) -> dict[str, Any]:
    """Inner implementation of _clone_build_app, called under per-app lock."""
    if not _looks_like_git_url(git_url):
        return {
            "ok": False,
            "name": app_name,
            "error": f"{git_url!r} is not a cloneable git URL",
        }

    pkg_dir = app_source_dir(app_name)
    clone_err = await _git_clone_or_pull(git_url, branch, pkg_dir, log_lines)
    if clone_err is not None:
        return clone_err

    result = await _run_app_build(pkg_dir, app_name, log_lines)
    if result["ok"]:
        result["pkg_dir"] = pkg_dir
    return result


async def _run_app_build(
    build_dir: Path,
    app_name: str,
    log_lines: list[str],
) -> dict[str, Any]:
    """Build a cloned app using a sensible default for its ecosystem.

    Detection (in order):
      - ``package.json``      → ``npm install`` (+ ``npm run build`` if a
                                 ``build`` script is declared)
      - ``pyproject.toml`` /
        ``setup.py`` /
        ``requirements.txt``  → ``pip install .`` (or ``-r requirements.txt``)
      - otherwise             → no build step (source is used as-is)

    The app's own ``setup.onInstall`` script (run later by
    ``install_from_registry``) can perform any additional steps.  A missing
    build toolchain (no npm / no pip) is treated as a soft failure: the step
    is skipped with a logged warning rather than aborting the install, so an
    app that needs no build still installs cleanly.
    """
    build_cmds: list[list[str]] = []

    if (build_dir / "package.json").is_file():
        if shutil.which("npm"):
            build_cmds.append(["npm", "install"])
            try:
                pkg = json.loads((build_dir / "package.json").read_text("utf-8"))
                if (pkg.get("scripts") or {}).get("build"):
                    build_cmds.append(["npm", "run", "build"])
            except (json.JSONDecodeError, OSError):
                pass
        else:
            log_lines.append("npm not found on PATH — skipping JavaScript build step")
    elif (
        (build_dir / "pyproject.toml").is_file()
        or (build_dir / "setup.py").is_file()
        or (build_dir / "requirements.txt").is_file()
    ):
        pip = shutil.which("pip") or shutil.which("pip3")
        if pip:
            if (build_dir / "requirements.txt").is_file() and not (
                (build_dir / "pyproject.toml").is_file() or (build_dir / "setup.py").is_file()
            ):
                build_cmds.append([pip, "install", "-r", "requirements.txt"])
            else:
                build_cmds.append([pip, "install", "."])
        else:
            log_lines.append("pip not found on PATH — skipping Python build step")

    if not build_cmds:
        log_lines.append("No build step detected — using source as-is")
        return {"ok": True}

    for cmd in build_cmds:
        log_lines.append(f"Running {' '.join(cmd)} in {build_dir}...")
        sandboxed_cmd, _cleanup = wrap_argv(cmd, mode="standard")
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            cwd=str(build_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=minimal_env(),
        )
        assert proc.stdout is not None

        async def _drain() -> None:
            async for raw_line in proc.stdout:  # type: ignore[union-attr]
                log_lines.append(raw_line.decode(errors="replace").rstrip())
            await proc.wait()

        try:
            await asyncio.wait_for(_drain(), timeout=_BUILD_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            return {
                "ok": False,
                "name": app_name,
                "error": f"build timed out after {_BUILD_TIMEOUT}s ({' '.join(cmd)})",
            }

        if proc.returncode != 0:
            return {
                "ok": False,
                "name": app_name,
                "error": f"build failed (exit {proc.returncode}): {' '.join(cmd)}",
            }

    log_lines.append("build succeeded")
    return {"ok": True}


async def install_from_registry(
    name: str,
    log_lines: list[str] | None = None,
) -> dict[str, Any]:
    """Clone an app from its git repo and install it.

    Source code is cloned to ``~/.kirocrew/app-sources/{name}/`` (persistent,
    survives reboots, used by app update scripts).

    For self-managed apps (``managed: "self"`` in registry), only the clone +
    install script is run — KiroCrew does NOT copy files to ``~/.kirocrew/apps/``
    or register resources via bridges.  The app registers itself at runtime.

    For kirocrew-managed apps, files are copied to ``~/.kirocrew/apps/{name}/``
    and resources are registered via bridges.py as usual.

    Args:
        name: Registry app name.
        log_lines: Optional list to collect log output.  Pass a
            :class:`StreamingLogLines` instance to stream logs in real-time
            via the SSE install endpoint.  If *None*, a plain ``list`` is used
            (original behaviour).

    Steps:
    1. Validate the app exists in the trusted registry JSON
    2. Clone the repo to ~/.kirocrew/app-sources/{name}/ (timeout: 60s)
    3. Build it (npm/pip, auto-detected) then run the install script from
       app.json if any (timeout: 300s)
    4. For kirocrew-managed: call install_app() or update_app()
    5. Store ``registry:<name>`` as source for future updates

    Returns a dict with ok, name, message/error, and log output.
    """
    entry = get_registry_app(name)
    if not entry:
        return {"ok": False, "error": f"app {name!r} not found in registry"}

    git_url = _entry_git_url(entry)
    if not git_url:
        return {"ok": False, "error": f"app {name!r} has no git URL configured"}

    repo = entry.get("repo", "")
    branch = entry.get("branch", "mainline")
    subdirectory = entry.get("subdirectory", "")

    # Fetch the app's manifest for platform info and install script. This is a
    # read-only metadata fetch (git archive of app.json), safe to do before the
    # admission gate so a correctly-signed manifest can be passed to it.
    manifest = await _fetch_app_manifest(
        repo, branch, subdirectory, app_name=name, git_url=git_url
    )

    # Admission: gate AFTER the manifest fetch (so a signed manifest is verified)
    # but BEFORE the repo is cloned and setup.onInstall runs, so a banned /
    # non-allowlisted / unsigned app is never cloned nor its install script run.
    admission_manifest = AppManifest.from_dict(manifest) if manifest else None
    denied = app_admission_denied(
        name, manifest=admission_manifest, action="install_from_registry"
    )
    if denied:
        sel().log_api_access(
            caller="app_install_from_registry",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return {"ok": False, "name": name, "error": f"blocked by admission policy: {denied}"}

    # Platform compatibility check — if the app requires a specific OS and
    # KiroCrew is running on an incompatible platform, return client install
    # instructions instead of attempting a server-side install.
    manifest_platform = (manifest or {}).get("platform", {})
    required_os = manifest_platform.get("os", ["macos", "linux"])
    install_mode = manifest_platform.get("installMode", "server")

    from kiro_crew.apps.manifest import PlatformConfig

    if install_mode == "client" and not PlatformConfig(os=required_os).supports_platform(
        sys.platform
    ):
        client_install = manifest_platform.get("clientInstall", {})
        os_label = ", ".join(o.capitalize() if o != "macos" else "macOS" for o in required_os)
        return {
            "ok": False,
            "needsClientInstall": True,
            "name": name,
            "clientInstall": client_install,
            "platform": {"required": required_os, "current": PlatformConfig.current_os()},
            "error": f"This app requires {os_label} and must be installed on your local machine.",
        }

    is_self_managed = entry.get("resources") == "app"
    if log_lines is None:
        log_lines = []

    # Validate minKiroCrewVersion if declared
    min_version = (manifest or {}).get("minKiroCrewVersion", "")
    if min_version:
        from kiro_crew.apps.version import check_min_version

        ver_err = check_min_version(min_version)
        if ver_err:
            return {
                "ok": False,
                "name": name,
                "error": ver_err,
            }

    # Guard: check if already installed externally (e.g. user ran setup.sh manually)
    detect_cmd = entry.get("detectInstalled", "")
    if detect_cmd:
        try:
            from kiro_crew.sandbox import wrap_argv

            base_cmd = ["/bin/sh", "-c", detect_cmd]
            sandboxed_cmd, _cleanup = wrap_argv(base_cmd, mode="strict")
            proc = await asyncio.create_subprocess_exec(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                return {
                    "ok": False,
                    "name": name,
                    "error": f"{name} is already installed on this machine. "
                    f"Launch it to register with KiroCrew automatically.",
                }
        except (asyncio.TimeoutError, OSError):
            pass

    try:
        # Step 1: Clone the app repo and build it (npm/pip auto-detected).
        # `git clone` handles fetch + branch checkout; a subsequent install
        # run fast-forwards the existing clone instead of re-cloning.
        build_result = await _clone_build_app(
            git_url,
            name,
            log_lines,
            branch=branch,
        )
        if not build_result["ok"]:
            return {**build_result, "log": "\n".join(log_lines)}

        app_source = build_result["pkg_dir"]
        if subdirectory:
            app_source = app_source / subdirectory

        if not (app_source / "app.json").is_file():
            return {
                "ok": False,
                "name": name,
                "error": f"app.json not found in {app_source}",
                "log": "\n".join(log_lines),
            }

        # Read install script from the cloned repo's app.json.
        # Trust model: curated registry entry → cloned repo → app.json
        # (maintained by the app author).  The install script has the same
        # trust level as any code you clone and build locally.
        install_script = ""
        try:
            manifest_raw = await asyncio.to_thread(
                (app_source / "app.json").read_text,
                "utf-8",
            )
            manifest_data = json.loads(manifest_raw)
            install_script = (manifest_data.get("setup") or {}).get("onInstall", "")
        except (json.JSONDecodeError, OSError):
            pass

        # Step 2: Run install script
        if install_script:
            log_lines.append(f"Running install script: {install_script}")
            # Sandboxed via wrap_argv(); consider migrating to AcpClient._spawn() for full OS-level isolation.
            # Trust model: curated registry entry → cloned repo → app.json
            # (maintained by the app author, same trust as any code you build locally).
            # SEL audit event emitted below for traceability.
            logger.info(
                "Executing sandboxed install script for app %s from repo %s",
                name,
                repo,
            )
            try:
                sel().log_api_access(
                    caller="registry",
                    operation="app_install_script",
                    outcome="started",
                    resources=f"{name} repo={repo}",
                )
            except Exception as exc:
                logger.debug("SEL audit failed for app %s install: %s", name, exc)
            # Wrap with safe defaults:
            #   set -e  — exit on first error
            #   set -u  — treat unset variables as errors (prevents rm -rf $EMPTY/)
            #   set -o pipefail — propagate pipe failures
            safe_script = f"set -euo pipefail\n{install_script}"
            from kiro_crew.sandbox import wrap_argv

            base_cmd = ["/bin/bash", "-c", safe_script]
            sandboxed_cmd, _cleanup = wrap_argv(base_cmd, mode="standard")
            proc = await asyncio.create_subprocess_exec(
                *sandboxed_cmd,
                cwd=str(app_source),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=minimal_env(NONINTERACTIVE="1"),
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_SCRIPT_TIMEOUT)
            except asyncio.TimeoutError:
                # Kill the entire process group (shell + children)
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    proc.kill()
                return {
                    "ok": False,
                    "name": name,
                    "error": f"install script timed out after {_SCRIPT_TIMEOUT}s",
                    "log": "\n".join(log_lines),
                }

            lines = stdout.decode(errors="replace").strip().split("\n")
            if len(lines) > 50:
                log_lines.append(f"... ({len(lines) - 50} lines truncated)")
                log_lines.extend(lines[-50:])
            else:
                log_lines.extend(lines)

            if proc.returncode != 0:
                return {
                    "ok": False,
                    "name": name,
                    "error": f"install script failed (exit {proc.returncode})",
                    "log": "\n".join(log_lines),
                }

        # Step 3: Resolve dependencies (if declared in manifest)
        deps_data = (manifest_data or {}).get("dependencies") if manifest_data else None
        if deps_data and isinstance(deps_data, dict):
            from kiro_crew.apps.dependencies import resolve_dependencies as _resolve_deps
            from kiro_crew.apps.manifest import Dependencies as _Deps

            deps = _Deps.from_dict(deps_data)
            dep_result = await _resolve_deps(name, deps)
            if dep_result.installed:
                log_lines.append(f"Installed {len(dep_result.installed)} dependency(ies)")
            if dep_result.failed:
                log_lines.append(
                    f"Failed to install {len(dep_result.failed)} dependency(ies): {', '.join(dep_result.failed)}"
                )
            if dep_result.missing:
                log_lines.append(f"Missing commands: {', '.join(dep_result.missing)}")

        # Step 4: Register with KiroCrew
        if is_self_managed:
            # Pre-register with manifest from the cloned repo so the app
            # appears in Installed tab immediately (with openCommand, icon, etc.)
            # The app will update its own registration on next launch.
            manifest_data_raw = None
            try:
                manifest_data_raw = json.loads((app_source / "app.json").read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

            from kiro_crew.apps.manager import register_external_app

            display = (manifest_data_raw or {}).get("displayName", name)
            version = (manifest_data_raw or {}).get("version", "0.0.0")
            register_external_app(
                name=name,
                version=version,
                display_name=display,
                source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                manifest_data=manifest_data_raw,
                origin="registry",
            )

            log_lines.append("Pre-registered from cloned manifest (self-managed)")
            log_lines.append("App will update its own registration on next launch")
            return {
                "ok": True,
                "name": name,
                "message": f"installed {name} from {repo} (self-managed)",
                "log": "\n".join(log_lines),
            }

        # Kirocrew-managed: copy to ~/.kirocrew/apps/ and register resources
        log_lines.append("Installing app...")
        existing = get_app(name)
        if existing:
            result = update_app(str(app_source))
        else:
            result = install_app(str(app_source))
        log_lines.append(result.message or result.error or "done")

        # Mark source as registry-installed
        if result.ok:
            set_app_source(result.name, f"{SOURCE_REGISTRY_PREFIX}{name}")

        return {
            "ok": result.ok,
            "name": name,
            "message": result.message,
            "error": result.error,
            "log": "\n".join(log_lines),
        }

    except Exception as exc:
        logger.exception("Failed to install %s from registry", name)
        return {"ok": False, "name": name, "error": str(exc), "log": "\n".join(log_lines)}
