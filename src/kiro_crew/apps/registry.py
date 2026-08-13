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
import unicodedata
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from kiro_crew.apps import install_receipt, official_catalog
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
from kiro_crew.config.loader import config_dir
from kiro_crew.platform import PlatformCompositionError, current_context

logger = logging.getLogger(__name__)

# Source type prefix for registry-installed apps.
SOURCE_REGISTRY_PREFIX = "registry:"

# A git object name: sha1 (40 hex) or sha256 (64 hex) repository format.
#
# Anchored at ``\Z`` rather than ``$``: Python's ``$`` matches before a trailing
# newline, so ``$`` accepted a 40-hex value with ``"\n"`` appended. That matters at
# both readers -- this pattern validates a pin before it reaches a git argument
# vector, and it validates a SHA read back off disk in
# :func:`_resolved_clone_commit`, where a value that only looks like a commit would
# be reported as the landed one.
_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


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


def _is_safe_env_key(key: str) -> bool:
    """Whether *key* is allowlisted, honoring Windows' case-insensitive env.

    Thin wrapper binding this module's allowlist to the shared matching
    convention — exact on POSIX, case-folded on Windows. The rationale (why a
    literal membership test silently drops ``SystemRoot`` on Windows, and why
    POSIX must stay exact) lives on :func:`platform_compat.env_key_allowed`.
    """
    return platform_compat.env_key_allowed(key, _SAFE_ENV_KEYS)


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


#: Trust tiers an ``ExternalRegistryConfig.trust`` value may name.
#
# ``index`` is the historical (and default) posture: the registry's index is
# untrusted content, so every app it lists clones credential-free. ``owner``
# is the operator's assertion that the index itself is under change control
# they own, which lets its apps clone with the machine's git identity.
#
# Anything not in this set resolves to ``_TRUST_INDEX`` — a typo, or a tier a
# future core adds and this one does not know, must fail toward the restrictive
# posture rather than the credentialed one.
_TRUST_INDEX = "index"
_TRUST_OWNER = "owner"
_REGISTRY_TRUST_TIERS: frozenset[str] = frozenset({_TRUST_INDEX, _TRUST_OWNER})


def _registry_identity_key(name_or_repo: str) -> str:
    """The key two registries collide on: the cache file they would share.

    Not the raw name. A registry's index cache is a FILE, and the file is what is
    actually contended — so the collision rule has to be derived from the path, not
    from the string. Two consequences that the raw name misses:

    * On a case-insensitive filesystem (Windows, default macOS) ``Official`` and
      ``official`` are one file, so they collide there and not on Linux. Folding
      case makes the answer the same everywhere: a configuration that would corrupt
      on one platform is refused on all of them, rather than working until someone
      runs it on a laptop.
    * ``_external_registry_cache_path`` slugifies and hash-disambiguates a name
      carrying unusual characters, so the mapping from name to file is not the
      identity function. Asking the path keeps this rule correct if that
      derivation ever changes.
    """
    return _external_registry_cache_path(name_or_repo).name.casefold()


def _is_supported_registry_transport(repo: str) -> bool:
    """Whether *repo* is a form a registry index may legitimately be fetched from.

    Accepts an https URL, an ssh/scp remote, or a bare legacy name — and nothing
    else, so plaintext ``http://``, ``git://`` and ``ext::`` never reach a clone.
    The index this fetches becomes install coordinates, so an unauthenticated
    transport lets anything on the network path substitute app code.

    **A credential embedded in the URL is refused outright**, not redacted. A
    pinned repo travels further than a log line: the fetch uses it, ``GET
    /api/apps/registries`` returns it to dashboard clients, and the SEL trail
    records it. Redaction covers the sinks this module controls and would leave
    the others, so the value simply must not carry a secret — an edition should
    rely on the ambient git credentials the clone already has. ``https://`` refuses
    ANY userinfo, since a bare token is commonly the whole of it; ssh/scp refuse a
    ``user:password@`` form while allowing the conventional ``git@host``, which is
    a username, not a secret.

    Deliberately a mirror of ``routes._is_safe_repo_identifier`` rather than an
    import: ``routes`` imports this module, so the dependency can only run one
    way. Keep the two in step — both gate the same decision from opposite ends
    (operator-typed rows there, edition-pinned rows here).
    """
    repo = (repo or "").strip()
    if not repo:
        return False
    if ".." in repo or any(c in repo for c in " \t\n\r;|&$`<>()*?!\\\"'"):
        return False
    if re.match(r"^[A-Za-z0-9_\-]+$", repo):  # bare legacy name
        return True
    if repo.startswith("https://"):
        authority = repo[len("https://") :].split("/", 1)[0]
        return "@" not in authority
    if _is_ssh_git_url(repo):
        # Reject only a password-bearing userinfo; ``git@host`` is a username.
        # Userinfo is split off BEFORE the port: stripping at the first colon
        # would cut inside ``user:token@host`` and hide the very thing being
        # looked for.
        scheme, sep, rest = repo.partition("://")
        hostpart = (rest if sep else repo).split("/", 1)[0]
        userinfo = hostpart.rsplit("@", 1)[0] if "@" in hostpart else ""
        return ":" not in userinfo
    return False


def _pinned_registries() -> list[Any]:
    """The edition's default registries, materialised and validated.

    Rows the edition supplies are dicts (see ``AppsLoader.default_registries``);
    they are materialised into ``ExternalRegistryConfig`` here so every call site
    sees one attribute shape. A malformed row is dropped with a warning rather
    than raised on: this list feeds security gates
    (:func:`is_clone_host_trusted`), and those must keep answering.
    """
    try:
        edition_rows = current_context().apps_loader.default_registries()
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("edition default_registries() unavailable", exc_info=True)
        return []

    if not edition_rows:
        return []
    if isinstance(edition_rows, (str, bytes, dict)) or not hasattr(edition_rows, "__iter__"):
        # A companion returning a scalar (or a mapping, or a bare string) is a
        # companion bug, but this list feeds `is_clone_host_trusted` — the SSRF
        # gate must answer, not raise, so the malformed value is dropped whole.
        logger.warning(
            "Ignoring a malformed default_registries() return of type %s",
            type(edition_rows).__name__,
        )
        return []

    from kiro_crew.config.loader import ExternalRegistryConfig

    pinned: list[Any] = []
    for row in edition_rows:
        if not isinstance(row, dict):
            logger.warning("Ignoring a non-object edition registry row: %s", type(row).__name__)
            continue
        repo = row.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            logger.warning("Ignoring an edition registry row with no repo URL: %r", row.get("name"))
            continue
        repo = repo.strip()
        # An edition is trusted code, but a MISCONFIGURED one must not be able to
        # downgrade the transport that carries installable app code: this index is
        # cloned and its rows become install coordinates, so a plaintext fetch lets
        # anything on the path replace them. `PUT /api/apps/registries` already
        # refuses a non-https/ssh repo, and a pinned row must not be the weaker
        # door. Mirrored rather than imported because `routes` imports this module.
        if not _is_supported_registry_transport(repo):
            logger.error(
                "Ignoring edition registry %r: %s is not an https/ssh git URL or a bare name",
                row.get("name"),
                _redact_url_userinfo(repo),
            )
            continue
        name = row.get("name")
        branch = row.get("branch")
        trust = row.get("trust")
        pinned.append(
            ExternalRegistryConfig(
                name=name.strip() if isinstance(name, str) else "",
                repo=repo,
                branch=branch if isinstance(branch, str) and branch else "main",
                trust=trust if isinstance(trust, str) and trust else _TRUST_INDEX,
            )
        )
    # Two pinned rows sharing an effective key would fetch into the SAME
    # name-keyed cache file, so each refresh would overwrite the other and later
    # reads would list — and install — entries from whichever repository wrote
    # last. Drop ALL rows for a duplicated key rather than keeping the first: an
    # edition shipping two registries under one name has a bug, and picking a
    # winner would hide it behind intermittently wrong app listings.
    counts: dict[str, int] = {}
    for reg in pinned:
        key = _registry_identity_key(reg.name or reg.repo)
        counts[key] = counts.get(key, 0) + 1
    duplicated = {key for key, n in counts.items() if n > 1}
    if duplicated:
        for key in sorted(duplicated):
            logger.error(
                "Ignoring %d edition registries that would share the index cache file %r — "
                "they would overwrite each other's entries.",
                counts[key],
                key,
            )
        pinned = [
            reg for reg in pinned if _registry_identity_key(reg.name or reg.repo) not in duplicated
        ]
    return pinned


def _effective_registries() -> list[Any]:
    """The external registries in force: edition defaults + operator config.

    Every consumer of the registry list goes through here, so an edition-pinned
    registry is visible to index fetch/refresh, the trusted-host allowlist, row
    lookup, install, and the blob-proxy allowlist alike. A seam wired into only
    some of those would surface an app the install path then refuses — the
    half-implemented-mechanism failure mode.

    Merge rule: an **edition default wins** on a ``name`` collision, and when the
    two rows name DIFFERENT repositories **neither is served**. The second half is
    not fastidiousness: the on-disk index cache is keyed by registry NAME, so the
    displaced row's cache would be read under the winning row's identity and every
    reader stamps ``_registry`` from the registry it asked for — apps the pinned
    repository does not list, attributed to it and installable under it. Refusing
    the ambiguous name makes that a visible, diagnosable state instead. The
    credential path is separately safe (``_owner_tier_confirmed`` re-reads the
    real index), so this is about provenance, not escalation.

    Same name AND same repo is not a conflict — the pinned row simply supersedes
    an operator row that already agreed with it, and the shared cache is correct.

    Operators can add registries freely; they just cannot silently repoint one the
    edition pinned. ``PUT /api/apps/registries`` refuses to create such a
    collision, so the case that survives here is a ``config.json`` that already
    used the name before the build pinned it.

    Edition rows come first, which is also the lookup precedence
    :func:`_registry_app_candidates` documents, so a pinned registry is the first
    row consulted for a same-named app. A config-load failure degrades to the
    pinned rows alone rather than raising — the security gates must keep
    answering.
    """
    pinned = _pinned_registries()
    # Resolved at call time from the loader module (not the module-level import)
    # so this stays the single seam callers and tests already patch for the
    # config boundary — see test_catalog_inventory's registry-candidate tests.
    from kiro_crew.config.loader import (
        KiroCrewConfig,  # circular import: loader.py imports from apps/ at module level; deferring avoids ImportError
    )

    try:
        configured = list(KiroCrewConfig.load().registries or [])
    except Exception as exc:  # config load is best-effort for the security gates
        logger.debug("Could not load config for the registry list: %s", exc)
        configured = []

    if not pinned:
        return configured

    pinned_by_key = {_registry_identity_key(reg.name or reg.repo): reg for reg in pinned}
    contested: set[str] = set()
    kept_configured = []
    for reg in configured:
        key = _registry_identity_key(reg.name or reg.repo)
        rival = pinned_by_key.get(key)
        if rival is None:
            kept_configured.append(reg)
            continue
        if (rival.repo, rival.branch) != (reg.repo, reg.branch):
            contested.add(key)
            logger.warning(
                "Registry name %r is claimed by this build (%s@%s) and by your config (%s@%s); "
                "serving neither until the names differ, because the index cache is keyed "
                "by name and would otherwise be read under the wrong registry's identity.",
                key,
                _redact_url_userinfo(rival.repo),
                rival.branch,
                _redact_url_userinfo(reg.repo),
                reg.branch,
            )
        # Same repo AND same branch: the pinned row supersedes it, nothing is lost.

    return [
        reg for reg in pinned if _registry_identity_key(reg.name or reg.repo) not in contested
    ] + kept_configured


