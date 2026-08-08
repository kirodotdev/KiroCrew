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
import sys
import time
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from kiro_crew.apps import install_receipt
from kiro_crew.apps.admission import app_admission_denied, verified_signer
from kiro_crew.apps.execution import app_execution_denied
from kiro_crew.apps.manager import (
    get_app,
    install_app,
)
from kiro_crew.apps.manager import list_apps as list_installed_apps
from kiro_crew.apps.manager import (
    set_app_provenance,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.sandbox import cgroup_scope_argv, create_subprocess_limited, wrap_argv
from kiro_crew.sel import sel

try:
    from kiro_crew.sel import sel as _sel_fn
except ImportError:
    _sel_fn = None  # type: ignore[assignment]
from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.platform import PlatformCompositionError, current_context

logger = logging.getLogger(__name__)

# Source type prefix for registry-installed apps.
SOURCE_REGISTRY_PREFIX = "registry:"

# A git object name: sha1 (40 hex) or sha256 (64 hex) repository format.
_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


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

# Number of days to retain moved-aside .stale-* / .partial-* checkouts before
# the best-effort sweep removes them.
_STALE_CHECKOUT_RETENTION_DAYS = 7

# Minimal environment for install/uninstall scripts.
# Only pass through variables needed for git, build tools, and shell operation.
# This prevents leaking secrets (API keys, tokens, AWS credentials) from the
# gateway process into app install scripts.
#
# The list is deliberately cross-platform. It was POSIX-only, which does not fail
# loudly on Windows — it fails *early and opaquely*: a Windows child without
# ``SystemRoot`` usually dies before ``main()`` (DLL and crypto init resolve
# through it), and one without ``USERPROFILE`` cannot find a per-user config root
# (for a TeX child, ``TEXMFHOME``). ``TMPDIR`` is the POSIX spelling only, so a
# Windows child also had no writable temp dir. Same key set and same reason as
# ``kiro_prerequisite._SAFE_ENV_KEYS``; kept in the allowlist shape so the
# credential-scrubbing property is unchanged — these are location hints, not
# secrets.
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
        # Windows equivalents of the above. `ProgramFiles` is spelled both ways
        # because Windows env lookups are case-insensitive while `os.environ` on
        # other platforms is not, and this set is matched literally.
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "PATHEXT",
        "ProgramFiles",
        "PROGRAMFILES",
        "SystemRoot",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
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


#: Case-folded view of the allowlist, for the Windows match below.
_SAFE_ENV_KEYS_FOLDED = frozenset(k.upper() for k in _SAFE_ENV_KEYS)


def _is_safe_env_key(key: str) -> bool:
    """Whether *key* is allowlisted, honoring Windows' case-insensitive env.

    On Windows, environment variable names are case-INSENSITIVE and CPython's
    ``os.environ`` upper-cases every key, so ``os.environ.items()`` yields
    ``SYSTEMROOT`` — never the ``SystemRoot`` spelling Microsoft documents and that
    this allowlist (and ``kiro_prerequisite``'s) writes. A literal membership test
    therefore dropped exactly the variables it was extended to carry, and the
    failure is silent at the boundary and fatal in the child: a Windows process
    without ``SystemRoot`` cannot resolve side-by-side assemblies and dies before
    ``main()``.

    Folding on Windows only, rather than upper-casing the list, keeps POSIX exact:
    ``PATH`` and ``Path`` are genuinely different variables there, and a
    case-insensitive match would let a lookalike through.
    """
    if platform_compat.IS_WINDOWS:
        return key.upper() in _SAFE_ENV_KEYS_FOLDED
    return key in _SAFE_ENV_KEYS


def minimal_env(**extra: str) -> dict[str, str]:
    """Build a minimal environment dict from the current process env.

    Only passes through safe keys (PATH, HOME, SSH_AUTH_SOCK, etc.)
    plus any explicit *extra* overrides.  Used by both registry install
    and route-level uninstall handlers.
    """
    env = {k: v for k, v in os.environ.items() if _is_safe_env_key(k)}
    env.update(extra)
    return env


# Env keys that let git present the gateway's *ambient* identity to a remote:
# the SSH agent socket, and any GIT_SSH / GIT_SSH_COMMAND override that could
# route auth through the owner's keys. Stripped for index-originated clones.
_GIT_CREDENTIAL_ENV_KEYS = frozenset(
    {"SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND"}
)


def anonymous_git_env(**extra: str) -> dict[str, str]:
    """Env for an INDEX-ORIGINATED (automatic, browse/refresh-time) git clone.

    Confused-deputy defense (companion to :func:`is_clone_host_trusted`): the
    clone-host trust gate is deliberately **host-granular**, so a host the owner
    configured for one registry (e.g. their internal forge) is trusted wholesale.
    A configured registry's ``app-registry.json`` is UNTRUSTED content, so it can
    list an app whose ``repo`` points at a *sibling* private repo on that same
    trusted host. The manifest/blob-proxy paths clone such repos **automatically**
    on browse/refresh — with no per-repo owner action — so cloning them with the
    gateway's ambient git/ssh identity would be a confused-deputy read of a
    private sibling repo, surfaced back through the App Store. Such automatic
    clones therefore run **credential-free / anonymous**:

    - drop the SSH agent + ``GIT_SSH``/``GIT_SSH_COMMAND`` passthrough
      (``_GIT_CREDENTIAL_ENV_KEYS``) so no ssh key/agent is ever offered;
    - disable system **and** global git config (``GIT_CONFIG_NOSYSTEM=1`` +
      ``GIT_CONFIG_GLOBAL=os.devnull``) so no HTTPS credential helper fires;
    - never prompt (``GIT_TERMINAL_PROMPT=0``, plus a batch-mode
      ``GIT_SSH_COMMAND`` with no identity/agent) so a private repo simply fails
      to clone (→ graceful fallback) instead of authenticating as the gateway.

    Callers must ALSO pass ``mode="strict"`` to :func:`wrap_argv` so the OS
    sandbox hides ``~/.ssh`` — env suppression and the sandbox are belt-and-
    suspenders on the same credential-free property.

    Credential posture by clone origin (all four paths gate on
    :func:`is_clone_host_trusted` first):

    - **Automatic** browse/refresh clones (manifest + blob proxy) — always
      credential-free / anonymous (this function), because no per-repo owner
      action gates them.
    - **Index-originated installs** — an app whose registry entry came from an
      owner-configured *external* index (carries ``_registry``): the ``repo``
      URL is index-controlled, so the install clone is ALSO credential-free
      (``anonymous_git_env`` + strict sandbox); the owner designated the index
      URL, not the app's repo. See :func:`_git_clone_or_pull`'s
      ``index_originated`` flag.
    - **Bundled / owner-designated installs** — the curated bundled registry (no
      ``_registry`` marker) and fetching the owner's own configured registry
      index keep full credentials via :func:`minimal_env`; those repos are
      deliberately owner-designated.
    """
    # The credential-suppression set is compared UPPER-CASED for the same reason
    # `_is_safe_env_key` folds: on Windows `os.environ` yields upper-cased keys, and
    # here a missed match would be the dangerous direction — it would PASS a
    # credential-bearing variable (`SSH_AUTH_SOCK`) that this function exists to
    # strip. These four are already upper-case, so the fold is a no-op today and a
    # guard against a future mixed-case entry.
    env = {
        k: v
        for k, v in os.environ.items()
        if _is_safe_env_key(k) and k.upper() not in _GIT_CREDENTIAL_ENV_KEYS
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    # If a trusted-host remote is nonetheless SSH, force batch mode with no
    # identity/agent so it can't silently authenticate as the gateway.
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none"
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
    neither yields something that looks cloneable — including when an
    index-controlled value is not a string at all (an object-valued ``gitUrl``
    from a malformed external index must degrade to "no URL", never crash the
    caller).
    """
    raw = entry.get("gitUrl") or entry.get("repo") or ""
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _sel_credential_grant(operation: str, git_url: str) -> None:
    """SEL-audit an owner-designated credential grant (best-effort).

    The same-repo carve-out escalates a clone from anonymous+strict to
    owner credentials + context sandbox. That is a security-relevant
    permission decision and must leave an audit record, mirroring the
    existing ``fetch_external_registry`` SEL events.
    """
    if _sel_fn is None:
        return
    try:
        _sel_fn().log_api_access(
            caller="registry",
            operation=operation,
            outcome="granted",
            resources=f"owner_designated_clone url={git_url}",
        )
    except Exception as exc:
        logger.debug("SEL audit log failed for %s: %s", operation, exc)


def _is_owner_designated_repo(entry: dict[str, Any]) -> bool:
    """True when an index entry's clone URL is the owner-configured registry repo.

    Same-repo credential carve-out: the confused-deputy defense (anonymous env +
    strict sandbox) exists because an *untrusted index* can point at a private
    sibling repo on the owner's trusted forge. When the entry's effective clone
    URL is **byte-identical** to the owner-typed ``ExternalRegistryConfig.repo``,
    the confused-deputy argument does not apply — the owner explicitly designated
    exactly that URL by adding the registry. Such entries may use owner
    credentials (``minimal_env`` + context sandbox mode) instead of the
    anonymous+strict posture.

    Security boundary:
      - Compares against the **config-stored** repo URL, never against
        index-supplied fields — the index can ``setdefault`` the repo field,
        but an explicit override by the index will NOT match the config URL.
      - Exact string equality only; no normalization, no host-level matching
        (host-granular trust is exactly the confused-deputy hole this defense
        exists for).
      - ``subdirectory`` remains untrusted: ``_contained_join`` containment
        checks are unaffected by this predicate.
    """
    registry_name = entry.get("_registry")
    if not registry_name:
        # Not from an external index — bundled entries are already
        # owner-designated via the absence of ``_registry``.
        return False

    effective_url = _entry_git_url(entry)
    if not effective_url:
        return False

    # Look up the owner-configured registry repo URL from config.
    config = KiroCrewConfig.load()
    for reg in config.registries or []:
        reg_key = reg.name or reg.repo
        if reg_key == registry_name:
            # Byte-identical comparison — the security contract.
            return effective_url == reg.repo
    return False


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


def is_clone_host_trusted(git_url: str) -> bool:
    """SSRF gate: is *git_url*'s host one the owner explicitly trusts to clone?

    The trust set is the well-known public forges (``_PUBLIC_GIT_HOSTS``, plus
    any a companion contributes) UNION the hosts of the owner's
    explicitly-configured external registries (``_configured_registry_hosts``).

    Why this exists: registry ``repo`` fields are now full git URLs, and a
    configured external (federated) registry's ``app-registry.json`` is
    UNTRUSTED content — it can list an app whose ``repo`` points at an internal
    address (e.g. ``https://127.0.0.1:8443/x``) or any attacker-controlled host.
    Such a value passes ``_is_safe_repo_identifier`` and enters the blob-proxy
    allowlist (``known_registry_repos``), so without this gate merely browsing
    the App Store would drive ``git clone`` against the loopback/internal
    network — an authenticated backend SSRF. Constraining every URL clone to an
    explicitly-trusted HOST closes that vector and is immune to DNS rebinding:
    the hostname itself must be trusted, not its (re-resolvable) IP. An
    owner-configured internal forge (e.g. self-hosted GitLab at a private IP)
    stays allowed precisely because the owner added it; an index-injected host
    never is.

    Bare-name legacy repos (no URL host) return ``False`` here and are handled
    by the bundled-registry allowlist — they never reach a URL clone. Fails
    CLOSED: an unparseable/hostless URL is untrusted.
    """
    host = _git_url_host(git_url)
    if not host:
        return False
    try:
        policy = current_context().registry
        trusted = frozenset(policy.public_git_hosts()) | _configured_registry_hosts()
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("clone-host trust set via context failed; using default", exc_info=True)
        trusted = _PUBLIC_GIT_HOSTS | _configured_registry_hosts()
    return host in trusted


def _edition_registry_rows() -> list[dict[str, Any]]:
    """Edition-contributed App-Store rows (CPP seam), fail-closed to []."""
    from kiro_crew.platform.context import safe_context_call

    def _read() -> list[dict[str, Any]]:
        rows = current_context().apps_loader.registry_rows()
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]

    return safe_context_call(
        _read,
        fallback_factory=list,
        log_message="edition registry_rows lookup failed; using bundled only",
    )


def _load_registry_file() -> list[dict[str, Any]]:
    """Load and parse the bundled app-registry.json, then merge edition rows.

    Edition rows (from the CPP ``AppsLoader.registry_rows`` seam) are appended
    ADD-only: a bundled core row wins over a same-``name`` edition row, so a
    companion can only add catalog entries, never repoint a core one. The public
    edition contributes none, so the merged list equals the bundled file.
    """
    rows: list[dict[str, Any]] = []
    if not _REGISTRY_FILE.is_file():
        logger.warning("Registry file not found: %s", _REGISTRY_FILE)
    else:
        try:
            data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows = data
            else:
                logger.warning("Registry file is not a JSON array")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load registry: %s", exc)

    seen = {r.get("name") for r in rows if isinstance(r, dict)}
    for row in _edition_registry_rows():
        if row.get("name") in seen:
            continue
        rows.append(row)
        seen.add(row.get("name"))
    return rows


# ---------------------------------------------------------------------------
# Remote manifest fetching + caching
# ---------------------------------------------------------------------------


def _safe_cache_stem(name: str) -> str:
    """Map an arbitrary registry/app name to a filesystem-safe cache stem.

    Pure-safe names (``[A-Za-z0-9_.\\-]``, no ``..``) are returned byte-identical
    so existing caches stay valid. Any name carrying disallowed characters —
    crucially path separators or ``..`` traversal supplied by an external
    registry entry (e.g. ``../../config``) — is slugified AND disambiguated with
    a short stable hash of the ORIGINAL name, so the derived path can never
    escape ``_manifest_cache_dir()`` nor collide with another name.
    """
    if ".." not in name and re.match(r"^[A-Za-z0-9_.\-]+$", name):
        return name
    slug = re.sub(r"[^A-Za-z0-9_\-]+", "-", name).strip("-") or "app"
    digest = sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _manifest_cache_path(name: str) -> Path:
    # Sanitize the name so a hostile/traversal entry name from an external
    # registry can never resolve outside the manifest cache dir (read, write,
    # AND delete all go through here, so they stay mutually consistent).
    return _manifest_cache_dir() / f"{_safe_cache_stem(name)}.json"


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


def _is_safe_registry_subdir(subdir: Any) -> bool:
    """True if *subdir* is a safe, contained relative path for a registry entry.

    An external registry index is untrusted and controls the entire entry,
    including ``subdirectory`` — which is later joined to the throwaway clone
    dir, the persistent app-source dir, and the manifest read path. An absolute
    or ``..`` value would escape those roots and let an attacker-selected
    ``app.json`` (→ ``setup.onInstall``) be read/executed. Empty/missing means
    the repo root (safe). Rejects non-strings, NUL, backslashes (Windows/UNC
    separators), absolute paths (POSIX ``/…`` or drive-letter ``C:…``), and any
    ``.``/``..`` path segment. Purely lexical; the use-site
    :func:`_contained_join` adds a symlink-resolving containment check as
    defense-in-depth.
    """
    if subdir in (None, ""):
        return True
    if not isinstance(subdir, str):
        return False
    if "\x00" in subdir or "\\" in subdir:
        return False
    if subdir.startswith("/") or (len(subdir) >= 2 and subdir[1] == ":"):
        return False
    return not any(seg in ("..", ".") for seg in subdir.split("/"))


def _contained_join(root: Path, subdir: str) -> Path | None:
    """Join *subdir* under *root*, returning the symlink-resolved result only if
    it stays within *root*; ``None`` on any escape.

    Defense-in-depth companion to :func:`_is_safe_registry_subdir`: the lexical
    gate rejects ``..``/absolute values before an entry is cached/listed, and
    this resolves symlinks so a hostile clone containing e.g. ``sub -> /etc``
    cannot smuggle a read outside the clone root at use time. Returns *root*
    unchanged for an empty *subdir*.
    """
    if not subdir:
        return root
    try:
        base = root.resolve()
        target = (root / subdir).resolve()
    except OSError:
        return None
    return target if target.is_relative_to(base) else None


async def _fetch_app_manifest(
    repo: str,
    branch: str,
    subdirectory: str = "",
    app_name: str = "",
    git_url: str = "",
    *,
    owner_designated: bool = False,
) -> dict[str, Any] | None:
    """Fetch app.json for an app from its source repo (lightweight).

    Tries, in order:
      1. The persistent clone under ``~/.kiro/crew/app-sources/{app_name}/``
         (if the app was already cloned by a previous install).
      2. A throwaway shallow clone of *git_url* into a temp directory, from
         which only ``app.json`` is read (the clone is then discarded).

    Returns the parsed app.json dict, or None on failure.  All failures are
    swallowed (returns None) so a missing/unreachable repo never crashes the
    listing path on a vanilla machine. *subdirectory* is an untrusted
    index-controlled value; it is joined via :func:`_contained_join` so an
    absolute/``..``/symlink value can never read outside the clone root.

    *owner_designated*: when True (same-repo credential carve-out), the
    clone uses ``minimal_env()`` + context sandbox mode instead of the
    default anonymous+strict posture. Only set when the entry's effective
    clone URL is byte-identical to the owner-configured registry repo URL.
    """
    if not git_url:
        git_url = repo

    # Try persistent clone first (already installed).
    #
    # The persisted clone is keyed on app NAME only, so a registry replacement
    # can leave a checkout of a DIFFERENT repo sitting here under the same
    # name. Its app.json must not stand in for the manifest of the repo we are
    # about to clone: the caller feeds this manifest to the admission gate, and
    # the install that follows discards a stale checkout and re-clones from
    # *git_url* (see _git_clone_or_pull). Trusting the stale copy would admit
    # repo A's manifest and then run repo B's code. So the local copy is only
    # used when the clone's origin still is git_url; otherwise fall through to
    # the throwaway clone of git_url, which always describes what gets cloned.
    if app_name:
        clone_dir = app_source_dir(app_name)
        manifest_dir = _contained_join(clone_dir, subdirectory)
        local_manifest = manifest_dir / "app.json" if manifest_dir is not None else None
        if (
            local_manifest is not None
            and local_manifest.is_file()
            and await _clone_origin_matches(clone_dir, git_url)
            and await _clone_branch_matches(clone_dir, branch)
        ):
            try:
                content = await asyncio.to_thread(local_manifest.read_text, "utf-8")
                return json.loads(content)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass

    if not _looks_like_git_url(git_url):
        # Not a cloneable URL (e.g. empty or a bare name on a public machine).
        return None
    # SSRF gate: only clone from explicitly-trusted hosts. An untrusted external
    # registry index can list an app repo pointing at an internal address; this
    # listing path clones automatically, so it must not honor such a host.
    # is_clone_host_trusted() loads config from disk (KiroCrewConfig.load), so
    # run it off the event loop to avoid blocking all gateway tasks.
    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        logger.debug(
            "manifest clone refused for %r: host not in trusted forge/registry set (SSRF gate)",
            git_url,
        )
        return None

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
        # Credential posture for the manifest clone. Default: anonymous+strict
        # (confused-deputy defense — see anonymous_git_env). Same-repo
        # carve-out: when owner_designated is True the clone URL is the
        # owner-configured registry repo itself, so the confused-deputy
        # argument does not apply — use owner credentials + context sandbox.
        if owner_designated:
            clone_env = minimal_env()
            sandbox_mode = _context_clone_sandbox_mode(git_url)
            _sel_credential_grant("fetch_app_manifest", git_url)
        else:
            clone_env = anonymous_git_env()
            sandbox_mode = "strict"
        sandboxed_cmd, _cleanup = wrap_argv(clone_cmd, mode=sandbox_mode)
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clone_env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        _, stderr = await _communicate_with_timeout(proc, timeout=_CLONE_TIMEOUT)
        if proc.returncode != 0:
            logger.debug(
                "manifest clone failed for %s: %s",
                git_url,
                stderr.decode(errors="replace").strip(),
            )
            return None
        manifest_dir = _contained_join(Path(tmp_root), subdirectory)
        if manifest_dir is None:
            # Untrusted index subdirectory escaped the clone root (absolute,
            # ``..``, or a symlink resolving outside tmp_root) — refuse.
            return None
        manifest_path = manifest_dir / "app.json"
        if not manifest_path.is_file():
            return None
        content = await asyncio.to_thread(manifest_path.read_text, "utf-8")
        return json.loads(content)
    except (asyncio.TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
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
    branch = entry.get("branch", "main")
    subdirectory = entry.get("subdirectory", "")
    git_url = _entry_git_url(entry)

    if not git_url:
        return entry

    # Try cache first
    cached = await asyncio.to_thread(_read_manifest_cache, name)
    if cached:
        return _merge_manifest(entry, cached)

    # Fetch from repo
    # Same-repo credential carve-out: if the entry's clone URL matches the
    # owner-configured registry repo, use owner credentials for the manifest
    # fetch (the confused-deputy defense does not apply to the owner's own URL).
    is_owner_repo = await asyncio.to_thread(_is_owner_designated_repo, entry)
    manifest = await _fetch_app_manifest(
        repo,
        branch,
        subdirectory,
        app_name=name,
        git_url=git_url,
        owner_designated=is_owner_repo,
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
        result["screenshotsDark"] = [
            f"/api/apps/blob?repo={repo}&path={p}" for p in screenshots_dark
        ]

    # Hero images — convert repo-relative paths to blob proxy URLs
    hero = manifest.get("heroImage", "")
    if hero and repo:
        result["heroImage"] = f"/api/apps/blob?repo={repo}&path={hero}"
    hero_dark = manifest.get("heroImageDark", "")
    if hero_dark and repo:
        result["heroImageDark"] = f"/api/apps/blob?repo={repo}&path={hero_dark}"
    # Detail-page hero images (wide banner ratio) — convert repo-relative paths
    # to blob proxy URLs. The detail page prefers these over the (near-square)
    # Browse-card hero so the wide banner isn't cropped.
    hero_detail = manifest.get("heroImageDetail", "")
    if hero_detail and repo:
        result["heroImageDetail"] = f"/api/apps/blob?repo={repo}&path={hero_detail}"
    hero_detail_dark = manifest.get("heroImageDetailDark", "")
    if hero_detail_dark and repo:
        result["heroImageDetailDark"] = f"/api/apps/blob?repo={repo}&path={hero_detail_dark}"

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

    # Pure-safe names keep the historical byte-identical path (no hash suffix)
    # so existing caches stay valid. Names carrying disallowed characters (e.g.
    # URL-derived registry names) are slugified AND disambiguated with a short
    # stable hash of the ORIGINAL name, so two distinct such names can never
    # clobber the same ``_registry_<name>.json`` cache file.
    if re.match(r"^[A-Za-z0-9_\-]+$", name):
        safe = name
    else:
        slug = re.sub(r"[^A-Za-z0-9_\-]+", "-", name).strip("-") or "registry"
        digest = sha256(name.encode("utf-8")).hexdigest()[:8]
        safe = f"{slug}-{digest}"
    return _manifest_cache_dir() / f"_registry_{safe}.json"


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
        if not isinstance(data, list):
            return None
        # Path-safety gate on EVERY cache read (not just fresh fetches). A cache
        # file written by an older build — or hand-tampered — may contain an
        # entry whose name is not valid kebab-case (e.g. ``../../victim``). Such
        # a name would otherwise flow through list_registry ->
        # install_from_registry -> ``app_source_dir(name)`` and let a failed
        # clone's ``shutil.rmtree(dest)`` escape the app-sources root. Fresh
        # fetches are already filtered before write; re-filter here so cached
        # and stale-fallback reads can never reintroduce a traversing name.
        from kiro_crew.apps.manifest import KEBAB_RE

        safe: list[dict[str, Any]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            entry_name = entry.get("name")
            if not isinstance(entry_name, str) or not KEBAB_RE.match(entry_name):
                logger.warning(
                    "Dropping cached external registry %s entry with invalid "
                    "name %r (must be lowercase kebab-case)",
                    name,
                    entry_name,
                )
                continue
            # ``subdirectory`` is untrusted index content joined to the clone /
            # app-source roots; drop any entry whose value is absolute or
            # traversing so it can never reach a filesystem op (same rationale
            # as the name gate above). Fresh fetches are filtered before write;
            # re-filter here so a cached/stale/hand-tampered file cannot
            # reintroduce a traversing subdirectory.
            if not _is_safe_registry_subdir(entry.get("subdirectory", "")):
                logger.warning(
                    "Dropping cached external registry %s entry %r with unsafe "
                    "subdirectory %r (must be a contained relative path)",
                    name,
                    entry_name,
                    entry.get("subdirectory"),
                )
                continue
            safe.append(entry)
        return safe
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
    """Communicate with a subprocess, killing its whole process tree on timeout.

    A timed-out ``git clone`` or ``/bin/sh -c <probe>`` can have descendants
    (SSH, a version-probe binary, ...). Killing only the immediate child with
    ``proc.kill()`` re-parents those grandchildren, so repeated timeouts leak
    processes. We instead signal the child's entire process group via
    ``platform_compat.kill_process_tree_async`` (killpg on POSIX, ``taskkill
    /T`` on Windows) and then reap the direct child. Callers MUST spawn the
    child with ``start_new_session`` (POSIX) / ``CREATE_NEW_PROCESS_GROUP``
    (Windows) so the group signal targets the child's own group and not the
    gateway's — every caller in this module does. If the group kill fails
    (e.g. the child already exited, or it was never made a group leader) we
    fall back to a pid-scoped ``proc.kill()`` so the child is never left
    un-reaped.
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
        except OSError:
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
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_env(),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
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
                    # Keep only well-formed object entries — a malformed index
                    # item (e.g. a bare string) must never reach normalization.
                    _sel_outcome("success")
                    return [item for item in data if isinstance(item, dict)]
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
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


async def _fetch_and_cache_external_registry(reg) -> list[dict[str, Any]] | None:
    """Fetch a registry's index, normalize entries, and write the cache.

    Returns the fresh entries on success (cache overwritten), or ``None`` on a
    fetch failure — in which case the caller decides whether to fall back to a
    stale cache. Because the cache is only overwritten on success, a transient
    forge/network failure leaves the prior (stale) cache intact ("stale >
    missing"): this is the fetch-then-swap contract the refresh path relies on.
    """
    name = reg.name or reg.repo
    entries = await _fetch_external_registry_index(reg.repo, reg.branch)
    if entries is None:
        return None
    # Defensively drop malformed (non-dict) index items before normalization:
    # a configured repo can return a valid JSON array containing a non-object
    # (e.g. ``["oops"]``), and ``entry.setdefault(...)`` on a str would raise
    # AttributeError — which, on the refresh path, escapes as an HTTP 500.
    entries = [e for e in entries if isinstance(e, dict)]
    # Path-safety gate: an external registry index is untrusted input. A
    # hostile/typo entry name such as ``/tmp/victim`` or ``../../victim`` would
    # otherwise flow through list_registry -> install_from_registry ->
    # ``app_source_dir(name)`` (which does ``_app_sources_dir() / name`` — an
    # absolute or traversing name escapes the app-sources root), and on a failed
    # clone ``_git_clone_or_pull`` calls ``shutil.rmtree(dest)`` on that
    # attacker-selected path. Reject any entry whose name is not a valid
    # kebab-case app name (the same KEBAB_RE gate install/register already
    # enforce) BEFORE it is cached or listed, so a malicious name can never
    # reach a filesystem operation.
    from kiro_crew.apps.manifest import KEBAB_RE

    valid_entries: list[dict[str, Any]] = []
    for entry in entries:
        entry_name = entry.get("name")
        if not isinstance(entry_name, str) or not KEBAB_RE.match(entry_name):
            logger.warning(
                "Dropping external registry %s entry with invalid name %r "
                "(must be lowercase kebab-case)",
                name,
                entry_name,
            )
            continue
        # ``subdirectory`` is untrusted index content later joined to the clone
        # and persistent app-source roots; an absolute/``..`` value would escape
        # them and read/execute an attacker-selected app.json. Drop it before it
        # is cached or listed (defense-in-depth with _contained_join at use).
        if not _is_safe_registry_subdir(entry.get("subdirectory", "")):
            logger.warning(
                "Dropping external registry %s entry %r with unsafe subdirectory "
                "%r (must be a contained relative path)",
                name,
                entry_name,
                entry.get("subdirectory"),
            )
            continue
        valid_entries.append(entry)
    entries = valid_entries
    # Ensure each entry has gitUrl/repo/branch set (for install_from_registry)
    for entry in entries:
        entry.setdefault("gitUrl", reg.repo)
        entry.setdefault("repo", reg.repo)
        entry.setdefault("branch", reg.branch)
        entry["_registry"] = name
    await asyncio.to_thread(_write_external_registry_cache, name, entries)
    return entries


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

        # Try cache first
        cached = await asyncio.to_thread(_read_external_registry_cache, name)
        if cached is not None:
            for entry in cached:
                entry["_registry"] = name
            return cached

        # Fetch from repo (writes the cache on success).
        entries = await _fetch_and_cache_external_registry(reg)
        if entries is not None:
            return entries

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
        logger.warning("Failed to load external registry %s from %s", name, reg.repo)
        return []

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


def _expire_cache_file(path: Path) -> None:
    """Backdate a cache file's mtime so it reads as stale (best-effort).

    Preferred over unlinking: a subsequent read treats the file as expired and
    refetches, but the data survives on disk as a stale-fallback if that
    refetch fails — so a refresh during a forge/network blip degrades to
    "slightly stale" instead of "apps vanished". Missing file is a no-op.

    Defense-in-depth: the resolved path must stay inside the manifest cache
    dir; anything else (a traversal-derived path) is ignored rather than
    touched. In practice ``_manifest_cache_path`` already sanitizes names, so
    this only guards against future callers.
    """
    try:
        cache_dir = _manifest_cache_dir().resolve()
        resolved = path.resolve()
        if cache_dir not in resolved.parents:
            logger.warning("Refusing to expire cache file outside cache dir: %s", path)
            return
        past = time.time() - max(_MANIFEST_CACHE_TTL, _EXTERNAL_REGISTRY_CACHE_TTL) - 3600
        os.utime(resolved, (past, past))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Failed to expire cache file %s: %s", path, exc)


async def refresh_registries(repo: str | None = None) -> dict[str, Any]:
    """Refetch external-registry caches (fetch-then-swap) and re-warm.

    For every configured registry (or just the one whose ``.repo`` matches
    *repo*), refetches its index and — only on a successful fetch — overwrites
    the cache and expires the per-app manifest caches its entries contributed
    (via mtime backdating, so a failed manifest refetch still falls back to the
    stale copy). A registry whose refetch FAILS keeps its existing cache intact
    and is reported in ``failed`` rather than silently reported as synced.

    Returns ``{ok, refreshed, failed, results, apps, lastSyncedAt}`` where
    ``ok`` is True only if every matched registry refreshed successfully and
    ``results`` carries the per-registry outcome so the UI can distinguish
    "synced" from "sync failed, serving stale". When *repo* is supplied but
    matches no configured registry, returns ``ok: False`` with
    ``not_found: True`` so the route can map it to HTTP 404.
    """
    from kiro_crew.config.loader import (
        KiroCrewConfig,  # deferred: loader imports apps/ at module level
    )

    config = await asyncio.to_thread(KiroCrewConfig.load)
    registries = list(config.registries or [])
    if repo:
        registries = [r for r in registries if r.repo == repo]
        # A caller-supplied ``repo`` that matches no configured registry is a
        # client error, not a silent success: refreshing nothing and returning
        # ``ok: true`` would let an API client believe a sync happened when the
        # target does not exist. Signal not-found so the route maps it to 404.
        if not registries:
            return {
                "ok": False,
                "not_found": True,
                "refreshed": [],
                "failed": [],
                "results": [],
                "apps": 0,
                "lastSyncedAt": datetime.now(timezone.utc).isoformat(),
            }

    refreshed: list[str] = []
    failed: list[str] = []
    results: list[dict[str, Any]] = []
    for reg in registries:
        name = reg.name or reg.repo
        # Read the (possibly stale) prior index up front so we know which
        # per-app manifest caches this registry contributed, even if the
        # refetch changes/removes some entries.
        prior = await asyncio.to_thread(_read_external_registry_cache, name, ignore_ttl=True)
        # Fetch-then-swap: the cache is overwritten only on a successful fetch.
        entries = await _fetch_and_cache_external_registry(reg)
        if entries is None:
            failed.append(name)
            results.append({"name": name, "ok": False})
            continue
        # Expire per-app manifest caches so fresh display info is refetched
        # lazily on the next read (mtime expiry preserves the stale fallback).
        manifest_names: set[str] = set()
        for e in (prior or []) + entries:
            entry_name = e.get("name")
            if isinstance(entry_name, str) and entry_name:
                manifest_names.add(entry_name)
        for entry_name in manifest_names:
            await asyncio.to_thread(_expire_cache_file, _manifest_cache_path(entry_name))
        refreshed.append(name)
        results.append({"name": name, "ok": True})

    # Re-warm so the response's app count reflects post-refresh state (and
    # untouched registries read their still-valid caches).
    apps = await list_registry()

    return {
        "ok": not failed,
        "refreshed": refreshed,
        "failed": failed,
        "results": results,
        "apps": len(apps),
        "lastSyncedAt": datetime.now(timezone.utc).isoformat(),
    }


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
        denied = app_execution_denied(name, action="registry_detect_installed", caller="registry")
        if denied:
            logger.debug("Skipping registry detectInstalled for %s: %s", name, denied)
            continue
        try:

            base_cmd = ["/bin/sh", "-c", detect_cmd]
            sandboxed_cmd, _cleanup = wrap_argv(base_cmd, mode="strict")
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            await _communicate_with_timeout(proc, timeout=5)
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
                    # Old cache files may predate persisted origin tags. Restore
                    # the authoritative discriminator at the lookup boundary so
                    # privacy gates never mistake a custom source for official.
                    return {**entry, "_registry": reg_name}
    return None


def _registry_app_candidates(name: str) -> list[dict[str, Any]]:
    """Every catalog row named *name*: bundled first, then each configured
    registry in config order.

    :func:`get_registry_app` returns only the FIRST match, which is precisely
    what lets a same-named row from another source answer for an app installed
    from somewhere else.  Provenance-pinned resolution needs the full candidate
    set so it can select the row the app is actually pinned to.
    """
    candidates = [
        entry
        for entry in _load_registry_file()
        if isinstance(entry, dict) and entry.get("name") == name
    ]
    from kiro_crew.config.loader import (
        KiroCrewConfig,  # circular import: loader.py imports from apps/ at module level; deferring avoids ImportError
    )

    for reg in KiroCrewConfig.load().registries:
        cached = _read_external_registry_cache(reg.name or reg.repo, ignore_ttl=True)
        for entry in cached or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                candidates.append(entry)
    return candidates


def _pinned_registry_entry(name: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    """Select the catalog row an installed app's recorded provenance pins it to.

    A row matches only when BOTH the clone URL and the originating registry id
    equal what was recorded at install time, so neither a row that reuses the
    name on a different repo nor a different registry publishing the same
    name/URL pair can stand in for the pinned source.  Returns None when no
    candidate matches.
    """
    want_url = str(meta.get("sourceUrl", "") or "")
    want_registry = str(meta.get("sourceRegistry", "") or "")
    for entry in _registry_app_candidates(name):
        if _entry_git_url(entry) != want_url:
            continue
        if str(entry.get("_registry", "") or "") != want_registry:
            continue
        return entry
    return None


def _resolve_install_entry(name: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve the catalog row that ``install_from_registry`` may act on.

    Fresh installs — and legacy records that predate provenance capture, which
    carry only the bare ``registry:<name>`` marker — keep the historical
    first-match-wins :func:`get_registry_app` lookup, so no migration is needed
    and today's behaviour is unchanged for them.  An installed app that DOES
    carry provenance is pinned to it: its update must come from the source it was
    installed from, never from whichever same-named row happens to resolve first.

    Blocking (reads installed metadata, config, and index caches) — call it off
    the event loop.  Returns ``(entry, error)``; a non-empty *error* means the
    caller must refuse, and must NOT fall back to a bare-name lookup.
    """
    meta = get_app(name) or {}
    pinned_url = str(meta.get("sourceUrl", "") or "")
    if not pinned_url:
        return get_registry_app(name), ""
    entry = _pinned_registry_entry(name, meta)
    if entry is None:
        return None, (
            f"app {name!r} was installed from {pinned_url} and no registry entry "
            f"currently offers that source — refusing to update it from a different source"
        )
    return entry, ""


def _external_registry_app_by_repo(repo: str) -> dict[str, Any] | None:
    """Look up an app entry by repo across the user's external (federated)
    registries, reading local sync caches only (``ignore_ttl`` so a stale index
    still resolves) — never fetches, so it is safe to call from the per-request
    blob-proxy worker. Fails open to ``None``."""
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: loader.py imports from apps/ at module level; deferring avoids ImportError
        )

        for reg in KiroCrewConfig.load().registries:
            cached = _read_external_registry_cache(reg.name or reg.repo, ignore_ttl=True)
            for entry in cached or []:
                if isinstance(entry, dict) and entry.get("repo") == repo:
                    return entry
    except Exception:  # fail open: branch resolution must never break blob serving
        logger.debug("_external_registry_app_by_repo: read failed", exc_info=True)
    return None


def get_registry_app_by_repo(repo: str) -> dict[str, Any] | None:
    """Look up a registry app by repo name (for blob proxy branch lookup).

    Searches the bundled registry first, then the user's external (federated)
    registries — matching ``known_registry_repos()``'s union — so an
    external-registry app pinned to a non-``main`` branch resolves the correct
    ref in the ``/api/apps/blob`` branch fallback instead of silently 403ing.
    """
    for entry in _load_registry_file():
        if entry.get("repo") == repo:
            return entry
    return _external_registry_app_by_repo(repo)


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
    """Return ~/.kiro/crew/app-sources/{name}/ — persistent clone directory."""
    return _app_sources_dir() / name


def _resolved_clone_commit(clone_root: Path) -> str:
    """Return the commit SHA checked out in *clone_root*, or ``""`` if unknown.

    Reads git's own on-disk refs rather than spawning ``git rev-parse``: the SHA
    is recorded as provenance only, so resolving it must not add a subprocess —
    nor a new failure mode — to the install path.  Every read failure degrades to
    ``""`` (provenance without a commit) instead of failing the install.
    """
    git_dir = clone_root / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if not head.startswith("ref:"):
        # Detached HEAD holds the SHA directly.
        return head if _COMMIT_SHA_RE.match(head) else ""
    ref = head[len("ref:") :].strip()
    # git writes this file, not the cloned repo — belt-and-braces so a ref can
    # never be read as a path outside the clone's own .git directory.
    if not ref or ref.startswith("/") or ".." in ref.split("/"):
        return ""
    try:
        loose = (git_dir / ref).read_text(encoding="utf-8").strip()
        if _COMMIT_SHA_RE.match(loose):
            return loose
    except (OSError, UnicodeDecodeError):
        pass
    # A repacked clone keeps no loose ref file.
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and _COMMIT_SHA_RE.match(parts[0]):
                return parts[0]
    except (OSError, UnicodeDecodeError):
        pass
    return ""


async def _refuse_identity_mismatch(
    entry_name: str,
    cloned_name: str,
    repo: str,
    clone_root: Path,
    log_lines: list[str],
    *,
    created_this_run: bool,
    pre_pull_commit: str = "",
    manifest_relpath: str = "app.json",
    manifest_snapshot: bytes | None = None,
    restore_from: Path | None = None,
) -> dict[str, Any]:
    """Abort an install whose cloned repo claims a different app name.

    A checkout **created by this run** is deleted so the squatting source (and
    any build output) leaves no residue in the entry's ``app-sources/`` slot — a
    leftover would also be preferred by :func:`_fetch_app_manifest` on the next
    listing, letting a refused repo keep answering as this app.  Nothing has
    been written under ``~/.kiro/crew/apps/`` at this point, so removing the
    fresh clone leaves the machine exactly as it was before the install.

    A checkout that **pre-existed** (the update path — ``git pull`` brought in a
    commit whose manifest renamed itself, or a build/script rewrote it in the
    working tree) is the installed app's source workspace, so it is preserved —
    but rolled back to its last-good state (``git reset --keep`` to the
    pre-pull commit plus a manifest restore from HEAD, both edit-preserving):
    left at the renamed manifest, the prefetch would re-read it and re-reject
    every retry before a fixed remote could ever be pulled.
    """
    declared = cloned_name or "<missing>"
    if not created_this_run:
        log_lines.append(
            "Preserving pre-existing source checkout (rolled back to its "
            "last-good state): the refused update installed nothing, and the "
            "workspace belongs to the already-installed app"
        )
    await _unpoison_rejected_checkout(
        entry_name,
        clone_root,
        log_lines,
        checkout_preexisted=not created_this_run,
        pre_pull_commit=pre_pull_commit,
        manifest_relpath=manifest_relpath,
        manifest_snapshot=manifest_snapshot,
        restore_from=restore_from,
    )
    error = (
        f"registry entry {entry_name!r} resolves to a repo whose app.json declares "
        f"{declared!r} — refusing to install an app under an identity that differs "
        f"from its registry entry"
    )
    log_lines.append(f"Refusing install: {error}")
    try:
        sel().log_api_access(
            caller="app_install_from_registry",
            operation="identity_mismatch",
            outcome="rejected",
            resources=f"name={entry_name!r} declared={declared!r} repo={repo}",
            error="cloned manifest name does not match registry entry name",
        )
    except Exception as exc:  # an audit failure must never mask the refusal
        logger.debug("SEL audit failed for %s identity mismatch: %s", entry_name, exc)
    return {"ok": False, "name": entry_name, "error": error, "log": "\n".join(log_lines)}


# ---------------------------------------------------------------------------
# Stale-checkout sweep — removes .stale-* / .partial-* siblings under
# app-sources that are older than _STALE_CHECKOUT_RETENTION_DAYS.
# ---------------------------------------------------------------------------

_STALE_CHECKOUT_PATTERN = re.compile(r"^.+\.(stale|partial)-[0-9a-f]{8}$")


def _is_stale_candidate(p: Path) -> bool:
    """Return True if *p* matches the .stale-*/.partial-* naming convention."""
    return bool(_STALE_CHECKOUT_PATTERN.match(p.name))


def _sweep_stale_checkouts_sync(sources_dir: Path, now_ts: float) -> list[str]:
    """Synchronous sweep of aged stale/partial dirs (runs in a thread).

    Returns a list of removed directory names (for logging).
    Only targets immediate children of *sources_dir* whose names match the
    fixed naming pattern AND whose mtime is older than the retention window.
    Symlinks pointing outside *sources_dir* are skipped (containment check).
    """
    if not sources_dir.is_dir():
        return []
    cutoff = now_ts - (_STALE_CHECKOUT_RETENTION_DAYS * 86400)
    removed: list[str] = []
    try:
        children = list(sources_dir.iterdir())
    except OSError:
        return []
    for child in children:
        if not _is_stale_candidate(child):
            continue
        # Containment check: resolve symlinks and verify the target is still
        # inside sources_dir. This prevents an attacker-placed symlink from
        # causing rmtree to delete files outside app-sources.
        try:
            resolved = child.resolve(strict=True)
        except OSError:
            # Cannot resolve — skip rather than delete blindly.
            continue
        try:
            resolved.relative_to(sources_dir.resolve())
        except ValueError:
            # Points outside app-sources — do not follow.
            continue
        # Age check via mtime.
        try:
            mtime = child.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        # Safe to remove — best-effort.
        try:
            shutil.rmtree(child, ignore_errors=True)
            if not child.exists():
                removed.append(child.name)
        except Exception:  # noqa: BLE001 — best-effort
            pass
    return removed


async def _sweep_stale_checkouts() -> None:
    """Best-effort async sweep of aged stale/partial dirs under app-sources.

    Called at the start of each install_from_registry invocation so old
    checkouts are eventually cleaned up without blocking or failing the
    install.
    """
    sources_dir = _app_sources_dir()
    now_ts = time.time()
    try:
        removed = await asyncio.to_thread(_sweep_stale_checkouts_sync, sources_dir, now_ts)
        if removed:
            logger.info(
                "Swept %d aged stale checkout(s): %s",
                len(removed),
                ", ".join(removed),
            )
    except Exception:  # noqa: BLE001 — never fail the install
        logger.debug("Stale checkout sweep failed (best-effort)", exc_info=True)


async def _clone_origin_url(dest: Path) -> str | None:
    """Read *dest*'s ``origin`` remote URL. Returns None when unreadable.

    Local metadata read: no network, and ``anonymous_git_env`` so a credential
    helper is never invoked just to inspect a checkout. Routed through the
    sandbox chokepoint + cgroup scope like every other git spawn in this
    module — the argv is fixed, but *dest* is derived from an index-supplied
    app name, so the cwd is not ours to trust.
    """
    if not (dest / ".git").is_dir():
        return None
    origin_cmd, _cleanup = wrap_argv(
        ["git", "remote", "get-url", "origin"],
        mode="strict",  # credential-free read; ~/.ssh stays hidden
    )
    origin_cmd = cgroup_scope_argv(origin_cmd)
    try:
        proc = await create_subprocess_limited(
            *origin_cmd,
            cwd=str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=anonymous_git_env(),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError:
        return None
    try:
        origin_out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        await _kill_process_group(proc)
        return None
    if proc.returncode != 0:
        return None
    return origin_out.decode(errors="replace").strip()


async def _clone_origin_matches(dest: Path, git_url: str) -> bool:
    """Whether *dest* is a checkout of *git_url* (byte-identical origin).

    Fails closed: an unreadable origin, a missing remote, or an empty
    *git_url* to compare against all return False.
    """
    if not git_url:
        return False
    return await _clone_origin_url(dest) == git_url


def _read_clone_branch(clone_dir: Path) -> str | None:
    """Read the current branch of an existing git clone.

    Returns the branch name (e.g. ``"main"``), or None if the clone does not
    exist, is in detached HEAD state, or the branch cannot be determined.
    Reads ``.git/HEAD`` directly (stdlib-only, no subprocess spawn) — mirrors
    the fail-closed posture of :func:`_clone_origin_matches`.

    A ``.git`` that is a *file* (worktree / submodule gitfile) rather than a
    directory also fails closed (``is_file()`` on the nested path returns
    False), so no fast path is attempted for those layouts.
    """
    head_file = clone_dir / ".git" / "HEAD"
    if not head_file.is_file():
        return None
    try:
        head_content = head_file.read_text("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    # A normal branch checkout has HEAD = "ref: refs/heads/<branch>"
    _REF_PREFIX = "ref: refs/heads/"
    if head_content.startswith(_REF_PREFIX):
        return head_content[len(_REF_PREFIX) :]
    # Detached HEAD (raw SHA) or unexpected format — fail closed.
    return None


async def _clone_branch_matches(dest: Path, branch: str) -> bool:
    """Whether *dest* has *branch* checked out (exact string equality).

    Fails closed: an unreadable or detached HEAD, a missing ``.git/HEAD``,
    or an empty *branch* to compare against all return False — the caller
    must fall through to the throwaway clone so admission sees the correct
    branch's manifest.
    """
    if not branch:
        return False
    clone_branch = await asyncio.to_thread(_read_clone_branch, dest)
    return clone_branch == branch


# ---------------------------------------------------------------------------
# Git clone + build support for App Store installs
# ---------------------------------------------------------------------------

_BUILD_TIMEOUT = 600  # 10 minutes — frontend bundlers / packagers can be slow
_KILL_GRACE_PERIOD = 5  # seconds to wait after SIGTERM before SIGKILL


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Send SIGTERM to the process group, escalate to SIGKILL if needed.

    Routed through platform_compat (killpg on POSIX, taskkill /T on Windows) so
    the app-build timeout path doesn't AttributeError on win32.
    """
    # Async variants offload Windows taskkill to subprocess_executor so this
    # The build timeout path never blocks the event loop on taskkill.exe.
    # POSIX branch stays inline (os.killpg is non-blocking).
    try:
        await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGTERM)
    except OSError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_PERIOD)
    except asyncio.TimeoutError:
        try:
            await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
        except OSError:
            proc.kill()
        await proc.wait()


async def _git_clone_or_pull(
    git_url: str,
    branch: str,
    dest: Path,
    log_lines: list[str],
    *,
    index_originated: bool = False,
    pending_cleanup: list[Path] | None = None,
) -> dict[str, Any] | None:
    """Clone *git_url* into *dest*, or fast-forward it if already present.

    Returns None on success, or a ``{"ok": False, ...}`` error dict on failure.

    If *pending_cleanup* is provided (a mutable list), any moved-aside directory
    that should be deleted after the caller's full install transaction succeeds
    is appended to it. The caller is responsible for cleaning up these paths
    on the happy path; on failure, the old checkout has already been restored
    by this function's finally block.

    *index_originated* selects the credential posture (confused-deputy defense —
    see :func:`anonymous_git_env`). When ``False`` (the default: a bundled /
    owner-designated install) the clone keeps the gateway's ambient git/ssh
    identity via :func:`minimal_env`. When ``True`` (the repo URL came from an
    owner-configured *external* registry index — index-controlled content, not a
    repo the owner typed) the clone runs **credential-free** via
    :func:`anonymous_git_env` and forces the ``strict`` OS sandbox (``~/.ssh``
    hidden), so a hostile index entry pointing at a private *sibling* repo on the
    owner's own trusted forge cannot be read with the gateway's identity.
    """
    clone_env = anonymous_git_env() if index_originated else minimal_env()
    sandbox_mode = "strict" if index_originated else _context_clone_sandbox_mode(git_url)
    # SSRF gate: refuse to clone/pull from a host the owner does not explicitly
    # trust (public forge or configured registry). The git_url may originate
    # from an untrusted external registry index; this prevents a clone against
    # a loopback/internal destination it could inject. is_clone_host_trusted()
    # loads config from disk, so run it off the event loop.
    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        log_lines.append(f"Refusing clone: host of {git_url!r} is not a trusted forge/registry")
        return {
            "ok": False,
            "error": "untrusted_clone_host",
            "message": "Refusing to clone from an untrusted host (not a public forge or configured registry).",
        }
    # Track a moved-aside directory if we need to preserve the old checkout
    # during origin-mismatch re-clone (delete-after-success pattern).
    moved_aside: Path | None = None

    if dest.is_dir() and (dest / ".git").is_dir():
        # The credential posture was decided from *git_url* — but a persisted
        # clone pulls from ITS OWN `origin`, which can be a different URL
        # (e.g. a registry replaced with the same app name leaves the old
        # clone behind). Never run a credentialed pull against an unverified
        # remote: require the existing origin to be byte-identical to the
        # vetted git_url, otherwise move the stale clone aside and re-clone
        # from the URL the posture decision was actually made for.
        #
        # The same origin check gates the manifest that admission ran on (see
        # _fetch_app_manifest), so the re-clone below cannot swap in code that
        # was admitted under a different repo's manifest.
        #
        # The mismatched clone is NEVER built from or pulled from — fail-closed.
        existing_origin = await _clone_origin_url(dest)
        if existing_origin is None:
            # Unreadable origin (corrupt .git/config, missing remote, etc.).
            # Fail-closed WITHOUT destroying the checkout — the user may
            # have local edits and the checkout might be the correct repo
            # with a broken config. Never enter the destructive
            # move-aside/re-clone path on an ambiguous signal.
            log_lines.append(
                f"Cannot read origin remote of existing checkout at {dest}; "
                "refusing to replace it (fix the checkout manually and retry)"
            )
            return {
                "ok": False,
                "name": dest.name,
                "error": "unreadable_clone_origin",
                "message": (
                    "The existing checkout's origin remote is unreadable. "
                    "Remove or fix it manually and retry the install."
                ),
            }
        if existing_origin != git_url:
            log_lines.append(
                f"Existing clone origin {existing_origin!r} does not match "
                f"{git_url!r}; moving aside stale clone for re-clone"
            )
            # Move aside with an atomic same-filesystem rename into a sibling
            # temp path under the app-sources root. If rename fails (e.g. locked
            # files on Windows), return fail-closed without deleting dest.
            stale_name = f"{dest.name}.stale-{uuid.uuid4().hex[:8]}"
            moved_aside = dest.with_name(stale_name)
            try:
                await asyncio.to_thread(dest.rename, moved_aside)
            except OSError as exc:
                log_lines.append(
                    f"Could not move aside the stale clone at {dest}: {exc}; "
                    "refusing to build from it"
                )
                return {
                    "ok": False,
                    "name": dest.name,
                    "error": "stale_clone_not_removed",
                    "message": (
                        "A checkout of a different repository is present and could not be "
                        f"moved aside: {exc}. Remove it manually and retry the install."
                    ),
                }
            # Refresh mtime so the retention clock starts now, not at the
            # checkout's last-modified time (which may already exceed the
            # sweep threshold).  Best-effort — failure here is harmless
            # (the directory just survives slightly shorter than intended).
            try:
                await asyncio.to_thread(os.utime, moved_aside)
            except OSError:
                pass

    if dest.is_dir() and (dest / ".git").is_dir():
        # Already cloned from the verified origin — fetch and fast-forward.
        # (The origin-mismatch gate above guarantees this checkout's origin is
        # byte-identical to git_url: a mismatched checkout was moved aside and
        # never reused, so the fetch source and the provenance record are the
        # same URL by construction.)
        log_lines.append(f"Updating {git_url} (branch: {branch})...")
        # Route through wrap_argv (OS sandbox) THEN cgroup_scope_argv, matching
        # the fresh-clone path below — the cgroup DoS ceiling is the outermost
        # layer but must not replace the wrap_argv sandbox on this
        # agent-influenced git spawn.
        pull_cmd, _cleanup = wrap_argv(
            ["git", "pull", "--ff-only", "origin", branch],
            mode=sandbox_mode,
        )
        pull_cmd = cgroup_scope_argv(pull_cmd)
        proc = await create_subprocess_limited(
            *pull_cmd,
            cwd=str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=clone_env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            log_lines.append(stdout.decode(errors="replace").strip())
            if proc.returncode != 0:
                # Fail closed: installing whatever the checkout happens to hold
                # while persisting the catalog URL as its provenance would
                # record a source the installed code was never fetched from.
                log_lines.append(f"git pull failed (exit {proc.returncode}) — aborting")
                return {
                    "ok": False,
                    "error": f"git pull failed (exit {proc.returncode}); not installing stale code",
                }
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            log_lines.append("git pull timed out — aborting")
            return {
                "ok": False,
                "error": "git pull timed out; not installing stale code",
            }
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
    sandboxed_cmd, _cleanup = wrap_argv(clone_cmd, mode=sandbox_mode)
    sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling

    # If we moved aside a stale clone for re-clone, wrap the entire spawn+wait
    # in try/finally so that ANY failure path (spawn exception, cancellation,
    # timeout, nonzero exit) restores the moved-aside checkout. The old checkout
    # must never disappear permanently due to a transient clone failure.
    clone_succeeded = False
    try:
        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=clone_env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
            log_lines.append(stdout.decode(errors="replace").strip())
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            await asyncio.to_thread(shutil.rmtree, dest, True)
            return {"ok": False, "name": dest.name, "error": "git clone timed out"}
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            await asyncio.to_thread(shutil.rmtree, dest, True)
            raise
        if proc.returncode != 0:
            await asyncio.to_thread(shutil.rmtree, dest, True)
            return {"ok": False, "name": dest.name, "error": "git clone failed"}
        clone_succeeded = True
        return None
    finally:
        if moved_aside is not None:
            if clone_succeeded:
                # Clone verified — but do NOT delete moved_aside yet.
                # The caller's build/install step has not run; if it fails
                # the user loses their old (possibly locally modified) code.
                # Instead, surface the path for the caller to clean up
                # after the full install transaction succeeds.
                if pending_cleanup is not None:
                    pending_cleanup.append(moved_aside)
            else:
                # Clone did NOT succeed — remove any partial dest and restore
                # the old checkout so the user's code is not stranded.
                await asyncio.to_thread(shutil.rmtree, dest, True)
                # If dest still exists (rmtree silently failed, e.g. locked
                # files on Windows), move IT aside so the restore rename
                # cannot collide. Keep the path inside app-sources.
                if dest.exists():
                    partial_name = f"{dest.name}.partial-{uuid.uuid4().hex[:8]}"
                    partial_aside = dest.with_name(partial_name)
                    try:
                        await asyncio.to_thread(dest.rename, partial_aside)
                        log_lines.append(
                            f"Undeletable partial clone moved to {partial_aside}; "
                            "remove it manually when the lock is released"
                        )
                    except OSError as move_exc:
                        log_lines.append(
                            f"Cannot remove or move partial clone at {dest}: "
                            f"{move_exc}; old checkout remains at {moved_aside}"
                        )
                        # Cannot restore — bail out of the restore attempt.
                        moved_aside = None  # skip the rename below
                    else:
                        # Refresh mtime so the retention clock starts now
                        # (best-effort — harmless if it fails).
                        try:
                            await asyncio.to_thread(os.utime, partial_aside)
                        except OSError:
                            pass
                if moved_aside is not None:
                    try:
                        await asyncio.to_thread(moved_aside.rename, dest)
                    except OSError as exc:
                        log_lines.append(
                            f"Cannot restore moved-aside checkout at "
                            f"{moved_aside}: {exc}; recover your files from "
                            f"{moved_aside}"
                        )


async def _clone_build_app(
    git_url: str,
    app_name: str,
    log_lines: list[str],
    branch: str = "main",
    *,
    index_originated: bool = False,
    subdirectory: str = "",
    entry_repo: str = "",
) -> dict[str, Any]:
    """Clone an app repo, gate its identity, then run its build.

    Source is cloned to ``~/.kiro/crew/app-sources/{app_name}/`` (persistent;
    survives reboots and is reused for updates).  **The identity gate runs
    BETWEEN clone and build**: the cloned ``app.json`` (under *subdirectory*
    when set) must declare *app_name* before :func:`_run_app_build` executes —
    build ecosystems run repo-authored lifecycle scripts (an npm ``preinstall``,
    a ``setup.py``), so validating only after the build would let a mismatched
    repo execute code despite the refusal.

    *index_originated* is forwarded to :func:`_git_clone_or_pull` to pick the
    credential posture (credential-free + strict sandbox for repos whose URL
    came from an external registry index — see that function's docstring).

    Returns ``{"ok": True, "pkg_dir": <Path>}`` on success or
    ``{"ok": False, "error": ...}`` on failure/refusal.
    """
    # Lock-free: the caller (route handler) holds app_lifecycle_lock(name)
    # across the complete lifecycle transaction — clone/build, copy,
    # registration, and backend startup — so nested acquisition here would
    # deadlock (asyncio.Lock is not reentrant).
    return await _clone_build_app_locked(
        git_url,
        app_name,
        log_lines,
        branch=branch,
        index_originated=index_originated,
        subdirectory=subdirectory,
        entry_repo=entry_repo,
    )


async def _unpoison_rejected_checkout(
    app_name: str,
    pkg_dir: Path,
    log_lines: list[str],
    *,
    checkout_preexisted: bool,
    pre_pull_commit: str,
    manifest_relpath: str = "app.json",
    manifest_snapshot: bytes | None = None,
    restore_from: Path | None = None,
) -> None:
    """Un-poison a checkout after an identity/admission rejection.

    The prefetch prefers the local checkout, so a checkout left sitting at a
    rejected state makes every retry re-reject at prefetch before it could
    ever pull a fixed remote — a permanently stuck app.

    A checkout created THIS RUN is deleted (no residue) and, when the run
    replaced a moved-aside previous checkout (*restore_from*), that previous
    checkout is renamed back into the slot — otherwise the rejection would
    leave the slot empty and strand the user's old workspace as a
    sweeper-doomed ``.stale-*`` sibling.

    A pre-existing workspace is rolled back to its pre-pull commit with
    ``git reset --keep`` (preserves uncommitted local edits; aborts on
    conflict), then the manifest is restored to its exact pre-update
    working-tree bytes (*manifest_snapshot*) — undoing whatever the pull,
    build, or ``onInstall`` script did to ``app.json`` WITHOUT discarding the
    user's own uncommitted manifest edits. Only when no snapshot exists does
    it fall back to ``git --literal-pathspecs checkout --`` from HEAD
    (literal pathspecs keep an index-controlled subdirectory from being
    parsed as pathspec magic). Best-effort throughout: a cleanup failure is
    logged, never raised — the refusal it follows must stand regardless.
    """
    if not checkout_preexisted:
        await asyncio.to_thread(shutil.rmtree, pkg_dir, ignore_errors=True)
        if restore_from is not None:
            try:
                await asyncio.to_thread(restore_from.rename, pkg_dir)
                log_lines.append(
                    "Restored the previous checkout after rejecting the replacement clone"
                )
            except OSError as exc:
                log_lines.append(
                    f"WARNING: could not restore the previous checkout from "
                    f"{restore_from.name}: {exc}; it is retained there for manual recovery"
                )
        return

    async def _run_git(argv: list[str]) -> int:
        cmd, _cleanup = wrap_argv(argv, mode="standard")
        cmd = cgroup_scope_argv(cmd)
        proc = await create_subprocess_limited(
            *cmd,
            cwd=str(pkg_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=minimal_env(),
        )
        # Tree-killing timeout: a bare wait_for would abandon a slow git
        # process still running, letting it race (and overwrite) the manifest
        # restore that follows.
        await _communicate_with_timeout(proc, timeout=15)
        return proc.returncode or 0

    try:
        if pre_pull_commit:
            rc = await _run_git(["git", "reset", "--keep", pre_pull_commit])
            if rc == 0:
                log_lines.append(
                    f"Rolled checkout back to pre-update commit {pre_pull_commit[:12]}"
                )
            else:
                log_lines.append(
                    "WARNING: could not roll the checkout back; "
                    "a retry may keep rejecting until the source is repaired"
                )
    except (asyncio.TimeoutError, OSError, RuntimeError) as exc:
        # RuntimeError covers SandboxUnavailableError from wrap_argv — cleanup
        # is best-effort and must never mask the refusal it follows.
        logger.debug("post-rejection rollback failed for %s: %s", app_name, exc)
    try:
        # Restore the manifest regardless — in its OWN guarded block so a
        # reset failure above cannot skip it: a build step or install script
        # rewriting app.json is a WORKING-TREE edit the reset cannot undo
        # (HEAD never moved), and app.json is the poison vector the next
        # prefetch reads.
        if manifest_snapshot is not None:
            await asyncio.to_thread((pkg_dir / manifest_relpath).write_bytes, manifest_snapshot)
            log_lines.append(f"Restored {manifest_relpath} to its exact pre-update contents")
        else:
            rc = await _run_git(["git", "--literal-pathspecs", "checkout", "--", manifest_relpath])
            if rc != 0:
                log_lines.append(
                    f"WARNING: could not restore {manifest_relpath}; "
                    "a retry may keep rejecting until the source is repaired"
                )
    except (asyncio.TimeoutError, OSError, RuntimeError) as exc:
        logger.debug("post-rejection manifest restore failed for %s: %s", app_name, exc)


async def _clone_build_app_locked(
    git_url: str,
    app_name: str,
    log_lines: list[str],
    branch: str = "main",
    *,
    index_originated: bool = False,
    subdirectory: str = "",
    entry_repo: str = "",
) -> dict[str, Any]:
    """Inner implementation of _clone_build_app, called under per-app lock."""
    if not _looks_like_git_url(git_url):
        return {
            "ok": False,
            "name": app_name,
            "error": f"{git_url!r} is not a cloneable git URL",
        }

    pkg_dir = app_source_dir(app_name)
    pending_cleanup: list[Path] = []
    # Captured BEFORE the clone so a refusal below can tell a checkout this run
    # created (delete: no residue) from a pre-existing app workspace (preserve).
    checkout_preexisted = (pkg_dir / ".git").is_dir()
    # And the pre-pull commit, so an admission rejection can ROLL BACK a
    # pre-existing checkout: the prefetch prefers the local checkout, so a
    # checkout left sitting at a policy-rejected commit would make every retry
    # reject at prefetch before the pull could ever fetch a fixed remote.
    pre_pull_commit = (
        await asyncio.to_thread(_resolved_clone_commit, pkg_dir) if checkout_preexisted else ""
    )
    # And the manifest's exact pre-update WORKING-TREE bytes (which may carry
    # the user's uncommitted local edits): a rejection restores THIS snapshot,
    # so cleanup undoes whatever the pull/build/script did to app.json without
    # discarding the user's own edits the way a checkout-from-HEAD would.
    manifest_rel = f"{subdirectory}/app.json" if subdirectory else "app.json"
    pre_update_manifest: bytes | None = None
    if checkout_preexisted:
        try:
            pre_update_manifest = await asyncio.to_thread((pkg_dir / manifest_rel).read_bytes)
        except OSError:
            pre_update_manifest = None
    clone_err = await _git_clone_or_pull(
        git_url,
        branch,
        pkg_dir,
        log_lines,
        index_originated=index_originated,
        pending_cleanup=pending_cleanup,
    )
    if clone_err is not None:
        return clone_err
    if pending_cleanup:
        # The origin-mismatch gate moved the old checkout aside and FRESH-CLONED
        # into pkg_dir: whatever pre-existed is now a .stale-* sibling, and the
        # directory at pkg_dir was created THIS RUN. The pre-clone snapshot
        # above describes the moved-aside (different-origin) history — using it
        # would make a later rejection try to reset the new clone to a commit
        # from another repository, or preserve a squatting clone as if it were
        # the user's workspace. Cleanup state must describe the ACTIVE checkout;
        # the moved-aside path is kept so a rejection can put the previous
        # checkout BACK instead of leaving the slot empty and the old workspace
        # stranded as a sweeper-doomed .stale-* sibling.
        checkout_preexisted = False
        pre_pull_commit = ""
        pre_update_manifest = None

    # IDENTITY GATE — before the build, so a repo whose app.json declares a
    # different name never gets to run npm/pip lifecycle scripts. Fail-closed:
    # a missing or unparseable app.json (or name) is a mismatch, not a pass.
    app_source = pkg_dir
    if subdirectory:
        contained = _contained_join(pkg_dir, subdirectory)
        if contained is None:
            return {
                "ok": False,
                "name": app_name,
                "error": f"unsafe subdirectory {subdirectory!r} escapes the app source root",
            }
        app_source = contained
    cloned_manifest: dict[str, Any] | None = None
    try:
        parsed = json.loads(await asyncio.to_thread((app_source / "app.json").read_text, "utf-8"))
        if isinstance(parsed, dict):
            cloned_manifest = parsed
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.debug("cloned app.json for %s is unreadable pre-build: %s", app_name, exc)
    cloned_name = str((cloned_manifest or {}).get("name", "") or "")
    if cloned_manifest is None or cloned_name != app_name:
        return await _refuse_identity_mismatch(
            app_name,
            cloned_name,
            entry_repo or git_url,
            pkg_dir,
            log_lines,
            created_this_run=not checkout_preexisted,
            pre_pull_commit=pre_pull_commit,
            manifest_relpath=manifest_rel,
            manifest_snapshot=pre_update_manifest,
            restore_from=(pending_cleanup[0] if pending_cleanup else None),
        )

    # ADMISSION GATE, second pass — on the CLONED manifest. The first pass ran
    # on the pre-clone prefetch, but the repository can advance between the two
    # reads: a signed preview can resolve to an unsigned (or newly banned)
    # manifest at clone time, and under a require-signature policy that content
    # must not build or install. Same fail-closed policy call, different
    # artifact.
    denied = app_admission_denied(
        app_name,
        manifest=AppManifest.from_dict(cloned_manifest),
        action="install_from_registry",
    )
    if denied:
        log_lines.append(f"Refusing install: blocked by admission policy: {denied}")
        try:
            sel().log_api_access(
                caller="app_install_from_registry",
                operation="admission_cloned",
                outcome="rejected",
                resources=f"name={app_name!r}",
                error=denied,
            )
        except Exception as exc:  # an audit failure must never mask the refusal
            logger.debug("SEL audit failed for %s cloned admission: %s", app_name, exc)
        # Un-poison the checkout so the rejection is retryable (see helper).
        await _unpoison_rejected_checkout(
            app_name,
            pkg_dir,
            log_lines,
            checkout_preexisted=checkout_preexisted,
            pre_pull_commit=pre_pull_commit,
            manifest_relpath=manifest_rel,
            manifest_snapshot=pre_update_manifest,
            restore_from=(pending_cleanup[0] if pending_cleanup else None),
        )
        return {
            "ok": False,
            "name": app_name,
            "error": f"blocked by admission policy: {denied}",
        }

    result = await _run_app_build(pkg_dir, app_name, log_lines)
    if result["ok"]:
        result["pkg_dir"] = pkg_dir
        # Surface the pre-clone checkout state so the caller's LATER gates
        # (post-build / post-script admission) can un-poison the checkout with
        # the same delete-fresh / roll-back-pre-existing semantics this
        # function applies at the cloned-admission gate above.
        result["_checkout_preexisted"] = checkout_preexisted
        result["_pre_pull_commit"] = pre_pull_commit
        result["_pre_update_manifest"] = pre_update_manifest
        # Do NOT delete moved-aside checkouts — even after a successful
        # install transaction the user may want to recover local edits from
        # the old checkout.  Surface the paths so the caller can log them.
        # The dirs are harmless siblings swept by _sweep_stale_checkouts()
        # after _STALE_CHECKOUT_RETENTION_DAYS (best-effort, runs at the
        # start of the next install_from_registry call).
        if pending_cleanup:
            result["_pending_stale_cleanup"] = list(pending_cleanup)
    else:
        # Build failed — restore the old checkout so the user's local edits
        # survive. Remove the (successfully cloned but unbuildable) new dest
        # and rename the moved-aside dir back.
        for stale_path in pending_cleanup:
            if stale_path.exists():
                await asyncio.to_thread(shutil.rmtree, pkg_dir, True)
                try:
                    await asyncio.to_thread(stale_path.rename, pkg_dir)
                    log_lines.append(
                        "Build failed; previous checkout restored from " f"{stale_path.name}"
                    )
                except OSError as exc:
                    log_lines.append(
                        f"Build failed; could not restore previous checkout "
                        f"from {stale_path}: {exc}. Recover your files from "
                        f"{stale_path}"
                    )
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
        # Resolve to a full path, mirroring the pip branch below: on Windows npm
        # is ``npm.CMD``, which shutil.which finds but CreateProcess cannot spawn
        # by the bare name "npm".
        npm = shutil.which("npm")
        if npm:
            build_cmds.append([npm, "install"])
            try:
                pkg = json.loads((build_dir / "package.json").read_text("utf-8"))
                if (pkg.get("scripts") or {}).get("build"):
                    build_cmds.append([npm, "run", "build"])
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
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
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
        proc = await create_subprocess_limited(
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

    Source code is cloned to ``~/.kiro/crew/app-sources/{name}/`` (persistent,
    survives reboots, used by app update scripts).

    For self-managed apps (``managed: "self"`` in registry), only the clone +
    install script is run — KiroCrew does NOT copy files to ``~/.kiro/crew/apps/``
    or register resources via bridges.  The app registers itself at runtime.

    For kirocrew-managed apps, files are copied to ``~/.kiro/crew/apps/{name}/``
    and resources are registered via bridges.py as usual.

    Args:
        name: Registry app name.
        log_lines: Optional list to collect log output.  Pass a
            :class:`StreamingLogLines` instance to stream logs in real-time
            via the SSE install endpoint.  If *None*, a plain ``list`` is used
            (original behaviour).

    Steps:
    1. Validate the app exists in the trusted registry JSON
    2. Clone the repo to ~/.kiro/crew/app-sources/{name}/ (timeout: 60s)
    3. Build it (npm/pip, auto-detected) then run the install script from
       app.json if any (timeout: 300s)
    4. For kirocrew-managed: call install_app() or update_app()
    5. Store ``registry:<name>`` plus structured provenance (source URL,
       originating registry, resolved commit, verified signer) for future updates

    Returns a dict with ok, name, message/error, and log output.
    """
    # An already-installed app that carries provenance may only be re-installed
    # (updated) from the source it came from; fresh installs and legacy records
    # keep the historical bare-name lookup. Blocking reads → off the loop.
    entry, pin_error = await asyncio.to_thread(_resolve_install_entry, name)
    if pin_error:
        try:
            sel().log_api_access(
                caller="app_install_from_registry",
                operation="provenance_mismatch",
                outcome="rejected",
                resources=f"name={name!r}",
                error=pin_error,
            )
        except Exception as exc:  # an audit failure must never mask the refusal
            logger.debug("SEL audit failed for %s provenance mismatch: %s", name, exc)
        return {"ok": False, "name": name, "error": pin_error}
    if not entry:
        return {"ok": False, "error": f"app {name!r} not found in registry"}

    git_url = _entry_git_url(entry)
    if not git_url:
        return {"ok": False, "error": f"app {name!r} has no git URL configured"}

    repo = entry.get("repo", "")
    branch = entry.get("branch", "main")
    subdirectory = entry.get("subdirectory", "")

    # Confused-deputy defense on the INSTALL path (companion to the automatic
    # browse/refresh defense in ``anonymous_git_env``). An entry that came from
    # an owner-configured *external* registry index carries ``_registry`` (set
    # when the index is fetched/cached); its ``repo`` URL is index-controlled
    # content, not a repo the owner typed — the owner clicked Install on an
    # index-authored name/description. Because ``is_clone_host_trusted`` is
    # host-granular, such an entry can point at a private *sibling* repo on the
    # owner's own trusted forge; cloning it with the gateway's ambient git/ssh
    # identity would read that private repo as a confused deputy. So an
    # index-originated install clones credential-free + strict-sandboxed too.
    # Bundled (curated, KiroCrew-shipped) entries have no ``_registry`` marker
    # and remain owner-designated → full credentials.
    #
    # Same-repo credential carve-out: when the entry's effective clone URL is
    # byte-identical to the owner-configured registry repo URL, the
    # confused-deputy argument does not apply — the owner explicitly designated
    # exactly that URL by adding the registry. The carve-out flips BOTH env
    # AND sandbox mode together (the strict sandbox hiding ~/.ssh is the
    # load-bearing enforcement on credential-helper setups, not the env alone).
    # Sibling repos on the same host remain anonymous+strict.
    index_originated = bool(entry.get("_registry"))
    # OFFICIALNESS is decided here, BEFORE the owner-designated carve-out below:
    # that carve-out flips index_originated as a CREDENTIAL decision (owner
    # explicitly designated the repo), but an external-index entry never becomes
    # an official-catalog entry — install receipts must not fire for it.
    official_entry = not index_originated
    # The originating external registry id, recorded as provenance. Empty means
    # the bundled (KiroCrew-shipped) catalog, which is itself a distinct source.
    # Captured BEFORE the owner-designated carve-out (same reasoning as above):
    # the entry still came from that external registry, and provenance must say so.
    source_registry = str(entry.get("_registry", "") or "")
    if index_originated and await asyncio.to_thread(_is_owner_designated_repo, entry):
        index_originated = False
        _sel_credential_grant("install_from_registry", _entry_git_url(entry) or "")
    # Capture event kind before clone/build/install scripts can register or
    # otherwise change app state. The receipt describes this call's starting
    # state, not an intermediate side effect.
    was_installed = get_app(name) is not None

    # Fetch the app's manifest for platform info and install script. This is a
    # read-only metadata fetch (git archive of app.json), safe to do before the
    # admission gate so a correctly-signed manifest can be passed to it.
    # Same-repo carve-out: if the entry is from an external index but its clone
    # URL matches the owner-configured registry repo (index_originated was
    # flipped to False above), use owner credentials for the manifest fetch too.
    manifest_owner_designated = bool(entry.get("_registry")) and not index_originated
    manifest = await _fetch_app_manifest(
        repo,
        branch,
        subdirectory,
        app_name=name,
        git_url=git_url,
        owner_designated=manifest_owner_designated,
    )

    # Admission: gate AFTER the manifest fetch (so a signed manifest is verified)
    # but BEFORE the repo is cloned and setup.onInstall runs, so a banned /
    # non-allowlisted / unsigned app is never cloned nor its install script run.
    admission_manifest = AppManifest.from_dict(manifest) if manifest else None
    denied = app_admission_denied(name, manifest=admission_manifest, action="install_from_registry")
    if denied:
        sel().log_api_access(
            caller="app_install_from_registry",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return {"ok": False, "name": name, "error": f"blocked by admission policy: {denied}"}

    # NOTE: the provenance signer is computed LATER, from the identity-checked
    # CLONED manifest — not from this pre-clone prefetch. An update can pull a
    # commit whose manifest is no longer signed (or signed by someone else);
    # provenance must record the artifact actually installed, not the preview.

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

    # detectInstalled, clone/build, dependency setup, and onInstall are all
    # executable third-party surfaces and share the same explicit admission.
    execution_denied = app_execution_denied(name, action="registry_install", caller="registry")
    if execution_denied:
        return {
            "ok": False,
            "name": name,
            "error": f"blocked by execution policy: {execution_denied}",
            # Same wire contract as the openCommand denial in routes.py: the
            # frontend keys its affordance off `code`, never off this prose.
            # Without it the App Store cannot tell "needs a trust grant" from
            # any other install failure and the consent modal never opens.
            "code": "app_execution_denied",
            "log": "\n".join(log_lines),
        }

    # Guard: check if already installed externally (e.g. user ran setup.sh manually)
    detect_cmd = entry.get("detectInstalled", "")
    if detect_cmd:
        try:

            base_cmd = ["/bin/sh", "-c", detect_cmd]
            sandboxed_cmd, _cleanup = wrap_argv(base_cmd, mode="strict")
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            await _communicate_with_timeout(proc, timeout=5)
            if proc.returncode == 0:
                return {
                    "ok": False,
                    "name": name,
                    "error": f"{name} is already installed on this machine. "
                    f"Launch it to register with Kiro Crew automatically.",
                }
        except (asyncio.TimeoutError, OSError):
            pass

    try:
        # Best-effort sweep of aged .stale-* / .partial-* dirs before the
        # install — prevents unbounded accumulation without blocking.
        await _sweep_stale_checkouts()

        # Step 1: Clone the app repo and build it (npm/pip auto-detected).
        # `git clone` handles fetch + branch checkout; a subsequent install
        # run fast-forwards the existing clone instead of re-cloning. The
        # cleanup state for later gates (_checkout_preexisted /
        # _pre_pull_commit) rides on build_result — it describes the ACTIVE
        # checkout, accounting for a move-aside re-clone.
        build_result = await _clone_build_app(
            git_url,
            name,
            log_lines,
            branch=branch,
            index_originated=index_originated,
            subdirectory=subdirectory,
            entry_repo=repo,
        )
        if not build_result["ok"]:
            return {**build_result, "log": "\n".join(log_lines)}

        app_source = build_result["pkg_dir"]
        clone_root = app_source
        if subdirectory:
            # ``subdirectory`` is untrusted index-controlled content. Join it
            # under the cloned source root with symlink-resolving containment so
            # an absolute/``..``/symlink value cannot point app.json (and thus
            # setup.onInstall) at an attacker-selected path outside the clone.
            contained = _contained_join(app_source, subdirectory)
            if contained is None:
                return {
                    "ok": False,
                    "name": name,
                    "error": f"unsafe subdirectory {subdirectory!r} escapes the app source root",
                    "log": "\n".join(log_lines),
                }
            app_source = contained

        # NOTE: a missing app.json is handled by the identity gate below
        # (fail-closed: unreadable manifest == mismatch), so a build step that
        # DELETES the manifest still goes through the refusal path and its
        # checkout cleanup rather than returning early with a poisoned tree.

        # Read the cloned repo's app.json once: it decides both the app's
        # IDENTITY and its install script.
        # Trust model: curated registry entry → cloned repo → app.json
        # (maintained by the app author).  The install script has the same
        # trust level as any code you clone and build locally.
        manifest_data: dict[str, Any] | None = None
        try:
            manifest_raw = await asyncio.to_thread(
                (app_source / "app.json").read_text,
                "utf-8",
            )
            parsed = json.loads(manifest_raw)
            if isinstance(parsed, dict):
                manifest_data = parsed
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.debug("cloned app.json for %s is unreadable: %s", name, exc)

        # IDENTITY GATE, second pass: the primary gate already ran inside
        # _clone_build_app BEFORE the build (so a mismatched repo never executes
        # npm/pip lifecycle scripts). This re-check catches the remaining
        # window — a build step that REWRITES app.json to a different name —
        # and stays fail-closed: a missing or unparseable name is a mismatch,
        # not a pass. ``install_app``/``update_app`` derive the installed
        # identity from this manifest, so it must still match the entry here.
        if manifest_data is None or str(manifest_data.get("name", "") or "") != name:
            return await _refuse_identity_mismatch(
                name,
                str((manifest_data or {}).get("name", "") or ""),
                repo,
                clone_root,
                log_lines,
                created_this_run=not bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=next(iter(build_result.get("_pending_stale_cleanup") or []), None),
            )

        # ADMISSION GATE, third pass — the post-build manifest is what
        # install_app/update_app will actually register, and a build step can
        # rewrite app.json; a manifest that no longer satisfies the admission
        # policy (e.g. signature required and now absent) must not install.
        denied = app_admission_denied(
            name,
            manifest=AppManifest.from_dict(manifest_data),
            action="install_from_registry",
        )
        if denied:
            log_lines.append(f"Refusing install: blocked by admission policy: {denied}")
            try:
                sel().log_api_access(
                    caller="app_install_from_registry",
                    operation="admission_postbuild",
                    outcome="rejected",
                    resources=f"name={name!r}",
                    error=denied,
                )
            except Exception as exc:  # audit failure must never mask the refusal
                logger.debug("SEL audit failed for %s post-build admission: %s", name, exc)
            # Same retry-poisoning hazard as the cloned-admission gate: the
            # checkout sits at the rejected commit and the prefetch prefers it,
            # so clean up with the same delete-fresh/roll-back semantics.
            await _unpoison_rejected_checkout(
                name,
                app_source_dir(name),
                log_lines,
                checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=next(iter(build_result.get("_pending_stale_cleanup") or []), None),
            )
            return {
                "ok": False,
                "name": name,
                "error": f"blocked by admission policy: {denied}",
                "log": "\n".join(log_lines),
            }

        # NOTE: the provenance commit AND signer are both resolved AFTER the
        # install-script block below — onInstall runs with write access to the
        # checkout and can advance it to another commit or swap the manifest;
        # provenance must record the state that actually registers.

        install_script = (manifest_data.get("setup") or {}).get("onInstall", "")

        # Step 2: Run install script
        if install_script:
            log_lines.append(f"Running install script: {install_script}")
            # Sandboxed via wrap_argv(); consider migrating to AcpClient._spawn() for full OS-level isolation.
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

            base_cmd = ["/bin/bash", "-c", safe_script]
            sandboxed_cmd, _cleanup = wrap_argv(base_cmd, mode="standard")
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
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
                # Kill the entire process group (shell + children), reap the
                # child, and escalate SIGTERM -> SIGKILL if it ignores the term.
                await _kill_process_group(proc)
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

            # Reap any SURVIVING descendants of the script's process group
            # before the final gates re-read app.json: a backgrounded child
            # (`nohup evil &`) outlives the shell's clean exit and could
            # rewrite the manifest AFTER the re-read below but before
            # install_app registers it — the exact TOCTOU the final pass
            # exists to close. The shell itself already exited, so anything
            # still in the group is a detached straggler with no legitimate
            # claim to keep running.
            #
            # POSIX: signal the KNOWN group id directly — the script was
            # spawned with start_new_session, so its pgid equals proc.pid by
            # construction, and the group outlives its (already-reaped)
            # leader. Resolving the group via getpgid(proc.pid) would raise
            # ProcessLookupError once the leader is reaped, silently skipping
            # the very stragglers this exists to kill. The pid>1 guard keeps
            # the killpg broadcast-safe (never signal group 0/1/self).
            # Windows: taskkill /T on the root pid via the platform shim.
            try:
                if platform_compat.IS_POSIX:
                    if type(proc.pid) is int and proc.pid > 1:
                        await asyncio.to_thread(os.killpg, proc.pid, platform_compat.SIGKILL)
                else:
                    await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
            except OSError:
                # Empty group (no stragglers) — the common case.
                pass

            # IDENTITY + ADMISSION, final pass — the install script just ran
            # with write access to the checkout and can rewrite app.json, and
            # install_app/update_app/register_external_app re-read that file
            # from disk. Whatever is on disk NOW is what gets registered, so it
            # must pass the same fail-closed gates as the post-build read.
            manifest_data = None
            try:
                parsed = json.loads(
                    await asyncio.to_thread((app_source / "app.json").read_text, "utf-8")
                )
                if isinstance(parsed, dict):
                    manifest_data = parsed
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                logger.debug("post-script app.json for %s is unreadable: %s", name, exc)
            if manifest_data is None or str(manifest_data.get("name", "") or "") != name:
                return await _refuse_identity_mismatch(
                    name,
                    str((manifest_data or {}).get("name", "") or ""),
                    repo,
                    clone_root,
                    log_lines,
                    created_this_run=not bool(build_result.get("_checkout_preexisted")),
                    pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                    manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                    manifest_snapshot=build_result.get("_pre_update_manifest"),
                    restore_from=next(iter(build_result.get("_pending_stale_cleanup") or []), None),
                )
            denied = app_admission_denied(
                name,
                manifest=AppManifest.from_dict(manifest_data),
                action="install_from_registry",
            )
            if denied:
                log_lines.append(f"Refusing install: blocked by admission policy: {denied}")
                try:
                    sel().log_api_access(
                        caller="app_install_from_registry",
                        operation="admission_postscript",
                        outcome="rejected",
                        resources=f"name={name!r}",
                        error=denied,
                    )
                except Exception as exc:  # audit failure must never mask the refusal
                    logger.debug("SEL audit failed for %s post-script admission: %s", name, exc)
                # onInstall ran with write access to the checkout, so this
                # denial leaves it poisoned exactly like the earlier gates —
                # apply the same delete-fresh/roll-back cleanup so a retry
                # can pull a fixed remote instead of re-rejecting at prefetch.
                await _unpoison_rejected_checkout(
                    name,
                    app_source_dir(name),
                    log_lines,
                    checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                    pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                    manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                    manifest_snapshot=build_result.get("_pre_update_manifest"),
                    restore_from=next(iter(build_result.get("_pending_stale_cleanup") or []), None),
                )
                return {
                    "ok": False,
                    "name": name,
                    "error": f"blocked by admission policy: {denied}",
                    "log": "\n".join(log_lines),
                }

        # Provenance is pinned from the FINAL state — after the build, the
        # install script, and the last identity/admission gates: the exact
        # commit the checkout sits at, and whoever signed the manifest that
        # actually registers. Resolving either any earlier would let onInstall
        # advance the checkout or swap the manifest and have provenance record
        # a predecessor. Purely observational: never denies; unsigned yields "".
        source_commit = await asyncio.to_thread(_resolved_clone_commit, clone_root)
        source_signer = await asyncio.to_thread(
            verified_signer, AppManifest.from_dict(manifest_data)
        )

        # Step 3: Resolve dependencies (if declared in manifest)
        deps_data = manifest_data.get("dependencies")
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
            # ``manifest_data`` is the identity-checked read from above — reusing
            # it avoids a second read that could see different bytes.
            from kiro_crew.apps.manager import register_external_app

            display = manifest_data.get("displayName", name)
            version = manifest_data.get("version", "0.0.0")
            reg_result = register_external_app(
                name=name,
                version=version,
                display_name=display,
                source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                manifest_data=manifest_data,
                origin="registry",
            )
            if reg_result.ok:
                set_app_provenance(
                    name,
                    source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                    url=git_url,
                    registry=source_registry,
                    commit=source_commit,
                    signer=source_signer,
                )

            log_lines.append("Pre-registered from cloned manifest (self-managed)")
            log_lines.append("App will update its own registration on next launch")
            # Retain moved-aside checkouts so the user can recover local
            # edits; they will be swept after _STALE_CHECKOUT_RETENTION_DAYS.
            for _stale in build_result.get("_pending_stale_cleanup") or []:
                log_lines.append(f"Previous checkout retained at: {_stale}")
                logger.info("Retained stale checkout: %s", _stale)
            if official_entry:
                install_receipt.dispatch(
                    name,
                    official=True,
                    kind=(
                        install_receipt.KIND_UPDATE if was_installed else install_receipt.KIND_FRESH
                    ),
                )
            return {
                "ok": True,
                "name": name,
                "message": f"installed {name} from {repo} (self-managed)",
                "log": "\n".join(log_lines),
            }

        # Kirocrew-managed: copy to ~/.kiro/crew/apps/ and register resources
        log_lines.append("Installing app...")
        # Lock-free: the route handler holds app_lifecycle_lock(name) across
        # the whole transaction (clone/build → copy → register → backend
        # start); asyncio.Lock is not reentrant, so no acquisition here.
        existing = get_app(name)
        # Off-loop: install_app/update_app do a blocking filesystem copy
        # that can take minutes on large source trees — on the loop it
        # would trip the loop-stall watchdog and kill the gateway.
        if existing:
            result = await asyncio.to_thread(update_app, str(app_source))
        else:
            result = await asyncio.to_thread(install_app, str(app_source))
        log_lines.append(result.message or result.error or "done")

        # Record the source marker plus structured provenance, so a later update
        # resolves the source this install actually came from rather than
        # whichever entry happens to answer to the bare name. This is also what
        # self-heals a legacy record: its next successful update writes the full
        # provenance it was missing.
        if result.ok:
            set_app_provenance(
                result.name,
                source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                url=git_url,
                registry=source_registry,
                commit=source_commit,
                signer=source_signer,
            )
            # Retain moved-aside checkouts so the user can recover local
            # edits; they will be swept after _STALE_CHECKOUT_RETENTION_DAYS.
            for _stale in build_result.get("_pending_stale_cleanup") or []:
                log_lines.append(f"Previous checkout retained at: {_stale}")
                logger.info("Retained stale checkout: %s", _stale)
            if official_entry:
                # Detached best-effort telemetry runs only after durable success.
                install_receipt.dispatch(
                    name,
                    official=True,
                    kind=(
                        install_receipt.KIND_UPDATE if was_installed else install_receipt.KIND_FRESH
                    ),
                )

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