def _registry_trust_tier(registry_name: str) -> str:
    """The trust tier in force for the registry identified by *registry_name*.

    **Only a BUILD-PINNED registry can carry ``owner``.** A row in
    ``config.json`` is read as ``index`` no matter what it declares, because
    ``config.json`` is agent-writable — ``security.py`` says so in as many words,
    with the check inline: ``is_sensitive_bash_command("echo x > …/config.json")``
    is ``None``. A tier read from there would therefore not be an operator's
    assertion at all; a prompt-injected shell could mint ``owner``, and the same
    write also adds its chosen host to ``_configured_registry_hosts()`` and lets
    it control the index that :func:`_owner_tier_confirmed` re-fetches. Every
    layer that decision passes through would be one the same write had already
    satisfied. ``default_registries()`` ships in the wheel instead, so an
    ``owner`` tier is a claim the build makes and the agent cannot forge.

    *registry_name* is the ``_registry`` tag an index entry carries, which is the
    registry's ``name`` or (when unnamed) its ``repo``. Returns ``_TRUST_INDEX``
    for an unknown registry, an unrecognised tier, or any lookup failure — the
    caller uses this to decide whether to offer credentials, so every ambiguous
    answer must be the credential-free one.
    """
    if not registry_name:
        return _TRUST_INDEX
    try:
        # Pinned AND in force. `_pinned_registries()` alone is not enough: a name
        # contested between a pinned row and a config row is served by NEITHER
        # (see `_effective_registries`), and reading the tier off the pinned list
        # would keep granting `owner` for a registry whose apps are not being
        # listed at all. So the row must survive the merge and be one the build
        # pinned — config rows are read as `index` regardless.
        pinned_keys = {_registry_identity_key(reg.name or reg.repo) for reg in _pinned_registries()}
        wanted = _registry_identity_key(registry_name)
        if wanted not in pinned_keys:
            return _TRUST_INDEX
        for reg in _effective_registries():
            if _registry_identity_key(reg.name or reg.repo) == wanted:
                tier = getattr(reg, "trust", _TRUST_INDEX)
                if isinstance(tier, str) and tier in _REGISTRY_TRUST_TIERS:
                    return tier
                if tier != _TRUST_INDEX:
                    logger.warning(
                        "Registry %r declares unknown trust %r — reading it as %r",
                        registry_name,
                        tier,
                        _TRUST_INDEX,
                    )
                return _TRUST_INDEX
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("trust-tier lookup failed for %r", registry_name, exc_info=True)
    return _TRUST_INDEX


def _redact_url_userinfo(url: str) -> str:
    """Strip any ``user[:password]@`` from *url* before it reaches a log.

    A clone URL is index-supplied and may embed credentials
    (``https://user:token@host/path``). These URLs are written to the SEL audit
    trail and to warnings, both of which persist and the former of which is
    dashboard-readable, so the credential must not travel with them.

    Userinfo is removed rather than the whole URL: a record whose purpose is
    "credentials were offered to clone THIS" is worth little if it cannot say
    which repository, and a bare host cannot distinguish two repos on one forge.
    """
    if not url:
        return url
    scheme, sep, rest = url.partition("://")
    if sep:
        head, slash, tail = rest.partition("/")
        if "@" in head:
            host = head.rsplit("@", 1)[1]
            return f"{scheme}://[redacted]@{host}{slash}{tail}"
        return url
    # scp-style ``user@host:path`` carries no password, but the user is still an
    # identity; normalise it the same way so both forms read alike in a log.
    if "@" in url and ":" in url.split("@", 1)[1]:
        return "[redacted]@" + url.split("@", 1)[1]
    return url


def _sel_credential_decision(
    operation: str, git_url: str, *, granted: bool, reason: str = ""
) -> None:
    """SEL-audit a credential decision on a registry clone (best-effort).

    Records the REFUSAL as well as the grant. A refusal is the more interesting
    record of the two: `_owner_tier_confirmed` returns False when a fresh read of
    the registry's index does not list the coordinates the local row claims, which
    is exactly the signal that something tried to escalate and was stopped. Left
    to a rotating ``logger.warning`` alone, the one event an incident responder
    would want is the one that ages out.

    Only a decision on an ATTEMPTED escalation is recorded. The ordinary
    non-escalation answers — a registry at the default tier, a bundled entry, an
    entry with no URL — are not decisions about credentials and would bury the
    real ones under a record per browse.
    """
    if _sel_fn is None:
        return
    detail = f"owner_designated_clone url={_redact_url_userinfo(git_url)}"
    if reason:
        detail = f"{detail} reason={reason}"
    try:
        _sel_fn().log_api_access(
            caller="registry",
            operation=operation,
            outcome="granted" if granted else "denied",
            resources=detail,
        )
    except Exception as exc:
        logger.debug("SEL audit log failed for %s: %s", operation, exc)


def _sel_credential_grant(operation: str, git_url: str) -> None:
    """SEL-audit an owner-designated credential GRANT (best-effort).

    The same-repo carve-out and the owner tier both escalate a clone from
    anonymous+strict to owner credentials + context sandbox. That is a
    security-relevant permission decision and must leave an audit record,
    mirroring the existing ``fetch_external_registry`` SEL events.
    """
    _sel_credential_decision(operation, git_url, granted=True)


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

    This predicate is safe on the AUTOMATIC (browse/refresh) paths, which is why
    it is the only escalation they get: it compares against a URL the operator
    typed, so an entry read from the agent-writable index cache cannot widen it.
    The registry ``trust`` tier is deliberately NOT consulted here — see
    :func:`_owner_tier_confirmed`, which is install-only and re-confirms against a
    fresh index.

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

    for reg in _effective_registries():
        reg_key = reg.name or reg.repo
        if reg_key == registry_name:
            # Byte-identical comparison — the security contract.
            return effective_url == reg.repo
    return False


def _install_coordinates(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    """The four values that decide WHAT an install clones and runs.

    Name, clone URL, branch and subdirectory together select the bytes and the
    setup script. They are compared as one tuple by :func:`_owner_tier_confirmed`
    so a credential escalation requires the fresh index to agree on all of them,
    not merely on the repository.

    Byte-identical string comparison, no normalization — the same rule as the
    same-repo carve-out, for the same reason: any normalization here is a place
    two spellings could be made to collide.
    """
    return (
        str(entry.get("name", "") or ""),
        _entry_git_url(entry),
        str(entry.get("branch", "") or ""),
        str(entry.get("subdirectory", "") or ""),
    )


async def _owner_tier_confirmed(entry: dict[str, Any]) -> bool:
    """True when an ``owner``-tier registry FRESHLY confirms *entry*'s clone URL.

    The credential escalation an organisation-wide registry needs cannot come from
    :func:`_is_owner_designated_repo`: that compares against the index URL itself,
    and a real catalog's apps live in other repos. But it also cannot simply
    believe the row, because the row reaching this point was read from
    ``_read_external_registry_cache`` — **agent-writable** content, as
    :func:`_resolve_registry_row` says of the same file when it refuses to resolve
    an install from it. Trusting the tier on a cached row would let anything able
    to write that cache name an arbitrary repo on the operator's own forge and
    have it cloned with the gateway's git identity: the confused-deputy read the
    anonymous posture exists to prevent, merely relocated from the index to its
    cache.

    So the tier is honoured only after a FRESH fetch of that registry's index
    confirms an entry whose clone URL is **byte-identical** to this one's. That
    mirrors the official catalog, whose install coordinates likewise never come
    from a cache. Consequences, all deliberate:

    - **Install only.** Callers are the explicit per-app install action. The
      automatic browse/refresh clones keep the credential-free posture
      unconditionally, per :func:`anonymous_git_env`'s contract — they are not
      gated by any owner action, and a network round trip per listed row would be
      the wrong cost anyway.
    - **Fail closed, never fall back.** An unreachable index, a parse failure, a
      missing entry, or a URL that does not match exactly all return ``False``,
      which leaves the anonymous posture in place. The cost is availability on a
      path that already needs the network to clone.
    - **The fresh index is authority for the URL only.** It cannot promote the
      tier (that is read from operator/edition config) and it cannot widen the
      host set (``is_clone_host_trusted`` still gates the clone).
    - **Every install coordinate must match, not just the URL.** ``branch`` and
      ``subdirectory`` reach the clone from the same cached row, and
      :func:`_apply_configured_branch` forces the configured branch only onto
      **same-repo** entries — an owner-tier registry's apps are cross-repo by
      definition, so their branch declaration survives from the cache. Matching
      the URL alone would leave a poisoned row free to keep the curated URL and
      swap the ref, or point ``subdirectory`` at another app's directory in the
      same repo, and have either cloned with credentials and its setup script
      run. So the fresh row must agree on name, URL, branch AND subdirectory.
    """
    registry_name = entry.get("_registry")
    if not registry_name:
        return False

    effective_url = _entry_git_url(entry)
    if not effective_url:
        return False

    registry_name = str(registry_name)
    if await asyncio.to_thread(_registry_trust_tier, registry_name) != _TRUST_OWNER:
        return False

    reg = None
    for candidate in await asyncio.to_thread(_effective_registries):
        if (candidate.name or candidate.repo) == registry_name:
            reg = candidate
            break
    if reg is None:
        return False

    try:
        fresh = await _fetch_external_registry_index(reg.repo, reg.branch)
    except Exception:
        logger.warning(
            "owner-tier confirmation failed for %r; keeping the credential-free posture",
            registry_name,
            exc_info=True,
        )
        _sel_credential_decision(
            "install_from_registry_owner_tier",
            effective_url,
            granted=False,
            reason="index_unreadable",
        )
        return False
    if not fresh:
        logger.info(
            "owner-tier registry %r could not be re-read; keeping the credential-free posture",
            registry_name,
        )
        _sel_credential_decision(
            "install_from_registry_owner_tier",
            effective_url,
            granted=False,
            reason="index_unavailable",
        )
        return False

    fresh_rows = [row for row in fresh if isinstance(row, dict)]
    # Normalise the fresh rows the same way a cached row was normalised, so the
    # comparison is like-for-like rather than a branch-override artefact.
    _apply_configured_branch(fresh_rows, reg)

    wanted = _install_coordinates(entry)
    for row in fresh_rows:
        if _install_coordinates(row) == wanted:
            return True

    logger.warning(
        "owner-tier registry %r does not currently list app %r at %s (branch %r, subdir %r) — "
        "refusing the credential escalation",
        registry_name,
        wanted[0],
        _redact_url_userinfo(wanted[1]),
        wanted[2],
        wanted[3],
    )
    # The load-bearing audit record: the local row claimed coordinates the
    # registry's own current index does not list, which is what a poisoned cache
    # looks like from here.
    _sel_credential_decision(
        "install_from_registry_owner_tier",
        wanted[1],
        granted=False,
        reason="coordinates_not_in_fresh_index",
    )
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
    """Hosts of the external registries in force (trusted for SSH).

    A registry the owner deliberately added to their config — or one the edition
    pins as a default (:func:`_effective_registries`) — is a host they intend to
    authenticate to, so its SSH clones are allowed ~/.ssh access even if it is not
    a well-known public forge (e.g. a self-hosted Gitea/GitLab).
    """
    hosts = {
        _git_url_host(reg.repo) for reg in _effective_registries() if _git_url_host(reg.repo)
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
    commit: str = "",
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

    *commit*: a pinned commit. When set, the manifest is read from THAT tree
    rather than a branch tip, and the local fast path compares commits instead of
    branch names. This matters on the install path specifically: this manifest is
    what the admission gate inspects, so reading it from a branch tip while the
    install fetches a pinned commit would gate one tree and install another --
    and a pinned row carries no branch at all, so the branch would silently be
    the ``"main"`` default.
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
    if app_name and not commit:
        # A PINNED entry gets no local fast path at all.
        #
        # The persistent checkout is agent-writable, and `app.json` there can be
        # edited without HEAD moving -- so a commit comparison attests where the tree
        # was placed, never what it now holds. This manifest is what the admission and
        # platform gates read, so a local edit bypasses `installMode`/`os`
        # restrictions and gets the tree built server-side. It is the same reason a
        # pinned install never reuses an existing checkout; the rule belongs here too,
        # and previously stopped one caller short of this one.
        #
        # The cost is a shallow single-commit fetch per pinned listing, which the
        # pinned branch below already performs.
        clone_dir = app_source_dir(app_name)
        manifest_dir = _contained_join(clone_dir, subdirectory)
        local_manifest = manifest_dir / "app.json" if manifest_dir is not None else None
        fresh_enough = await _clone_branch_matches(clone_dir, branch)
        if (
            local_manifest is not None
            and local_manifest.is_file()
            and await _clone_origin_matches(clone_dir, git_url)
            and fresh_enough
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
        # Credential posture for the manifest fetch. Default: anonymous+strict
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

        if commit:
            # Read the manifest from the pinned tree. `--branch` cannot take a
            # commit id, and on the install path this manifest is what the
            # admission gate inspects -- gating a branch tip while installing a
            # pinned commit would check one tree and install another.
            fetch_log: list[str] = []
            # A NONEXISTENT child, not `tmp_root` itself. `tmp_root` already exists
            # (TemporaryDirectory created it), and `_git_fetch_commit` refuses a
            # destination that exists but is not a checkout -- the guard that stops it
            # from adopting, and later deleting, a directory it did not create. Handing
            # it `tmp_root` made every pinned manifest fetch fail, which left the
            # admission and platform-compatibility gates with no manifest at all.
            fetch_dest = Path(tmp_root) / "pinned"
            checkout_root = fetch_dest
            err = await _git_fetch_commit(
                git_url,
                commit,
                fetch_dest,
                fetch_log,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if err is not None:
                logger.debug("manifest fetch of pinned commit failed for %s: %s", git_url, err)
                return None
        else:
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
            checkout_root = Path(tmp_root)
        # Containment is measured from the root the tree actually landed in, which
        # differs by branch: the pinned fetch uses a child of `tmp_root` so it gets a
        # destination it created, the branch clone uses `tmp_root` itself.
        manifest_dir = _contained_join(checkout_root, subdirectory)
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


def _remote_controlled_url(entry: dict[str, Any]) -> bool:
    """Whether *entry*'s clone URL came from content we do not control.

    Drives the CREDENTIAL posture: a True answer means the clone runs
    credential-free and strict-sandboxed (:func:`anonymous_git_env`), because the
    URL is not one the owner typed.

    Both markers qualify. ``_registry`` is an external index's row. ``_catalog`` is
    the official catalog's, whose URL arrives in a document fetched over the network
    whose signature this client does not yet verify -- so it is remote-controlled in
    exactly the same way. Reading "no ``_registry``" as "owner-designated" held only
    while the sole marker-less rows came from the wheel's bundled seed, which the
    owner installed deliberately.
    """
    return bool(entry.get("_registry")) or bool(entry.get("_catalog"))


def _official_entry(entry: dict[str, Any]) -> bool:
    """Whether *entry* is an app WE list, which decides install-receipt eligibility.

    Deliberately NOT the negation of :func:`_remote_controlled_url`. A catalog row
    is remote-controlled (credential-free) AND official (receipt fires); collapsing
    both onto one boolean is what made the catalog row take owner credentials.
    """
    return not entry.get("_registry")


async def _move_checkout_aside(dest: Path, log_lines: list[str]) -> Path | None:
    """Atomically rename *dest* to a sibling temp path under the app-sources root.

    Returns the new path, or ``None`` if the rename failed. Nothing is ever
    deleted here: the caller reports the failure and the retention sweep owns the
    moved-aside directory's eventual removal.
    """
    aside = dest.with_name(f"{dest.name}.stale-{uuid.uuid4().hex[:8]}")
    try:
        await asyncio.to_thread(dest.rename, aside)
    except OSError as exc:
        log_lines.append(f"Could not move aside the checkout at {dest}: {exc}")
        return None
    # Refresh mtime so the retention clock starts now, not at the checkout's
    # last-modified time (which may already exceed the sweep threshold).
    # Best-effort: failure only shortens how long the directory survives.
    try:
        await asyncio.to_thread(os.utime, aside)
    except OSError:
        pass
    return aside


def _same_git_target(a: str, b: str) -> bool:
    """Whether two clone URLs name the same repository.

    Cosmetic variance between the separately-authored seed and catalog is a trailing
    ``/``, a trailing ``.git``, and the case of the scheme and host -- those three
    are normalised.

    **The PATH keeps its case.** Repository paths are case-sensitive on plenty of
    forges, so folding them makes two DIFFERENT repositories compare equal, and this
    predicate is what decides whether a catalog row may stand in for a bundled app.
    App trust is keyed by name, so a false "same target" here is the name-rebinding
    that requiring URL equality exists to prevent.
    """

    def norm(url: str) -> str:
        u = (url or "").strip().rstrip("/")
        if u.endswith(".git"):
            u = u[: -len(".git")]
        scheme, sep, rest = u.partition("://")
        if not sep:
            # No scheme to split on (scp-style or a bare path): fold nothing, since
            # the host cannot be told from the path without guessing.
            return u
        host, slash, path = rest.partition("/")
        return f"{scheme.lower()}://{host.lower()}{slash}{path}"

    return bool(a) and norm(a) == norm(b)


def _catalog_row_supersedes_seed(seed: dict[str, Any], catalog_row: dict[str, Any]) -> bool:
    """Whether *catalog_row* may stand in for the same-named *seed* row.

    The catalog is the shelf and the bundled seed is its offline snapshot, so when
    both describe the same app the catalog's row is the better one: it carries the
    curated copy AND the commit pin, while the seed carries four coordinate fields
    and no pin. Deferring to the seed instead is what made the pin dead data for
    every app that actually has one -- both git catalog entries are also seed
    entries, so a name-collision rule that favoured the seed discarded 100% of the
    published pins.

    Requiring URL equality is the security half. Without it, a catalog revision
    could rebind a bundled app's NAME to a different repository, and app trust is
    keyed by name -- so the row that replaces the seed must be describing the same
    repository the wheel shipped against, not merely claiming the same name.
    """
    return _same_git_target(_entry_git_url(seed), _entry_git_url(catalog_row))


def _is_catalog_row(entry: dict[str, Any]) -> bool:
    """Whether *entry* came from the official catalog and may skip the manifest fetch.

    Both halves are required. ``_catalog`` is on the row-projection allowlist, so
    an external registry's index -- untrusted, index-controlled JSON -- can set it
    on its own rows; ``_registry`` is attached server-side per configured registry
    and cannot be forged. Testing only ``_catalog`` would let such a row skip the
    fetch of the app's OWN manifest and keep index-supplied display copy instead,
    which is the substitution the manifest fetch exists to prevent.
    """
    return bool(entry.get("_catalog")) and not entry.get("_registry")


async def _identity(entry: dict[str, Any]) -> dict[str, Any]:
    """Return *entry* unchanged, as an awaitable.

    Lets the manifest-fetch gather hold a uniform list of coroutines while a
    catalog row skips the fetch entirely: the alternative is branching on row
    kind at the gather site AND at the result-zip below it, where an index
    mismatch would silently pair a row with another row's manifest.
    """
    return entry


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


#: Keys a registry index row may contribute to a merged app-store row.
#
# An index row is UNTRUSTED content — an external registry's index is
# user-supplied JSON — so the merge projects these names explicitly instead of
# spreading the row. Spreading shipped every key an index chose to invent
# straight to the browser, which both grew the payload with fields no consumer
# reads and gave an index a channel for keys the client never validated.
#
# Each name here has a reader: ``name`` is identity; ``gitUrl`` / ``repo`` /
# ``branch`` / ``subdirectory`` are the clone coordinates
# (``_entry_git_url``, ``install_from_registry``); ``resources`` selects the
# self-managed install path; ``detectInstalled`` is the pre-install probe;
# ``managed`` is the legacy registry-only flag; ``featured`` is the Discover
# spotlight flag (kept only on non-external rows by ``_apply_trust_fields``);
# ``_registry`` is the server-attached source tag; ``_index_author`` is the
# author snapshot ``_apply_trust_fields`` consumes for the verified mark.
#
# Display fields are deliberately ABSENT: they come from the fetched
# ``app.json`` below, so an index cannot publish display copy for an app whose
# manifest says otherwise. Install-status and trust fields are also absent —
# ``_enrich_with_install_status`` and ``_apply_trust_fields`` run after this
# and stamp them server-side.
_REGISTRY_ROW_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "gitUrl",
        "repo",
        "branch",
        # `_catalog` must survive the merge or the row goes back through the
        # per-app manifest clone it exists to make unnecessary.
        #
        # `commit` survives for data fidelity only, NOT as an authorization: this
        # projection also builds rows from an external registry's index, so the
        # value here is index-controlled. `install_from_registry` reads the pin only
        # for `_is_catalog_row`, which no index row can satisfy.
        "commit",
        "_catalog",
        "subdirectory",
        "resources",
        "detectInstalled",
        "managed",
        "featured",
        "_registry",
        "_index_author",
    }
)


def _merge_manifest(entry: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Merge app.json fields into a registry entry.

    Registry-only fields (``_REGISTRY_ROW_KEYS``) are preserved from the entry.
    Everything else comes from app.json, with the blob proxy URL pattern
    applied to image paths.
    """
    repo = entry.get("repo", "")
    result = {k: v for k, v in entry.items() if k in _REGISTRY_ROW_KEYS}

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

    # Icon — convert repo-relative path to blob proxy URL.
    #
    # Only ``iconPath`` (repo-relative) is honoured, never a manifest-declared
    # ``iconUrl``: an index-fetched manifest is untrusted content, and copying an
    # absolute URL out of it would let a third party point the store's <img> at
    # any host it likes. Rewriting a repo-relative path keeps every icon fetch
    # on our own proxy, which enforces the extension allowlist and the
    # trusted-host gate.
    icon_path = manifest.get("iconPath", "")
    if icon_path and repo:
        result["iconUrl"] = f"/api/apps/blob?repo={repo}&path={icon_path}"
    # Dark-appearance variant. Raster icons have fixed bytes, so an app that
    # must read well on both backgrounds ships two files; first-party
    # ``/app-assets/`` SVGs are inlined and repaint from theme tokens instead.
    icon_path_dark = manifest.get("iconPathDark", "")
    if icon_path_dark and repo:
        result["iconUrlDark"] = f"/api/apps/blob?repo={repo}&path={icon_path_dark}"
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


def _is_external_row(entry: dict[str, Any]) -> bool:
    """Whether *entry* is an EXTERNAL registry's row, for trust-field stamping.

    Refuses on the PRESENCE of an external marker rather than granting from its
    absence: ``_registry`` is attached server-side per configured registry and
    cannot be forged by index content, and ``provenance == "external"`` is the
    server-computed stamp derived from it. Deliberately NOT
    :func:`_remote_controlled_url`: a ``_catalog`` row is remote-controlled for
    CREDENTIAL purposes but is still an app WE list, so its trust fields are
    first-party.
    """
    return bool(entry.get("_registry")) or entry.get("provenance") == "external"


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
            # ``origin`` is trust-adjacent (surfaces read ``"builtin"`` as
            # first-party), and ``installed_map`` matches by NAME alone — so an
            # external registry's row named after an installed built-in must not
            # inherit that app's ``origin``, or the gateway emits a row whose
            # ``origin`` contradicts the ``provenance: "external"`` stamped
            # beside it by ``_apply_trust_fields``. External rows keep whatever
            # the trust boundary decides for them instead.
            if not _is_external_row(entry):
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


#: Index-declared author spellings that name US, folded by ``_fold_author``.
#
# The product name is two words, so the bundled catalog and the official
# published catalog both state ``Kiro Crew``; the historical bundled spelling
# was the single token ``kirocrew``. Both are us, so both mint the mark.
FIRST_PARTY_AUTHORS: frozenset[str] = frozenset(
    {"kirocrew", "kiro crew"}  # brand-ok: folded values, lower-cased by contract
)


def _fold_author(value: object) -> str:
    """Fold an author name for the first-party comparison.

    NFKC maps fullwidth forms onto ASCII, category-``Cf`` code points (ZWSP,
    soft hyphen, bidi marks) are dropped, and runs of whitespace collapse to a
    single space. Without this, ``Ｋｉｒｏ Ｃｒｅｗ`` and ``kiro\u200bcrew``
    read as us to a human but compare unequal, so an index row that legitimately
    names us in a non-ASCII form would silently lose the mark.

    Widening the match is safe HERE and only here: ``_apply_trust_fields``
    short-circuits every ``_registry``-tagged row to ``verified: False`` before
    consulting the author at all, so the folded comparison is only ever reached
    for rows whose index we ship or sign. Do not reuse this to GRANT trust on a
    path where untrusted content supplies the name.
    """
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKC", value)
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return " ".join(folded.split()).lower()


def _apply_trust_fields(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp server-computed ``provenance`` and ``verified`` on every row.

    SECURITY CONTRACT: these two fields are the API trust boundary for
    ``GET /api/apps/registry``. They are computed here — where the
    server-attached ``_registry`` tag is authoritative — and OVERWRITE any
    value an index entry may have published, so an external registry can
    never spoof them. Client code must read these fields and must not
    re-derive trust from the absence of ``_registry``, an internal tagging
    detail. ``_registry`` stays in the payload: the external-source label
    text, older clients, ``appManifest.ts::keysFor`` (first-party copy
    gate), and ``pickFeatured``'s legacy arm all still read it — do not
    stop emitting or rename it without migrating those dependants.

    Per row:

    - ``provenance``: ``"external"`` when ``_registry`` is set (the tag is
      applied server-side per configured registry and cannot be forged by
      index content); otherwise ``"builtin"`` when ``origin == "builtin"``,
      else ``"official"``.

      ``"official"`` means "an app WE list", and the bundled
      ``app-registry.json`` is one delivery of that list — the offline seed
      that ships inside the wheel. It answers the same question the remote
      signed catalog answers, so it gets the same value rather than a second
      one: two provenance values for one claim would put a weaker integrity
      guarantee (rides on the install artifact, cannot be revoked before the
      next release) behind a label a client cannot tell apart from the
      stronger one. The value names WHOSE list an app is on; how that list
      reached the client is a separate axis, and belongs in a separate field
      once there is more than one answer to record.

      ``"core"`` was the previous spelling. Clients accept both during the
      migration, so an older gateway's rows still label correctly.
    - ``verified``: ``True`` only when provenance is NOT ``"external"`` AND
      (``origin == "builtin"`` or the INDEX-declared author — snapshotted
      into ``_index_author`` by ``list_registry`` before the manifest merge
      — names us after ``_fold_author`` (see ``FIRST_PARTY_AUTHORS``). The
      badge asserts first-party
      provenance next to an Install button that runs setup code with
      gateway privileges, so it is never awardable from index-published
      trust keys or from the repo-fetched ``app.json``: a third-party core
      repo publishing ``"author": "kirocrew"`` in its manifest does not
      mint it (the merged ``author`` display field is deliberately NOT
      consulted).
    - ``featured``: dropped entirely from external rows so an external index
      can never self-flag into the Discover spotlight, regardless of client
      logic. Core-entry ``featured`` flags are preserved.
    - ``origin``: on external rows, any value other than the server-stamped
      ``"external"`` is dropped, so the wire never carries an ``origin`` that
      contradicts ``provenance: "external"`` — neither an index-published one
      nor one cross-stamped from an installed same-named app.
    """
    for entry in entries:
        index_author = entry.pop("_index_author", None)
        folded_author = _fold_author(index_author)
        if entry.get("_registry"):
            entry["provenance"] = "external"
            entry["verified"] = False
            entry.pop("featured", None)
            # ``origin`` is trust-adjacent (``"builtin"`` reads as first-party
            # to every consumer), and on an external row it can arrive from
            # untrusted content: an index may publish the key itself, and it
            # survives a failed manifest fetch because ``_resolve_manifest``
            # returns the row as-is on that path. The only value the server
            # itself stamps on an external row is ``"external"``
            # (``detectInstalled`` hits in ``_enrich_with_install_status``);
            # anything else must not go on the wire beside
            # ``provenance: "external"``. Scrubbed HERE and not only at the
            # sources because this function is the trust boundary — a rule
            # stated anywhere else is a rule some later assignment can undo.
            if entry.get("origin") != "external":
                entry.pop("origin", None)
        else:
            builtin = entry.get("origin") == "builtin"
            entry["provenance"] = "builtin" if builtin else "official"
            if entry.get("_catalog"):
                # A catalog row's author is curated copy from a document whose
                # signature this client does not yet check, so it must never mint
                # the first-party badge.
                #
                # The refusal lives HERE and not at the row's source because
                # omitting `_index_author` upstream does not survive: the snapshot
                # loop in `list_registry` assigns `_index_author = entry["author"]`
                # unconditionally, which silently re-created the very path the
                # omission was meant to close. This function is the trust
                # boundary, so a rule stated anywhere else is a rule some later
                # assignment can undo.
                entry["verified"] = False
            else:
                entry["verified"] = builtin or folded_author in FIRST_PARTY_AUTHORS
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
            if not isinstance(entry_name, str) or not KEBAB_RE.fullmatch(entry_name):
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


def _apply_configured_branch(entries: list[dict[str, Any]], reg, *, warn: bool = False) -> None:
    """Force the operator-configured registry branch onto same-repo entries.

    The registry index is cloned and parsed from exactly ``reg.branch``, so a
    same-repo entry declaring a different branch describes a state that does
    not exist on the ref the operator asked for (e.g. a pre-merge entry
    declaring ``main``); honouring it makes install clone a ref where the
    app's subdirectory is missing. The declared value is index-controlled
    (untrusted) content, while ``reg.branch`` already passed the branch regex
    gate before the fetch, so the override also narrows what a registry index
    can make the installer clone.

    A cross-repo entry — one whose effective clone URL differs from the
    configured registry repo — keeps its declaration: its branch names a ref
    in ANOTHER repository, about which ``reg.branch`` carries no information.
    The comparison is byte-identical string equality, matching the
    owner-designated carve-out semantics (no normalization, no host-level
    matching). A cross-repo entry with no usable declared branch still
    inherits ``reg.branch`` (an explicit JSON ``null`` counts as absent, so
    ``None`` can never flow to the clone coordinates).

    Runs at fetch finalisation AND on every cache read that feeds a branch
    consumer, so a cache written before the registry's branch config changed
    (or by a version that honoured per-app declarations) cannot keep an
    overridden branch alive until the next refresh. ``warn`` is set only on
    the fetch path so a divergent declaration is logged once per refresh
    rather than on every lookup.
    """
    for entry in entries:
        declared_branch = entry.get("branch")
        if _entry_git_url(entry) == reg.repo:
            if warn and declared_branch is not None and declared_branch != reg.branch:
                logger.warning(
                    "External registry %s entry %r declares branch %r; using the "
                    "configured registry branch %r",
                    reg.name or reg.repo,
                    entry.get("name"),
                    declared_branch,
                    reg.branch,
                )
            entry["branch"] = reg.branch
        elif not declared_branch:
            entry["branch"] = reg.branch


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
        if not isinstance(entry_name, str) or not KEBAB_RE.fullmatch(entry_name):
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
    # Ensure each entry has gitUrl/repo set (for install_from_registry), then
    # apply the operator-configured branch policy (see _apply_configured_branch:
    # same-repo entries get reg.branch forced with a divergence warning;
    # cross-repo entries keep their declaration).
    for entry in entries:
        entry.setdefault("gitUrl", reg.repo)
        entry.setdefault("repo", reg.repo)
        entry["_registry"] = name
    _apply_configured_branch(entries, reg, warn=True)
    await asyncio.to_thread(_write_external_registry_cache, name, entries)
    return entries


async def _load_external_registries() -> list[dict[str, Any]]:
    """Load app entries from all configured external registries.

    Reads the ``registries`` config field and fetches each repo's index.
    Results are cached for 1 hour. Each entry is tagged with its registry
    source for UI grouping.
    """
    registries = await asyncio.to_thread(_effective_registries)
    if not registries:
        return []

    all_entries: list[dict[str, Any]] = []

    async def _load_one(reg) -> list[dict[str, Any]]:
        name = reg.name or reg.repo

        # Try cache first
        cached = await asyncio.to_thread(_read_external_registry_cache, name)
        if cached is not None:
            for entry in cached:
                entry["_registry"] = name
            # Repair caches written before a branch-config change (or by a
            # version that honoured per-app declarations) — see helper.
            _apply_configured_branch(cached, reg)
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
            _apply_configured_branch(stale, reg)
            return stale
        logger.warning("Failed to load external registry %s from %s", name, reg.repo)
        return []

    results = await asyncio.gather(
        *[_load_one(reg) for reg in registries],
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
    registries = await asyncio.to_thread(_effective_registries)
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


async def _detect_installed_probe(
    entries: list[dict[str, Any]],
    installed_map: dict[str, dict[str, Any]],
) -> set[str]:
    """Run each entry's ``detectInstalled`` probe; return the names that report installed.

    Names already known to the app manager (present in *installed_map*) are
    skipped, as are names whose execution policy denies the probe. A probe
    timeout or ``OSError`` is swallowed and treated as not-installed. Shared by
    ``list_registry`` (offline path) and ``_append_external_registry_apps``
    (online path) so both probe identically -- an app installed OUTSIDE the app
    manager reads installed on either path.
    """
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
    return detected


async def _append_external_registry_apps(
    rows: list[dict[str, Any]],
    reserved_names: set[Any],
    installed_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Append user-configured external-registry apps to *rows*; return ``(rows, detected)``.

    The SINGLE site where external registries merge into a store listing, called
    by both ``list_registry`` (offline fallback) and ``list_catalog_apps`` (online
    catalog path) so the two paths cannot drift. External rows:

    - load via ``_load_external_registries`` (each server-tagged ``_registry``);
    - deduplicate by ``name`` against *reserved_names* AND each other, so a
      catalog/seed/builtin row always wins a collision and an external row only
      ADDS a name no reserved source claims (mirrors ``list_registry``'s original
      ``seen_names`` precedence). The caller reserves EVERY name the catalog and
      seed declare -- including a catalog ``git`` name it filtered out as
      not-yet-installable -- so an external row can never shadow a name install
      resolves by, which would point install-by-name at the wrong repository;
    - resolve display copy from the app's own ``app.json`` via ``_resolve_manifest``
      (the per-app fetch external rows pay today; catalog/seed rows do not pay it);
    - are probed with ``detectInstalled`` via ``_detect_installed_probe``.

    ``_index_author`` is deliberately NOT snapshotted here: every external row
    carries ``_registry``, so ``_apply_trust_fields`` takes its external branch,
    which drops ``_index_author`` and never derives the verified mark from it.
    """
    external = await _load_external_registries()
    seen = set(reserved_names)
    kept: list[dict[str, Any]] = []
    for entry in external:
        name = entry.get("name")
        if name in seen:
            continue
        seen.add(name)
        kept.append(entry)
    if not kept:
        return rows, set()
    resolved = await asyncio.gather(
        *[_resolve_manifest(e) for e in kept],
        return_exceptions=True,
    )
    kept = [r if isinstance(r, dict) else kept[i] for i, r in enumerate(resolved)]
    detected = await _detect_installed_probe(kept, installed_map)
    rows.extend(kept)
    return rows, detected


async def list_registry() -> list[dict[str, Any]]:
    """Return all registry apps with display info and install status.

    1. Load minimal registry JSON (name, repo, branch)
    2. Load external registries from user config
    3. Fetch each app's app.json (cached, 24h TTL) for display info
    4. Run detectInstalled commands for external installs
    5. Enrich with install status from KiroCrew's app manager
    6. Stamp server-computed trust fields (``provenance``/``verified``) and
       strip ``featured`` from external rows — see ``_apply_trust_fields``
    """
    entries = await asyncio.to_thread(_load_registry_file)

    # The catalog is the shelf, and the bundled seed is its offline snapshot.
    #
    # This runs FIRST, before the manifest fetches below, because a catalog row
    # already carries everything the list renders -- display copy, artwork, and
    # the `version` that decides whether an update is available. Applied here,
    # those rows need no per-app clone at all. Applied afterwards (which is what
    # `annotate` alone did) the clone is paid for and then overwritten.
    #
    # Seed rows LOSE a name collision when both name the same repository, because
    # the seed's four coordinate fields carry no pin: favouring it discarded every
    # published pin, since both git catalog entries are also seed entries. The seed
    # remains the fallback for when the catalog cannot be loaded at all (the
    # `except` below), which is the availability job it actually exists for. A
    # catalog row naming a DIFFERENT repository does not supersede -- see
    # `_catalog_row_supersedes_seed`.
    catalog_entries: list[dict[str, Any]] = []
    try:
        # A FRESH fetch, not `load_official_catalog` -- that reads the agent-writable
        # cache, and a cached row that MATERIALISES inventory can render with official
        # provenance and deduplicate the real same-named external row out of the
        # listing. The consent prompt would then describe an official app while the
        # name grant it produces installs the external one. The cache still feeds
        # `annotate` further down, which is display copy for rows that exist anyway
        # and skips rows carrying `_registry`.
        catalog_entries = await asyncio.to_thread(official_catalog.fetch_inventory_entries)
        if catalog_entries:
            by_name = {e.get("name"): i for i, e in enumerate(entries)}
            for row in official_catalog.inventory(catalog_entries):
                idx = by_name.get(row.get("name"))
                if idx is None:
                    entries.append(row)
                elif _catalog_row_supersedes_seed(entries[idx], row):
                    # Same app, same repo: take the catalog's row whole. It carries
                    # the pin and the curated copy; the seed carries neither.
                    # Replacing rather than overlaying `commit` onto the seed row is
                    # deliberate -- a row needs `_catalog` for the install path to
                    # honour its pin at all, and a seed row given `_catalog` would
                    # then skip the manifest fetch it depends on for display copy.
                    entries[idx] = row
                else:
                    logger.warning(
                        "catalog entry %r names a different repository than the "
                        "bundled seed (%r vs %r); keeping the seed row",
                        row.get("name"),
                        _entry_git_url(row),
                        _entry_git_url(entries[idx]),
                    )
    except Exception:  # noqa: BLE001 - degrade, never 500 the store
        # No catalog inventory this listing. That is the behaviour the store had
        # before this module existed (seed + built-ins discovered on disk), and it
        # is the right degradation: a name we cannot confirm right now must not
        # appear as an official row, because that row is what a consent grant is
        # made against.
        logger.warning("no official catalog inventory this listing", exc_info=True)

    # Load external registries from config, deduplicating against core and each other
    installed = await asyncio.to_thread(list_installed_apps)
    installed_map = {a["name"]: a for a in installed}
    # Snapshot the INDEX-declared author before the manifest merge below
    # overwrites ``author`` with the repo-fetched app.json value.
    # ``_apply_trust_fields`` derives ``verified`` from this snapshot only:
    # the bundled/edition index is trusted content, the fetched manifest is
    # the app author's — a repo publishing ``"author": "kirocrew"`` in its
    # app.json must not mint the badge. Unconditional assignment also
    # neutralizes an index that pre-seeds the key itself. External rows are
    # appended AFTER this by ``_append_external_registry_apps`` and always carry
    # ``_registry``, so ``_apply_trust_fields`` drops ``_index_author`` for them
    # and never reads it — which is why the helper does not snapshot it.
    for entry in entries:
        entry["_index_author"] = entry.get("author")

    # Fetch manifests in parallel, EXCEPT for catalog rows.
    #
    # A catalog row already carries the display fields and the `version` this
    # list needs, baked at publish time from the app's own app.json by a pipeline
    # that read it once, centrally. Cloning the app's repository again to learn
    # what the catalog already told us is the per-app network cost this document
    # exists to remove -- and it is O(N) in the number of third-party apps, which
    # is exactly the number the store is meant to grow.
    resolved = await asyncio.gather(
        *[_identity(e) if _is_catalog_row(e) else _resolve_manifest(e) for e in entries],
        return_exceptions=True,
    )
    entries = [r if isinstance(r, dict) else entries[i] for i, r in enumerate(resolved)]

    # Probe the seed/catalog rows, then append external registries at the single
    # shared merge site. Reserving every seed/catalog name means an external row
    # can only ADD a name none of them claim — the precedence the inline dedup
    # here used to enforce.
    detected = await _detect_installed_probe(entries, installed_map)
    entries, external_detected = await _append_external_registry_apps(
        entries, {e.get("name") for e in entries}, installed_map
    )
    detected |= external_detected

    # Overlay the official catalog's curated fields LAST among the content
    # sources, so they win over a fetched manifest -- that is what curation
    # means: the catalog is ours, the manifest belongs to the app. It runs BEFORE
    # the trust stamp so `_apply_trust_fields` still derives `verified` from the
    # index-declared author snapshot, which the overlay deliberately leaves
    # alone while the document's signature is not yet checked.
    #
    # Containment, not defensiveness. This handler has no try/except above it, so
    # anything escaping the catalog step is an HTTP 500 for the WHOLE store -- and the
    # curated copy is an enhancement to a listing that is already complete without it.
    # "Anything went wrong, render what we had" is therefore the correct semantics at
    # this seam specifically, and a broad catch here is not hiding a defect: it logs
    # with a traceback, and the module's own precise guards still run first.
    #
    # Annotate from the SAME fresh entries the inventory came from, never the cache.
    #
    # Round 11 stopped the agent-writable cache from INTRODUCING a row; this stops it
    # from REWRITING one. `annotate` overlays `displayName` and `description`, which
    # are exactly what the consent modal renders, and it only skips rows carrying
    # `_registry` -- so a poisoned cache entry could re-label a freshly fetched
    # first-party row and the name-scoped grant would then execute the real app under
    # a spoofed identity. Same value, different verb, same trust boundary.
    #
    # When the fetch failed, `catalog_entries` is empty and no catalog-derived copy is
    # applied at all. That is the correct degradation: rows then render the copy their
    # own manifest supplies, which is what the store did before this module existed.
    # There is no third source to fall back to -- a second source for the same rows is
    # precisely the defect.
    if catalog_entries:
        try:
            official_catalog.annotate(entries, catalog_entries)
        except Exception:  # noqa: BLE001 - degrade, never 500 the store
            logger.warning("ignoring curated catalog copy after a failure", exc_info=True)

    return _apply_trust_fields(
        _enrich_with_install_status(entries, installed_map, detected)
    )


async def list_catalog_apps() -> list[dict[str, Any]]:
    """Store rows built from the published catalog, enriched and trust-stamped.

    The JSON-only storefront path: when the published catalog is available its
    rows REPLACE the seed + per-app manifest fetch, so the store renders the
    published document's list and display copy. An empty result means the catalog
    was unavailable, and the caller falls back to ``list_registry`` offline.

    Install coordinates stay with the seed: the catalog is trusted only as far as
    TLS, so a ``git`` row is kept only when the seed or an external registry also
    names it, and it carries no clone URL of its own — install resolves the seed
    entry by name. A catalog-only ``git`` name renders nothing until it is
    installable. ``verified`` stays ``False`` for non-builtin rows until the
    catalog signature is checked, so this path never mints the first-party badge
    from a document trusted only as far as TLS.

    User-configured external registries (``config.registries``) are appended here
    too, through the same ``_append_external_registry_apps`` merge site
    ``list_registry`` uses, so they surface whether or not the catalog is
    reachable and are enriched, probed, and trust-stamped identically on both
    paths. A catalog/seed/builtin row WINS a name collision — external rows only
    ADD apps no catalog or seed name claims — and only external rows pay the
    per-app manifest fetch. The reserved names include EVERY catalog row name,
    snapshotted before the ``git``-installability filter below drops a
    not-yet-installable ``git`` row, so an external row can never shadow a name
    install resolves by (which would point install-by-name at the wrong repo).
    """
    # Off the event loop: the first call after a cache expiry does network I/O.
    rows = await asyncio.to_thread(official_catalog.list_catalog_rows)
    if not rows:
        return []
    installable = await asyncio.to_thread(_load_registry_file)
    installable_names = {e.get("name") for e in installable if isinstance(e, dict)}
    # Reserve every catalog name BEFORE the git filter, plus every seed name, so
    # an external row can never shadow a catalog/seed name — including a catalog
    # `git` row filtered out here for not being installable yet, whose name
    # install still resolves by.
    reserved_names: set[Any] = {row.get("name") for row in rows} | installable_names
    rows = [
        row
        for row in rows
        if row.get("source", {}).get("type") != "git" or row.get("name") in installable_names
    ]

    installed = await asyncio.to_thread(list_installed_apps)
    installed_map = {a["name"]: a for a in installed}
    rows, detected = await _append_external_registry_apps(rows, reserved_names, installed_map)
    return _apply_trust_fields(_enrich_with_install_status(rows, installed_map, detected))


def get_server_platform() -> dict[str, str]:
    """Return the server's platform info for frontend compatibility checks."""
    from kiro_crew.apps.manifest import PlatformConfig

    return {"os": PlatformConfig.current_os(), "arch": _platform.machine()}


def _seed_row(name: str) -> dict[str, Any] | None:
    """The bundled seed row named *name*, if this wheel shipped one."""
    for entry in _load_registry_file():
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _resolve_registry_row(name: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve *name* to an installable row, or a refusal reason.

    Searches the official catalog first, then the bundled seed, then external
    registry caches. Returns ``(row, reason)``; a non-empty *reason* means the
    caller must REFUSE and must not substitute another row.

    **"The catalog says there is no pin" and "I could not ask the catalog" are
    different answers, and collapsing them is a security defect.** A seeded
    official app has a published pin, so falling back to its branch-only seed row
    on a lookup failure installs a mutable branch tip while the store claims the
    app is pinned -- the pin silently not applying, which is this path's one
    quiet-yet-"successful" failure mode. So a lookup FAILURE with a seed row
    present refuses, while an authoritative "no catalog row" keeps using the seed.

    The availability cost is bounded and small: an install already requires the
    network to clone, so the only window this closes is "catalog host unreachable
    while the git host is reachable". Refusing there is the same choice made for
    the coordinate cache (never read for install) and for existing checkouts
    (never reused) -- a security property that holds only when the network
    cooperates is not one anybody can reason about.
    """
    seed_row = _seed_row(name)

    catalog_row: dict[str, Any] | None = None
    catalog_failed = False
    try:
        catalog_row = official_catalog.inventory_for_install(name)
    except official_catalog.CatalogUnavailable as exc:
        catalog_failed = True
        logger.warning("cannot confirm official coordinates for %r: %s", name, exc)
    except Exception:  # noqa: BLE001 - fail closed: an unexpected error is not "no row"
        catalog_failed = True
        logger.warning("official catalog lookup failed for %r", name, exc_info=True)

    if catalog_row is not None and (
        seed_row is None or _catalog_row_supersedes_seed(seed_row, catalog_row)
    ):
        # The catalog row carries the pin and the curated copy; a same-repo seed row
        # carries neither, so it does not win. Deferring to it is what made every
        # published pin unreachable on the install path.
        return catalog_row, ""

    if catalog_failed:
        # Before ANY fallback, seed or external. Without the catalog we cannot know
        # whether this name is a catalog app, and app trust is keyed by name: a
        # same-named external registry row would install a different repository's
        # code under a name the owner already permitted for execution. The local
        # cache cannot be consulted to decide -- it is agent-writable, which is the
        # surface `inventory_for_install` refuses to read in the first place.
        detail = (
            "is bundled and may carry an official commit pin"
            if seed_row is not None
            else "may be an official catalog app"
        )
        return None, (
            f"app {name!r} {detail}, but the official catalog could not be reached to "
            "confirm it — refusing to resolve it from another source. Retry when the "
            "catalog is reachable."
        )

    if seed_row is not None:
        return seed_row, ""
    return _external_registry_row(name), ""


def get_registry_app(name: str) -> dict[str, Any] | None:
    """Look up a registry app by name (synchronous, for internal use).

    Returns the row, or ``None`` when no source offers *name*.

    RAISES :class:`official_catalog.CatalogUnavailable` when resolution was refused
    because the catalog could not be consulted. Raising rather than returning None is
    what lets the refusal keep its reason while this stays the single lookup the
    install path goes through: a returned None is indistinguishable from "no such
    app", and re-deriving the difference in the caller costs a second catalog fetch
    and stops being sound as soon as the refusal covers more than one case.

    Blocking (reads the bundled file, fetches the catalog over HTTPS, and reads
    external index caches) — call it off the event loop.
    """
    row, refusal = _resolve_registry_row(name)
    if refusal:
        raise official_catalog.CatalogUnavailable(refusal)
    return row


def _external_registry_row(name: str) -> dict[str, Any] | None:
    """The first row named *name* from an owner-configured external registry cache.

    Separate from the catalog/seed resolution above because these rows are a
    different trust class: they carry ``_registry``, which flips provenance and is
    attached here at the lookup boundary so a stale cache cannot omit it.
    """
    for reg in _effective_registries():
        reg_name = reg.name or reg.repo
        cached = _read_external_registry_cache(reg_name, ignore_ttl=True)
        if cached:
            for entry in cached:
                if entry.get("name") == name:
                    # Repair a stale cache's branch before install reads it.
                    _apply_configured_branch([entry], reg)
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
    # The catalog is an inventory source, so it must appear here too. An app
    # installed from a catalog-only row records its provenance, and provenance-
    # pinned resolution then looks for a candidate offering that same source: a
    # candidate set that omits the catalog refuses EVERY later update of exactly
    # the apps this inventory exists to make installable.
    try:
        row = official_catalog.inventory_for_install(name)
        if row is not None:
            # REPLACE the equivalent seed candidates, do not merely outrank them.
            #
            # Ordering alone is not enough because provenance matching compares the
            # recorded source URL EXACTLY: an app installed when the seed's URL had no
            # `.git` suffix does not match a catalog URL that has one, so the match
            # walks past the catalog row and takes the retained seed -- delivering a
            # branch-tip update for an app the store presents as pinned. Ordering only
            # helped in the case that never needed help (URLs already identical).
            #
            # Replacing also keeps this path consistent with `_resolve_registry_row`,
            # where the catalog row wins outright; two resolvers disagreeing about the
            # same collision is how one of them ends up wrong.
            superseded = [c for c in candidates if _catalog_row_supersedes_seed(c, row)]
            if superseded:
                candidates = [c for c in candidates if c not in superseded]
                candidates.insert(0, row)
            else:
                candidates.append(row)
    except Exception:  # noqa: BLE001 - a failed lookup is not "no pin"; see below
        # A seed candidate names the SAME repository as the catalog row it shadows,
        # so provenance-pinned resolution would accept it and deliver a branch-tip
        # update for an app the store says is pinned. With the pin unknowable, the
        # seed candidates are dropped rather than offered.
        logger.warning(
            "official catalog lookup failed for %r; refusing to offer any candidate "
            "rather than resolving an update from an unconfirmed source",
            name,
            exc_info=True,
        )
        # Return immediately. Clearing the seed candidates and then falling through
        # to the external caches below still let a same-named external row answer --
        # and a tampered cache row missing its `_registry` marker reads as official,
        # so a provenance match would install an unpinned branch with the OWNER's
        # credentials. `_resolve_registry_row` already refuses before any fallback;
        # this is its sibling and must refuse the same way.
        return []
    for reg in _effective_registries():
        cached = _read_external_registry_cache(reg.name or reg.repo, ignore_ttl=True)
        for entry in cached or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                _apply_configured_branch([entry], reg)
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
        try:
            return get_registry_app(name), ""
        except official_catalog.CatalogUnavailable as exc:
            # A refusal, not an absence: report the cause so the user is not sent
            # looking for a missing app during a catalog outage.
            return None, str(exc)
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
        for reg in _effective_registries():
            cached = _read_external_registry_cache(reg.name or reg.repo, ignore_ttl=True)
            for entry in cached or []:
                if isinstance(entry, dict) and entry.get("repo") == repo:
                    _apply_configured_branch([entry], reg)
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
        for reg in _effective_registries():
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


def _restore_moved_aside(
    moved_aside: Path | None, pkg_dir: Path, log_lines: list[str], reason: str
) -> None:
    """Put a moved-aside checkout back at *pkg_dir*, setting the replacement aside.

    One restoration path, called from every exit that abandons a replacement clone.
    A pinned install moves the previous checkout aside on EVERY reinstall, so an exit
    that forgets this leaves the user's only edited copy as a `.stale-*` sibling that
    the retention sweep later deletes.

    TWO RENAMES, NO RECURSIVE DELETE, and that shape is what makes it callable from
    anywhere. Two review rounds pulled in opposite directions here: awaiting is unsound
    during cancellation (re-entering a loop being torn down surfaces as
    ``RuntimeError: Event loop is closed``), while a synchronous ``rmtree`` of a large
    checkout stalls the gateway's tasks and heartbeat on the loop thread. Both are right,
    so neither answer is -- the deletion itself is what has to go.

    What actually saves the user's data is the rename, which is O(1) on one filesystem.
    The discarded replacement is renamed to a ``.partial-*`` sibling and left for
    :func:`_sweep_stale_checkouts`, which already owns both the ``stale`` and ``partial``
    prefixes. So this function is cheap enough to run inline on any path, needs no thread
    and no loop, and cannot block.
    """
    if moved_aside is None or not moved_aside.exists():
        return
    if pkg_dir.exists():
        discarded = pkg_dir.with_name(f"{pkg_dir.name}.partial-{uuid.uuid4().hex[:8]}")
        try:
            pkg_dir.rename(discarded)
        except OSError as exc:
            # Cannot clear the destination, so the restore rename below would collide.
            # Leave both trees in place and say where the copy is.
            log_lines.append(
                f"WARNING: could not set aside the replacement at {pkg_dir}: {exc}; "
                f"the previous checkout is retained at {moved_aside.name}"
            )
            return
    try:
        moved_aside.rename(pkg_dir)
        log_lines.append(f"Restored the previous checkout after {reason}")
    except OSError as exc:
        log_lines.append(
            f"WARNING: could not restore the previous checkout from "
            f"{moved_aside.name}: {exc}; it is retained there for manual recovery"
        )


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


async def _git_fetch_commit(
    git_url: str,
    commit: str,
    dest: Path,
    log_lines: list[str],
    *,
    clone_env: dict[str, str],
    sandbox_mode: str,
) -> dict[str, Any] | None:
    """Materialise *dest* at exactly *commit*. Returns None on success.

    ``git clone --branch`` cannot take a commit id -- it exits 128 with
    ``Remote branch <sha> not found in upstream origin`` -- so a pinned entry
    needs fetch-by-SHA instead. The published catalog pins every third-party app
    to a commit, and this is the only path that honours that pin.

    Four separate git invocations, each of which MUST carry the same credential
    posture and sandbox mode as the clone it replaces: one that forgets is a
    credential-exposure hole, not a style slip.

    ``git remote add origin`` is not optional bookkeeping. ``git init`` + ``git
    fetch <url> <sha>`` leaves NO origin remote, and the update path reads it
    (:func:`_clone_origin_url`); without it every later update fails closed with
    ``unreadable_clone_origin`` and deliberately does NOT delete the checkout, so
    the app installs once and then needs manual cleanup to ever update again.

    ``--filter=blob:none`` is deliberately absent. A server that does not support
    it merely warns and sends everything, and a server that DOES support it turns
    the app's source tree into a partial clone whose later file reads become lazy
    network fetches -- during the build, off the install path's error handling.

    ``--template=`` is likewise load-bearing: the owner-designated posture uses
    :func:`minimal_env`, which does not disable the user's global git config, so a
    configured ``init.templateDir`` would install hooks into this repository and
    the checkout below would then execute ``post-checkout``.
    """

    async def run(argv: list[str], *, cwd: Path | None = None, timeout: int) -> tuple[int, str]:
        sandboxed, _cleanup = wrap_argv(argv, mode=sandbox_mode)
        sandboxed = cgroup_scope_argv(sandboxed)
        proc = await create_subprocess_limited(
            *sandboxed,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=clone_env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            return 124, "timed out"
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            raise
        return proc.returncode or 0, out.decode(errors="replace").strip()

    # Hardening applied to the network step. None of these are set anywhere on
    # the existing clone paths; the fetch is where an attacker-influenced URL
    # meets git, so they start here rather than nowhere.
    hardening = [
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "submodule.recurse=false",
        "-c",
        "fetch.recurseSubmodules=no",
    ]

    # Destination lifecycle, stated as one invariant because three reviewer findings
    # were three exits from the same mistake:
    #
    #   THIS FUNCTION MAY DELETE `dest` IF AND ONLY IF THIS INVOCATION CREATED IT
    #   AND DID NOT SUCCEED -- and that is decided in ONE place, not per branch.
    #
    # The earlier drafts put cleanup on each failure branch, so each fix closed one
    # exit and left the others: adopting a non-checkout destination (deleted the
    # user's files), a moved-aside restore that skipped on cancellation, and finally
    # a spawn exception between `git init` and `git remote add` that left a `.git`
    # directory with no origin -- which then wedges every later attempt on
    # `unreadable_clone_origin`, a fail-closed path that deliberately does not clean
    # up after itself.
    #
    # Cleanup therefore belongs to the LIFETIME of the thing created, not to the
    # enumeration of ways to fail. The `finally` below covers return, raise and
    # cancellation without any branch having to remember.
    created_here = False
    succeeded = False
    try:
        if (dest / ".git").is_dir():
            pass  # a checkout we can fetch into; never ours to delete
        elif dest.exists():
            # Refuse rather than adopt. `not (dest / ".git").is_dir()` answers "is
            # there a checkout here", which is NOT "am I creating this": a plain
            # directory of the user's files, or a `.git` FILE from a worktree link,
            # would read as fresh.
            log_lines.append(
                f"Refusing to fetch into {dest}: it exists but is not a git checkout "
                "(remove or fix it manually and retry)"
            )
            return {
                "ok": False,
                "name": dest.name,
                "error": "destination_not_a_checkout",
                "message": (
                    "The destination exists but is not a git checkout. Remove or fix it "
                    "manually and retry the install."
                ),
            }
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Set BEFORE the spawn: `git init` may create the directory and then
            # fail, or the spawn itself may raise, and either way the directory is
            # ours to discard.
            created_here = True
            code, out = await run(
                ["git", "init", "--quiet", "--template=", str(dest)], timeout=15
            )
            if code != 0:
                log_lines.append(f"git init failed (exit {code}): {out}")
                return {"ok": False, "name": dest.name, "error": "git init failed"}
            code, out = await run(
                ["git", "remote", "add", "origin", git_url], cwd=dest, timeout=15
            )
            if code != 0:
                log_lines.append(f"git remote add failed (exit {code}): {out}")
                return {"ok": False, "name": dest.name, "error": "git remote add failed"}

        log_lines.append(f"Fetching {git_url} at {commit[:12]}...")
        code, out = await run(
            ["git", *hardening, "fetch", "--depth", "1", "origin", commit],
            cwd=dest,
            timeout=_CLONE_TIMEOUT,
        )
        log_lines.append(out)
        if code != 0:
            # Fail closed. A server that will not serve this object (unreachable
            # commit, or one no ref contains) must not degrade into "install the
            # default branch instead" -- that is the pin silently not applying.
            return {
                "ok": False,
                "name": dest.name,
                "error": f"git fetch of pinned commit failed (exit {code})",
            }

        code, out = await run(
            ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=dest, timeout=15
        )
        if code != 0:
            log_lines.append(f"git checkout failed (exit {code}): {out}")
            return {
                "ok": False,
                "name": dest.name,
                "error": "git checkout of pinned commit failed",
            }

        # The pin only becomes real here. `_resolved_clone_commit` degrades to "" on
        # any read failure, so an empty answer is a FAILURE rather than a pass: the
        # whole point is refusing to build a tree we cannot identify.
        landed = await asyncio.to_thread(_resolved_clone_commit, dest)
        if landed != commit:
            log_lines.append(
                f"pinned commit not honoured: asked for {commit}, checkout reports "
                f"{landed or '<unknown>'} — refusing to install"
            )
            return {
                "ok": False,
                "name": dest.name,
                "error": "pinned commit verification failed",
            }
        succeeded = True
        return None
    finally:
        # `created_here` means the destination is this call's own `git init`, so a
        # failed fetch leaves a repository holding read-only pack files -- the same
        # removal requirement as the clone path in `_git_clone_or_pull`.
        if created_here and not succeeded:
            await asyncio.to_thread(platform_compat.rmtree_force, dest)


async def _git_clone_or_pull(
    git_url: str,
    branch: str,
    dest: Path,
    log_lines: list[str],
    *,
    index_originated: bool = False,
    pending_cleanup: list[Path] | None = None,
    restorable_stale: list[Path] | None = None,
    commit: str = "",
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
    # Whether *moved_aside* is the same repository (restore it on failure) rather
    # than a different one moved out of the way (retain it, never restore).
    moved_aside_is_restorable = False

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
            moved_aside = await _move_checkout_aside(dest, log_lines)
            if moved_aside is None:
                log_lines.append(f"Refusing to build from the stale clone at {dest}")
                return {
                    "ok": False,
                    "name": dest.name,
                    "error": "stale_clone_not_removed",
                    "message": (
                        "A checkout of a different repository is present and could not be "
                        "moved aside. Remove it manually and retry the install."
                    ),
                }

    if dest.is_dir() and (dest / ".git").is_dir():
        if commit:
            # A PINNED INSTALL NEVER REUSES AN EXISTING TREE.
            #
            # Four review rounds landed here, each a different way of trusting
            # on-disk state: adopting a destination that was not a checkout, an
            # exception leaving a half-built one, HEAD equality standing in for
            # contents, and finally content `git status` cannot report at all. The
            # last one is why no cleanliness check can close this class: `.git/`
            # is never reported by any `git status` variant, so an added
            # `.git/hooks/post-checkout` is invisible, and an untracked
            # `sitecustomize.py` executes on interpreter start if the tree lands on
            # `sys.path`. Both run attacker code while the receipt records the pin.
            #
            # So the pin's meaning is restored structurally instead: the tree the
            # build sees is one this call fetched, not one it inspected. The old
            # checkout is moved aside (never deleted) and the fresh-checkout path
            # below fetches into an empty destination created by
            # `git init --template=`, which is also what keeps hooks absent.
            #
            # The cost is one shallow fetch of a single commit per pinned reinstall.
            # The fast path it replaces was buying that round-trip with trust in an
            # agent-writable directory.
            moved_aside = await _move_checkout_aside(dest, log_lines)
            # This one is the SAME repository with the user's own edits, so a failed
            # transaction must put it back. The origin-mismatch move above is a
            # DIFFERENT repository's checkout: restoring that would hand the build the
            # very tree the mismatch gate refused, so it is only ever retained. One
            # list carrying both meanings is what made a failed pinned update either
            # lose the user's edits or resurrect the wrong repo, depending on which
            # rule won.
            moved_aside_is_restorable = moved_aside is not None
            if moved_aside is None:
                return {
                    "ok": False,
                    "name": dest.name,
                    "error": "existing_checkout_not_moved_aside",
                    "message": (
                        "The existing app checkout could not be moved aside, so a "
                        "pinned install cannot be performed safely. Remove or move it "
                        "manually and retry the install."
                    ),
                }
            # Fall through: `dest` no longer exists, so the pinned fetch below
            # creates it fresh inside the try/finally that owns restoration.
        else:
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
                        "error": (
                            f"git pull failed (exit {proc.returncode}); "
                            "not installing stale code"
                        ),
                    }
            except asyncio.TimeoutError:
                await _kill_process_group(proc)
                log_lines.append("git pull timed out — aborting")
                return {
                    "ok": False,
                    "error": "git pull timed out; not installing stale code",
                }
            return None

    # Fresh checkout.
    #
    # Both the pinned and the branch path run inside the SAME try/finally below,
    # which owns moved-aside restoration. The first draft gave the pinned path its
    # own restore, which skipped on a spawn exception or cancellation -- and the two
    # copies could nest, stranding the user's old checkout. One restoration path,
    # exercised by both.
    clone_succeeded = False
    try:
        if commit:
            result = await _git_fetch_commit(
                git_url,
                commit,
                dest,
                log_lines,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if result is not None:
                return result
            clone_succeeded = True
            return None

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

        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=clone_env,
        )
        # `rmtree_force`, never `shutil.rmtree(..., ignore_errors=True)`: what a
        # half-finished `git clone` leaves at *dest* is a git checkout, and git
        # creates `.git/objects/pack/*.{pack,idx,rev}` READ-ONLY. On Windows that
        # is the FILE_ATTRIBUTE_READONLY bit, so the unlink raises, `ignore_errors`
        # swallows it, and the tree stays on disk while this returns an error the
        # caller reads as "nothing was left behind".
        #
        # Only the FRESH-INSTALL path reaches the three removals below; when an
        # existing checkout was moved aside the `finally` owns the unwind and
        # already copes with a surviving tree. That asymmetry is the bug: with
        # nothing moved aside, an undeletable partial clone is never noticed, and
        # the next install finds `dest/.git` present with a matching origin and
        # takes the fast-forward branch instead -- `git pull` in a repo the clone
        # never finished, which fails, so every retry of that install fails too.
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
            log_lines.append(stdout.decode(errors="replace").strip())
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            await asyncio.to_thread(platform_compat.rmtree_force, dest)
            return {"ok": False, "name": dest.name, "error": "git clone timed out"}
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            await asyncio.to_thread(platform_compat.rmtree_force, dest)
            raise
        if proc.returncode != 0:
            await asyncio.to_thread(platform_compat.rmtree_force, dest)
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
                # Reported separately, because only a same-repository move may be
                # restored: putting an origin-mismatched checkout back would give the
                # build the tree that gate refused.
                if moved_aside_is_restorable and restorable_stale is not None:
                    restorable_stale.append(moved_aside)
            else:
                # Clone did NOT succeed — remove any partial dest and restore
                # the old checkout so the user's code is not stranded.
                await asyncio.to_thread(platform_compat.rmtree_force, dest)
                # If dest still exists the removal genuinely could not finish:
                # `rmtree_force` clears the read-only bit, but a file another
                # process holds OPEN still refuses to unlink on Windows. Move IT
                # aside so the restore rename cannot collide. Keep the path inside
                # app-sources.
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
    commit: str = "",
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
    # The restoration state is collected HERE, at the single return, rather than
    # stamped onto the result inside `_clone_build_app_locked`. That function has six
    # exits and the state was only attached on the successful one, so a post-fetch
    # failure -- a subdirectory that escapes containment, an identity mismatch, a
    # rejected admission -- dropped it, and the caller's `finally` had nothing to
    # restore from: the user's edited checkout went to the retention sweep. A list the
    # callee fills and this one exit reads cannot be forgotten by a new exit.
    restorable_stale: list[Path] = []
    try:
        result = await _clone_build_app_locked(
            git_url,
            app_name,
            log_lines,
            branch=branch,
            index_originated=index_originated,
            subdirectory=subdirectory,
            entry_repo=entry_repo,
            commit=commit,
            restorable_stale=restorable_stale,
        )
    except BaseException:
        # Cancellation and exceptions never reach the stamping line below, so the
        # caller's `finally` would see no state and the user's moved-aside checkout
        # would go to the retention sweep. There is no result dict to carry it on
        # this path, so restore HERE, where both the state and the destination are
        # known. `BaseException` on purpose: `CancelledError` is the reported case.
        #
        # SYNCHRONOUS, and that is the point: `await` during cancellation re-enters a
        # loop that is being torn down, which surfaces as `RuntimeError: Event loop is
        # closed` -- a failure this handler caused on three CI platforms at once. The
        # work is a rmtree plus a rename, so it never needed the loop.
        if restorable_stale:
            _restore_moved_aside(
                restorable_stale[0],
                app_source_dir(app_name),
                log_lines,
                "the build was interrupted",
            )
        raise
    if restorable_stale and isinstance(result, dict):
        result["_restorable_stale"] = list(restorable_stale)
    return result


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
    commit: str = "",
    restorable_stale: list[Path] | None = None,
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
    if restorable_stale is None:
        restorable_stale = []
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
        restorable_stale=restorable_stale,
        commit=commit,
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

    # Build in the directory that actually HOLDS the package, not the clone root.
    #
    # A monorepo registry entry declares `subdirectory`, and historically it was
    # joined only AFTER this build ran — so `_run_app_build` looked for
    # pyproject.toml/package.json at the clone root, found none, logged "No build
    # step detected — using source as-is", and returned ok=True having installed
    # nothing. The app's own pyproject.toml was never seen. A silent success is
    # the worst shape for this: `setup.onInstall` does get `cwd=app_source`, so
    # an app could paper over it with a script, which is exactly how a bug like
    # this stays hidden.
    #
    # `app_source` is already the containment-checked join of `subdirectory`
    # under the clone root (the identity gate above fails closed on an escaping
    # value), so it is safe to run the build command there.
    result = await _run_app_build(app_source, app_name, log_lines)
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
        # `sys.executable -m pip`, NOT `shutil.which("pip")`.
        #
        # A Python app has to land in the interpreter that will IMPORT it — the one
        # running this gateway. `which("pip")` resolves to whatever pip is first on
        # PATH, which is routinely a different interpreter: `bin/kirocrew` execs
        # `.venv/bin/kirocrew` WITHOUT putting the venv's `bin/` on PATH, and
        # `service/common.py::service_path()` prepends `~/.local/bin` ahead of it. So the
        # build pip was whatever the user happened to have.
        #
        # The failure is SILENT, which is why it survived. Measured on a host whose first
        # pip was 3.7 and whose gateway venv was 3.12: with a version-incompatible pip the
        # install failed loudly, but with a *compatible-but-different* pip (3.10) it
        # reported "Successfully installed", the build step reported success, and the
        # package landed in `~/.local/lib/python3.10/site-packages` — invisible to the
        # gateway, with `ENABLE_USER_SITE = False` in a venv so there is no fallback. The
        # app installs, the entry point never appears, and nothing anywhere says why.
        #
        # Our own packages only fail loudly because they declare `requires-python`; a
        # third-party app without that constraint fails silently on EVERY mismatch.
        #
        # EXCEPTION: never run pip against the desktop app's bundled interpreter.
        # The desktop build ships a python-build-standalone runtime inside the
        # application bundle (`Resources/backend-dist/...`); on macOS that bundle is
        # code-signed, so a pip install writing into its site-packages invalidates
        # the signature and breaks subsequent launches/updates — and the write is
        # discarded on every app update anyway. This is a LOUD failure, not a
        # soft-skip: a Python app that declares a build step needs its packages
        # importable by the gateway, and skipping the install while reporting
        # success would recreate exactly the silent-broken-install shape this
        # function is written to prevent. Detection lives in
        # platform_compat.is_bundled_interpreter() — the single owner of the
        # packaging-layout sentinel — so a bundler rename breaks its pinned test
        # instead of silently un-matching an inline check here.
        if platform_compat.is_bundled_interpreter():
            return {
                "ok": False,
                "name": app_name,
                "error": (
                    "Python apps that require a build step are not supported in "
                    "the desktop app: its bundled interpreter is inside the "
                    "signed application bundle and cannot install packages"
                ),
            }
        pip_cmd = [sys.executable, "-m", "pip"]
        if (build_dir / "requirements.txt").is_file() and not (
            (build_dir / "pyproject.toml").is_file() or (build_dir / "setup.py").is_file()
        ):
            build_cmds.append([*pip_cmd, "install", "-r", "requirements.txt"])
        else:
            build_cmds.append([*pip_cmd, "install", "."])

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
    # Pinning is a CATALOG mechanism, so the pin is read only for a catalog row.
    #
    # `commit` is on the row-projection allowlist (`_REGISTRY_ROW_KEYS`), and that
    # projection also builds rows from an external registry's index -- untrusted,
    # index-controlled JSON. Reading it unconditionally would hand that index a
    # capability its `branch` field cannot express: a fetch BY SHA reaches objects
    # no branch contains (a commit force-pushed away, or one that only ever existed
    # on a side ref), while a branch clone can only ever reach what a ref points at.
    # The owner-configured `branch` would then stop bounding which code gets built
    # and runs `onInstall`.
    #
    # `_is_catalog_row` is the right test rather than a bare `_catalog` check,
    # because `_catalog` is index-settable while `_registry` is attached server-side
    # per configured registry and cannot be forged.
    if _is_catalog_row(entry):
        commit = str(entry.get("commit", "") or "")
    else:
        commit = ""
        if entry.get("commit"):
            # Not a refusal: `branch` is exactly the coordinate such a row is
            # entitled to, so honouring it is correct. But an index author who
            # believes they pinned deserves to see that they did not.
            logger.warning(
                "ignoring commit pin on non-catalog row %r: pinning is a catalog "
                "mechanism; installing from branch %r instead",
                name,
                branch,
            )

    # The pin is honoured or the install is refused -- there is no third option.
    #
    # `branch` above defaults to "main", and that default is what makes a quiet
    # failure possible: a catalog row carries a commit and no branch, so a path
    # that ignored `commit` would clone the tip of "main", SUCCEED, and record the
    # tip's commit as this app's provenance. The store would then look like it
    # installs pinned bytes while installing whatever the app's default branch
    # holds today. Refusing a malformed pin is the only safe answer, because the
    # alternative is inventing coordinates nobody signed.
    if commit and not _COMMIT_SHA_RE.match(commit):
        return {
            "ok": False,
            "name": name,
            "error": (
                f"app {name!r} carries a malformed pinned commit; refusing to "
                f"install rather than fall back to a branch"
            ),
        }

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
    # Credential posture and OFFICIALNESS are two different questions, and a
    # catalog row is the case that separates them: its URL arrives in a document
    # fetched over the network whose signature this client does not yet verify, so
    # it is remote-controlled content exactly like an external index's URL -- but
    # it IS an app we list, so its install receipt must still fire.
    #
    # Treating "no `_registry`" as "the owner designated this repo" was true while
    # the only marker-less rows came from the wheel's bundled seed, which the owner
    # installed deliberately. A catalog row is not that: nobody typed its URL, and
    # a repointed row on a trusted forge would otherwise be cloned with the
    # gateway's ambient git/ssh identity -- the confused-deputy read this posture
    # exists to prevent.
    index_originated = _remote_controlled_url(entry)    # OFFICIALNESS is decided from `_registry` ALONE, and BEFORE the
    # owner-designated carve-out below: that carve-out flips index_originated as a
    # CREDENTIAL decision (owner explicitly designated the repo), but an
    # external-index entry never becomes an official-catalog entry — install
    # receipts must not fire for it. A catalog row has no `_registry`, so it stays
    # official even though it takes the credential-free posture above.
    official_entry = _official_entry(entry)
    # The originating external registry id, recorded as provenance. Empty means
    # the bundled (KiroCrew-shipped) catalog, which is itself a distinct source.
    # Captured BEFORE the owner-designated carve-out (same reasoning as above):
    # the entry still came from that external registry, and provenance must say so.
    source_registry = str(entry.get("_registry", "") or "")
    if index_originated and await asyncio.to_thread(_is_owner_designated_repo, entry):
        index_originated = False
        _sel_credential_grant("install_from_registry", _entry_git_url(entry) or "")
    elif index_originated and await _owner_tier_confirmed(entry):
        # An ``owner``-tier registry re-confirmed this exact clone URL in a fresh
        # fetch of its index. Install-only and never from the cache — see
        # `_owner_tier_confirmed`.
        index_originated = False
        _sel_credential_grant("install_from_registry_owner_tier", _entry_git_url(entry) or "")
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
        commit=commit,
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

    build_result: dict[str, Any] = {}
    # Cleared only after the transaction durably succeeds; the `finally` below reads it.
    durable_success = False
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
            # Passed so the BUILD runs where the package is. The containment check
            # below is still authoritative for choosing app.json's directory.
            subdirectory=subdirectory,
            entry_repo=repo,
            commit=commit,
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
            # Set BEFORE any of the fallible bookkeeping below, because this branch
            # returns ok=True regardless of how the registration and provenance writes
            # go: the clone is in place and the app will register itself on next
            # launch. Leaving it False would report success to the caller while the
            # `finally` rolled the source checkout back underneath it.
            durable_success = True
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
            # BEFORE the bookkeeping below, not after. `install_app`/`update_app` has
            # already copied the files into place, so the installed app IS updated. If
            # provenance persistence then raises, deciding "not durable" and rolling
            # the SOURCE checkout back would leave installed files from the new
            # version beside a source tree from the old one -- a torn state worse than
            # either outcome. A failed receipt is a bookkeeping problem to log; it does
            # not un-install what is installed.
            durable_success = True
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
    finally:
        # RESTORATION BELONGS TO THE LIFETIME, NOT TO THE LIST OF FAILURES.
        #
        # A pinned install moves the previous checkout aside on every reinstall, and
        # this function has seven post-clone exits (containment, identity mismatch,
        # two admission gates, onInstall, the install step, the happy path) plus an
        # exception path and cancellation. Restoring on the branches instead meant an
        # `onInstall` that exited non-zero returned early and left the user's only
        # edited copy as a `.stale-*` sibling for the retention sweep to delete.
        #
        # One site, reached by every exit. It is a no-op unless a moved-aside
        # checkout exists AND the transaction did not durably succeed, so the
        # pre-clone exits and the happy path both pass through untouched.
        if not durable_success:
            try:
                pending = build_result.get("_restorable_stale") or []
                if pending:
                    _restore_moved_aside(
                        Path(pending[0]),
                        # `app_source_dir(name)`, NOT `build_result["pkg_dir"]`: every
                        # post-clone FAILURE dict omits `pkg_dir`, so reading it raised a
                        # KeyError that the broad catch below swallowed -- the
                        # restoration silently did nothing on exactly the exits it
                        # exists for. The destination is a function of the app name, so
                        # derive it instead of depending on a key the failure paths do
                        # not carry. It is the clone ROOT either way: a `subdirectory`
                        # entry points `app_source` inside the tree, while the
                        # moved-aside sibling replaces the whole checkout.
                        app_source_dir(name),
                        log_lines,
                        "the install did not complete",
                    )
            except Exception:  # noqa: BLE001 - never mask the outcome being returned
                # WARNING, not debug: this catch is what hid the KeyError above for
                # four review rounds. A restoration that could not run is a possible
                # data loss, so it has to be visible in the log the user sees.
                logger.warning(
                    "could not restore the moved-aside checkout for %r", name, exc_info=True
                )
                log_lines.append(
                    "WARNING: the previous checkout could not be restored; recover it "
                    "from the .stale-* sibling directory"
                )
