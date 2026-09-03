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
from ipaddress import IPv6Address
from pathlib import Path
from typing import Any

from kiro_crew.apps import install_receipt, official_catalog
from kiro_crew.apps.admission import app_admission_denied, verified_signer
from kiro_crew.apps.execution import (
    app_execution_denied,
    repository_bound_grant_denied,
    trusted_app_repository,
)
from kiro_crew.apps.manager import (
    get_app,
    install_app,
)
from kiro_crew.apps.manager import list_apps as list_installed_apps
from kiro_crew.apps.manager import (
    registry_source_repository,
    set_app_provenance,
    update_app,
)
from kiro_crew.apps.manifest import (
    RESERVED_APP_NAME_CODE,
    AppManifest,
    app_name_error,
    is_reserved_app_name,
)
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)
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


#: Locale forced onto the INDEX-ORIGINATED clone (:func:`anonymous_git_env`) —
# the only clone whose stderr feeds the credential-posture classifier
# (:func:`_git_output_is_auth_shaped`). ``_SAFE_ENV_KEYS`` passes the operator's
# ``LANG``/``LC_ALL`` through to the child, so git would localize its client-side
# ``fatal: Authentication failed`` message on a non-English host — and the STRICT
# English-only marker allowlist would then miss, silently dropping the
# credential-posture hint for the exact credential-blocked owner it exists to
# help. Pinning ``LC_ALL`` (which wins over ``LANG`` and any narrower ``LC_*``)
# AFTER the ``os.environ`` copy makes that classifier's input deterministic
# English regardless of the operator locale. The value is a platform-appropriate
# UTF-8 locale — always English message text with UTF-8 decoding of any path
# bytes — because there is no single name valid on both libcs: ``C.UTF-8`` is the
# always-present UTF-8 locale on glibc/musl (Linux) but is NOT a valid BSD-libc
# locale on macOS, where an explicitly-set invalid ``LC_ALL`` makes ``setlocale``
# fall to C/ASCII AND suppresses CPython's PEP 538 coercion, so a child reading
# non-ASCII git output raises ``UnicodeDecodeError``. macOS ships ``en_US.UTF-8``
# in its base locale set, so Darwin uses that (mirroring
# :func:`kiro_crew.service.common` for the same reason). This is a
# location/format hint, never a credential, so it does not weaken any
# suppression in :func:`anonymous_git_env`. It is pinned ONLY there, not in
# :func:`minimal_env`, whose subprocesses never reach the classifier.
_GIT_CLONE_LOCALE = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"


def minimal_env(**extra: str) -> dict[str, str]:
    """Build a minimal environment dict from the current process env.

    Only passes through safe keys (PATH, HOME, SSH_AUTH_SOCK, etc.)
    plus any explicit *extra* overrides.  Used by both registry install
    and route-level uninstall handlers.

    The operator's ``LANG``/``LC_ALL`` are passed through unchanged: this env
    is NOT read by the credential-posture classifier (that runs only on the
    index-originated path, which uses :func:`anonymous_git_env`), so pinning a
    locale here would only degrade the many other ``minimal_env`` subprocesses
    (pip installs, app backends, lifecycle scripts, …) for no classifier
    benefit — and ``C.UTF-8`` is invalid on macOS BSD libc.
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
    # Pin the locale (over any operator LANG/LC_ALL from os.environ) so git's
    # client-side failure text stays English for the credential-posture
    # classifier — see :data:`_GIT_CLONE_LOCALE`. A benign format hint, not a
    # credential, so it preserves every suppression above.
    env["LC_ALL"] = _GIT_CLONE_LOCALE
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


def _public_registry_name(reg: Any) -> str:
    """Credential-free identity stamped on rows and returned to callers."""
    return _strip_git_target_userinfo(reg.name or reg.repo)


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
                        _strip_git_target_userinfo(registry_name),
                        tier,
                        _TRUST_INDEX,
                    )
                return _TRUST_INDEX
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug(
            "trust-tier lookup failed for %r",
            _strip_git_target_userinfo(registry_name),
            exc_info=True,
        )
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


def _owner_designated_repo_target(entry: dict[str, Any]) -> str:
    """Return the configured transport target for an exact same-repo row.

    External-registry rows and their on-disk cache are credential-free. When a
    legacy configured registry URL still carries HTTP userinfo, recover that raw
    value only from current config and only for the network call that needs it.
    Repository identity remains byte-exact and credential-free on both sides.
    """
    registry_name = entry.get("_registry")
    if not isinstance(registry_name, str) or not registry_name:
        return ""
    effective_url = _entry_git_url(entry)
    if not effective_url:
        return ""
    for reg in _effective_registries():
        public_repo = _strip_git_target_userinfo(reg.repo)
        if _public_registry_name(reg) == registry_name and effective_url == public_repo:
            return reg.repo
    return ""


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
    return bool(_owner_designated_repo_target(entry))


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
        if _public_registry_name(candidate) == registry_name:
            reg = candidate
            break
    if reg is None:
        return False

    try:
        fresh = await _fetch_external_registry_index(reg.repo, reg.branch)
    except Exception:
        logger.warning(
            "owner-tier confirmation failed for %r; keeping the credential-free posture",
            _strip_git_target_userinfo(registry_name),
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
            _strip_git_target_userinfo(registry_name),
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
        _strip_git_target_userinfo(registry_name),
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


def _git_target_has_ambiguous_scp_prefix(url: str) -> bool:
    """Whether a no-scheme target has Git's host/path colon before ``@``."""
    target = (url or "").strip()
    if "://" in target:
        return False
    at_index = target.find("@")
    colon_index = target.find(":")
    return at_index > 0 and 0 <= colon_index < at_index


def _git_target_has_ambiguous_ssh_userinfo(url: str) -> bool:
    """Whether an SSH URI has colon-bearing routing userinfo.

    Git passes the complete ``user:segment`` spelling to OpenSSH as the remote
    username; the segment is not a password field.  Rewriting it to ``user``
    would therefore make the consent/host identity differ from the transport.
    """
    target = (url or "").strip()
    scheme, sep, rest = target.partition("://")
    if not sep or scheme.lower() not in {"ssh", "git+ssh"}:
        return False
    authority_end = len(rest)
    for delimiter in "/?#":
        found = rest.find(delimiter)
        if found >= 0:
            authority_end = min(authority_end, found)
    authority = rest[:authority_end]
    userinfo, at, _hostport = authority.rpartition("@")
    return bool(at and ":" in userinfo)


def _normalized_ipv6_literal(value: str) -> str:
    """Canonical bracket contents, or ``""`` for malformed/non-IPv6 text."""
    if not value or "%" in value:
        return ""
    try:
        return IPv6Address(value).compressed.lower()
    except ValueError:
        return ""


def _valid_git_port(value: str) -> bool:
    """Validate a decimal TCP port without unbounded integer conversion."""
    return (
        1 <= len(value) <= 5
        and value.isascii()
        and value.isdigit()
        and 0 < int(value) <= 65535
    )


def _git_url_host(url: str) -> str:
    """Extract an exact lowercase host from a Git URI/SCP target.

    Bracketed IPv6 literals are validated and returned without brackets in
    canonical compressed form. Malformed authorities, unbracketed IPv6, empty
    hosts, and colon-before-``@`` SCP identities fail closed to ``""``.
    """
    target = (url or "").strip()
    if not target or any(ch.isspace() for ch in target):
        return ""
    if (
        "?" in target
        or "#" in target
        or _git_target_has_ambiguous_scp_prefix(target)
        or _git_target_has_ambiguous_ssh_userinfo(target)
    ):
        return ""

    scheme_end = target.find("://")
    if scheme_end >= 0:
        scheme = target[:scheme_end]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.\-]*", scheme):
            return ""
        rest = target[scheme_end + 3 :]
        authority_end = len(rest)
        for delimiter in "/?#":
            found = rest.find(delimiter)
            if found >= 0:
                authority_end = min(authority_end, found)
        authority = rest[:authority_end]
        if not authority or authority.count("@") > 1:
            return ""
        _userinfo, at, hostport = authority.rpartition("@")
        if not at:
            hostport = authority

        if hostport.startswith("["):
            close = hostport.find("]", 1)
            if close <= 1:
                return ""
            suffix = hostport[close + 1 :]
            if suffix and (
                not suffix.startswith(":")
                or not _valid_git_port(suffix[1:])
            ):
                return ""
            return _normalized_ipv6_literal(hostport[1:close])

        if any(ch in "[]/@" for ch in hostport):
            return ""
        if ":" in hostport:
            if hostport.count(":") != 1:
                return ""
            host, port = hostport.rsplit(":", 1)
            if not _valid_git_port(port):
                return ""
        else:
            host = hostport
        return host.lower() if host else ""

    if target.count("@") > 1:
        return ""
    _userinfo, at, host_and_path = target.rpartition("@")
    if not at:
        host_and_path = target
    if host_and_path.startswith("["):
        close = host_and_path.find("]", 1)
        if close <= 1 or close + 1 >= len(host_and_path):
            return ""
        if host_and_path[close + 1] != ":" or not host_and_path[close + 2 :]:
            return ""
        return _normalized_ipv6_literal(host_and_path[1:close])

    colon = host_and_path.find(":")
    if colon <= 0 or not host_and_path[colon + 1 :]:
        return ""
    host = host_and_path[:colon]
    if any(ch in "[]/@" for ch in host):
        return ""
    return host.lower()


def _is_ssh_git_url(url: str) -> bool:
    """True when *url* clones over SSH (and would need ~/.ssh for key auth)."""
    target = (url or "").strip()
    scheme_end = target.find("://")
    if scheme_end >= 0:
        return target[:scheme_end].lower() in {"ssh", "git+ssh"} and bool(
            _git_url_host(target)
        )
    return "@" in target and bool(_git_url_host(target))


def _clone_sandbox_mode(git_url: str, trusted_hosts: frozenset[str] | None = None) -> str:
    """Pick the sandbox mode for cloning *git_url*.

    Returns ``"standard"`` (exposes ~/.ssh so git can offer the owner's SSH
    keys) ONLY for an SSH/scp remote whose host is trusted — a well-known
    public forge or a host the user explicitly configured as an external
    registry. All other cases return ``"strict"`` (~/.ssh hidden): https/git
    remotes never need SSH keys, and an untrusted SSH host fails closed rather
    than being offered the owner's private keys.
    """
    if _git_target_is_unsupported(git_url) or not _is_ssh_git_url(git_url):
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
    if _git_target_is_unsupported(git_url):
        return "strict"
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
    if _git_target_is_unsupported(git_url):
        return False
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
    credential_target = git_url or repo
    if _git_target_is_unsupported(credential_target):
        logger.warning("registry manifest fetch refused an unsupported clone target")
        return None
    git_url = _strip_git_target_userinfo(credential_target)
    credentialed_transport = credential_target != git_url

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
            _strip_git_target_userinfo(git_url),
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
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if err is not None:
                logger.debug(
                    "manifest fetch of pinned commit failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    str(err),
                )
                return None
        elif credentialed_transport:
            # A credentialed `git clone` performs both the network fetch and the
            # checkout in one process. The checkout may launch an inherited filter
            # selected by the fetched `.gitattributes`, which would inherit the
            # one-shot URL rewrite. Split the operations and give only fetch the
            # credential-bearing environment.
            fetch_log = []
            fetch_dest = Path(tmp_root) / "branch"
            checkout_root = fetch_dest
            err = await _git_fetch_branch(
                git_url,
                branch,
                fetch_dest,
                fetch_log,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if err is not None:
                logger.debug(
                    "manifest fetch of branch failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    str(err),
                )
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
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                clone_cmd, mode=sandbox_mode, _prepare=wrap_argv
            )
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            transport_env = _git_transport_env(credential_target, git_url, clone_env)
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=transport_env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            _, stderr = await _communicate_with_timeout(proc, timeout=_CLONE_TIMEOUT)
            if proc.returncode != 0:
                logger.debug(
                    "manifest clone failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    _loggable_git_transport_output(
                        stderr.decode(errors="replace").strip(),
                        credentialed=credentialed_transport,
                    ),
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
        logger.debug(
            "Failed to fetch app.json from %s: %s",
            _strip_git_target_userinfo(git_url),
            _loggable_git_transport_output(str(exc), credentialed=credentialed_transport),
        )
        return None
    finally:
        if tmp_root:
            await _rmtree_force_settled(tmp_root)


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


class _MoveAsideUndoFailed(OSError):
    """A move-aside undo failed, stranding the checkout at *aside*.

    The retained-path carrier for the round-11 undo contract: whenever a
    checkout is left physically at ``aside`` (not ``dest``) — possibly holding
    an already-expired mtime — the caller MUST learn the exact ``aside`` path
    to report it retained instead of letting the age-based sweep delete an
    unnamed recovery copy. Carries ``aside`` as an attribute (not a re-derived
    string) so :func:`_move_checkout_aside` reports the true on-disk path.

    :func:`_rename_and_refresh_mtime` now refreshes *dest*'s mtime BEFORE the
    rename, so a refresh failure fails closed before anything moves and never
    strands a copy under a ``.stale-*`` name — it does not raise this. The
    class and its handler are kept as the fail-closed contract for any residual
    stranding path (the cancellation-settlement undo in
    :func:`_move_checkout_aside`).
    """

    def __init__(self, aside: Path, cause: BaseException) -> None:
        super().__init__(f"move-aside undo failed; checkout retained at {aside}: {cause}")
        self.aside = aside


def _rename_and_refresh_mtime(dest: Path, aside: Path) -> None:
    """Refresh *dest*'s mtime, then rename it to *aside*, in one thread call.

    The mtime refresh runs BEFORE the rename, so the moved-aside directory
    already carries a fresh retention clock the instant it appears under its
    sweep-recognized ``.stale-*`` name. This closes a concurrent-sweep race the
    old rename-then-utime ordering left open: between ``dest.rename(aside)``
    landing and a following ``os.utime(aside)`` completing, *aside* is already
    visible under the sweepable name while still holding the checkout's OLD
    (possibly already-expired) mtime, so a CONCURRENT ``install_from_registry``
    running the age-based sweep in that sub-syscall window could delete the
    user's recovery copy. Refreshing *dest* first means *aside* is never
    observable under a swept name with a stale clock — the two syscalls stay in
    one ``asyncio.to_thread`` invocation, so a cancellation between them cannot
    reorder them either.

    The mtime refresh is NOT best-effort. A moved-aside checkout is the user's
    recovery copy, and the fresh mtime is the whole retention window: a
    checkout already older than :data:`_STALE_CHECKOUT_RETENTION_DAYS` would be
    sweep-eligible the instant it appears under the ``.stale-*`` name if its
    mtime were not renewed, so a silently-swallowed ``utime`` failure would hand
    the age-based sweep a green light to delete the recovery copy on the very
    next install. So a ``utime`` failure fails CLOSED before anything moves:
    *dest* has not been renamed yet, so it is still the caller's checkout at its
    original path with its original clock untouched, and this simply re-raises.
    Nothing is ever stranded under a ``.stale-*`` name by a failed refresh
    because the refresh precedes the rename. The caller
    (:func:`_move_checkout_aside`) turns the re-raised error into a ``None``
    return, and every caller of that fails closed with the non-destructive
    ``stale_clone_not_removed`` refusal — the checkout is left in place.

    Only once the refresh SUCCEEDS is the rename attempted. ``Path.rename`` is a
    single atomic syscall: it either moves *dest* to *aside* (now carrying the
    fresh clock) or leaves *dest* exactly where it was, so a rename failure
    strands nothing and re-raises for the same fail-closed handling. The
    round-11 :class:`_MoveAsideUndoFailed` retained-path contract is preserved
    on the caller side: its handler still reports the exact ``.stale-*`` path if
    a checkout is ever found stranded there (e.g. the cancellation-settlement
    undo below), so no recovery copy is ever swept unnamed.
    """
    # Refresh the retention clock IN PLACE first, so the moved-aside dir carries
    # a fresh mtime the instant it becomes visible under the sweep-recognized
    # name. A utime failure fails closed here: dest has not moved, so the caller
    # keeps its checkout untouched at its original path and refuses
    # non-destructively. Never rename after a failed refresh — that is what
    # would strand an un-refreshed copy under a swept name.
    os.utime(dest)
    dest.rename(aside)


async def _move_checkout_aside(dest: Path, log_lines: list[str]) -> Path | None:
    """Atomically rename *dest* to a sibling temp path under the app-sources root.

    Returns the new path, or ``None`` if the rename failed. Nothing is ever
    deleted here: the caller reports the failure and the retention sweep owns the
    moved-aside directory's eventual removal. The mtime refresh now runs BEFORE
    the rename, so a refresh failure fails closed with *dest* untouched at its
    original path and strands nothing under a ``.stale-*`` name — it surfaces as
    the ``Could not move aside`` log line and a ``None`` return. The
    ``.stale-*`` retained-path report (via :class:`_MoveAsideUndoFailed`) is
    still emitted for the one residual strander, the cancellation-settlement
    undo below, so no stranded recovery copy is ever left unreported.

    Report durability: every branch that strands a recovery copy at a
    ``.stale-*`` path records it with ``logger.warning`` as well as appending to
    *log_lines*. The request-local list is discarded when a gateway shutdown
    cancels the update before it returns (the response the list renders into is
    never sent), so a list-only report would leave the age-based sweep to delete
    an unnamed recovery copy. The durable process-log line is what survives that
    shutdown, so the retained path is always recoverable.

    Cancellation safety: the rename+mtime-refresh runs on a retained worker
    future, and the handler SETTLES that worker before inspecting *aside*.
    Cancelling the awaiting task does not cancel a thread already running in
    the executor -- ``asyncio.Future.cancel()`` returns while the worker runs
    on -- so a bare ``if aside.exists():`` check would race the in-thread
    rename: a worker past dispatch but pre-rename at check time would complete
    the rename after the handler re-raised, stranding the checkout at *aside*
    unrecorded. Awaiting the worker to completion first makes the inspection
    deterministic: if the rename ran, this synchronously moves *aside* back to
    *dest* so the caller's state is unchanged by the attempt; if that undo
    itself fails, the aside path is logged durably (``logger.warning``, so it is
    never silently strandable even when the cancelling shutdown discards
    *log_lines*) before the cancellation is re-raised. Repeated cancellation
    does NOT get
    to skip that undo-or-log: the worker is an executor THREAD and will finish
    regardless of how many times the awaiting task is cancelled, so the
    settlement loop absorbs every further ``CancelledError`` until the worker
    future is done, THEN runs the synchronous undo-or-log, THEN re-raises a
    single ``CancelledError``. The earlier "acceptable to skip on a second
    cancel" behavior was wrong by this PR's own standard: a skipped undo leaves
    the checkout at an UNREPORTED ``.stale-*`` path that the retention sweep
    later deletes -- exactly the silent-deletion class this surface exists to
    close, and an mtime refresh only delays the sweep, it does not report the
    path. No new ``await`` runs after settlement: the undo/log is synchronous
    so it cannot itself be interrupted.
    """
    aside = dest.with_name(f"{dest.name}.stale-{uuid.uuid4().hex[:8]}")
    loop = asyncio.get_running_loop()
    # Retain the worker future so the CancelledError handler can settle it
    # before inspecting *aside*; shield keeps a task cancel from propagating
    # into the executor item (a thread cannot be cancelled anyway).
    worker = loop.run_in_executor(None, _rename_and_refresh_mtime, dest, aside)
    try:
        await asyncio.shield(worker)
    except _MoveAsideUndoFailed as undo_failed:
        # The utime refresh failed AND the rename-back failed: the checkout is
        # stranded at *aside* (NOT dest) with a possibly-expired mtime. Report
        # the exact retained path so the sweep does not delete an unnamed
        # recovery copy -- the same undo-or-log contract the cancellation path
        # below honours. Must come before the generic OSError handler since
        # _MoveAsideUndoFailed subclasses OSError.
        #
        # Record it DURABLY as well as into log_lines: every branch that strands
        # a recovery copy at a .stale-* path names it in the process log, not
        # only in the request-local list, so the retained path survives a
        # gateway shutdown that discards the response the list would have
        # rendered into.
        logger.warning("Previous checkout retained at: %s", undo_failed.aside)
        log_lines.append(f"Previous checkout retained at: {undo_failed.aside}")
        return None
    except OSError as exc:
        # utime failed but the rename-back succeeded: the checkout is back at
        # *dest* with its original clock, nothing stranded, so naming dest is
        # the honest report.
        log_lines.append(f"Could not move aside the checkout at {dest}: {exc}")
        return None
    except asyncio.CancelledError:
        # Settle the worker before inspecting *aside*: the shield delivered the
        # cancel to us while the thread may still be mid-flight, and only once
        # the worker has finished is aside.exists() an honest reading of whether
        # the rename ran. The worker is a thread and WILL finish, so keep
        # awaiting it across any FURTHER cancellation: a second cancel delivered
        # during settlement must not skip the undo-or-log below and strand the
        # checkout at an unreported .stale-* path. Absorb each extra cancel and
        # re-await until the future is done; asyncio.wait never re-raises the
        # worker's own exception (we do not need its result, only that it
        # settled).
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                # A repeated cancel landed on the settling await. Loop: the
                # thread is still running and the undo-or-log is owed either way.
                continue
        # Settled. The undo-or-log is synchronous, so it runs to completion
        # even under a pending cancellation, then a single CancelledError is
        # re-raised to the caller.
        if aside.exists():
            try:
                aside.rename(dest)
            except OSError as undo_exc:
                # The undo failed, so the recovery checkout is stranded at the
                # .stale-* aside path. The cancellation that brought us here is
                # typically a gateway shutdown, which DISCARDS log_lines (the
                # request never returns to render them), so the request-local
                # append alone would leave the age-based sweep to delete an
                # unnamed recovery copy. Emit a durable logger.warning FIRST so
                # the retained path survives the shutdown in the process log;
                # the log_lines append still carries it into the response on the
                # non-shutdown cancellation paths that do return.
                logger.warning(
                    "Cancelled while moving aside %s; the checkout is retained "
                    "at %s and could not be restored: %s",
                    dest,
                    aside,
                    undo_exc,
                )
                log_lines.append(
                    f"Cancelled while moving aside {dest}; the checkout is "
                    f"retained at {aside} and could not be restored: {undo_exc}"
                )
        raise
    return aside


async def _rmtree_force_settled(path: str | Path) -> None:
    """Remove *path* fully before cancellation can escape to a path reuser.

    Cancelling ``asyncio.to_thread`` does not stop its worker. Returning while
    that worker is still deleting is unsafe for update rollback: the caller may
    restore an old checkout at the same path, after which the orphaned worker
    can delete the restored tree. Retain and shield the executor future, absorb
    repeated cancellation until it settles, then propagate cancellation.
    """
    loop = asyncio.get_running_loop()
    worker = loop.run_in_executor(None, platform_compat.rmtree_force, path)
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                continue
        raise


def _strip_git_target_userinfo(url: str) -> str:
    """Return *url* without embedded secrets, preserving SSH usernames.

    Clone credentials select *who may fetch* a repository; they are not part of
    the repository's identity. Keeping them in the consent identity both made
    credential rotation invalidate an otherwise identical grant and, worse,
    carried secrets through ``trustRepository`` into config and the dashboard.

    HTTP(S) userinfo is an authentication capability, so it is removed in full.
    In SSH and git+ssh URLs the username is transport routing (and is often
    required by the server); retain username-only userinfo. Colon-bearing SSH
    userinfo is also routing text rather than a password field, so executable
    and governance paths reject it as an ambiguous identity. This metadata-only
    sanitizer redacts the suffix rather than returning it through persistence.
    The scp form (``user@host:path``) has no password field, so its username is
    retained; a password-like prefix is reduced to that username. URI query and
    fragment components are also excluded because a free-form source can place a
    token there and no downstream API/log/persistence sink can classify every
    provider-specific secret key safely. Bare/local paths remain untouched.
    """

    target = (url or "").strip()
    scheme, sep, rest = target.partition("://")
    if sep:
        suffix_at = len(rest)
        for delimiter in "/?#":
            found = rest.find(delimiter)
            if found >= 0:
                suffix_at = min(suffix_at, found)
        authority, suffix = rest[:suffix_at], rest[suffix_at:]
        public_suffix_at = len(suffix)
        for delimiter in "?#":
            found = suffix.find(delimiter)
            if found >= 0:
                public_suffix_at = min(public_suffix_at, found)
        public_suffix = suffix[:public_suffix_at]
        userinfo, at, hostport = authority.rpartition("@")
        if not at:
            return f"{scheme}://{authority}{public_suffix}"
        if scheme.lower() in {"ssh", "git+ssh"}:
            username, password_sep, _password = userinfo.partition(":")
            safe_authority = f"{username}@{hostport}" if username else hostport
            # A username-only SSH authority is already safe and must remain
            # byte-for-byte stable; this also keeps callers from classifying it
            # as a credentialed transport merely because it contains ``@``.
            if not password_sep:
                safe_authority = authority
        else:
            safe_authority = hostport
        return f"{scheme}://{safe_authority}{public_suffix}"

    # SCP's standard ``user@host:path`` form carries a routing username, not a
    # password, and must remain byte-for-byte stable. A password-like
    # ``user:password@host:path`` is not a valid SCP credential mechanism: Git
    # treats the first colon as host/path, so executable/governance callers must
    # reject it via ``_git_target_is_unsupported``. This helper still redacts the
    # credential-like segment for metadata-only registration and API projection.
    # Bare/local paths stay untouched unless the suffix is SCP-shaped.
    at_index = target.rfind("@")
    if at_index <= 0:
        return target
    userinfo = target[:at_index]
    username, password_sep, _password = userinfo.partition(":")
    if not password_sep:
        return target

    authority_and_path = target[at_index + 1 :]
    colon = authority_and_path.find(":")
    if not authority_and_path.startswith("[") and colon <= 0:
        # No SCP-shaped ``host:path`` suffix: preserve a local filename that
        # merely contains both ':' and '@'.
        return target
    safe_username = (
        username
        if username and all(ch.isalnum() or ch in "._-" for ch in username)
        else ""
    )
    prefix = f"{safe_username}@" if safe_username else ""
    return f"{prefix}{authority_and_path}"


def _git_target_has_query_or_fragment(url: str) -> bool:
    """Whether a clone target has a suffix that cannot be a safe identity."""
    return "?" in (url or "") or "#" in (url or "")


def _git_target_is_unsupported(url: str) -> bool:
    """Whether *url* cannot safely serve as both consent and Git identity.

    A query may select different server-side content, so stripping it from the
    consent identity while still using it for transport would reintroduce a
    repository-rebinding gap. A colon-before-@ SCP target has the same problem:
    Git treats the colon as host/path, not as a password separator. In SSH URI
    userinfo, Git passes the whole colon-bearing string as the OpenSSH username;
    it is likewise not a password that can be removed without changing routing.
    Registry clone and governance paths reject these forms before trust or fetch;
    metadata-only paths may still sanitize them to prevent credential-like text
    from entering persistence and APIs.
    """
    return (
        _git_target_has_query_or_fragment(url)
        or _git_target_has_ambiguous_scp_prefix(url)
        or _git_target_has_ambiguous_ssh_userinfo(url)
    )


def _loggable_git_transport_output(text: str, *, credentialed: bool) -> str:
    """Return git output that is safe to publish or log.

    A credential-bearing URL is expanded inside git from the one-shot transport
    environment, and git is allowed to echo that expanded URL on failure. In that
    posture we never try to scrub and forward free text: doing so would keep the
    raw credential in the logging dataflow and make correctness depend on an
    exhaustive replacement grammar. Return only fixed classifications instead.

    Non-credentialed transports retain the historical output because there is no
    embedded userinfo capability for git to echo.
    """

    if not credentialed or not text:
        return text
    if _git_output_is_auth_shaped(text):
        return "git authentication failed (credentialed transport details redacted)"
    failure_class = _redacted_git_failure_class(text)
    if failure_class:
        return f"git transport failed: {failure_class} (details redacted)"
    return "git transport output redacted (credentialed remote)"


def _git_transport_env(
    credential_target: str, safe_target: str, env: dict[str, str]
) -> dict[str, str]:
    """Return the one-shot environment for an already-sanitized git command.

    Embedded HTTP/SSH userinfo may be needed for the network request, but handing
    that URL directly to ``git clone`` also persists it as ``remote.origin.url``.
    Callers therefore build and sandbox argv with *safe_target* only. This helper
    receives the credential-bearing transport target separately and returns only
    an environment -- never an argv element or repository identity.

    The mapping is passed through ``GIT_CONFIG_*`` instead of ``git -c`` argv.
    Besides keeping process listings credential-free, this is a security boundary:
    sandbox diagnostics retain and log argv, so raw userinfo must never enter that
    generic API. The returned environment is a copy so callers can create it only
    after :func:`wrap_argv` and give it to the exact clone/fetch/pull subprocess;
    local setup and checkout commands keep the credential-free base environment.

    Git may execute repository- or operator-configured hooks, fsmonitor commands,
    and credential helpers inside that same subprocess. Those children inherit
    the command environment, including the one-shot URL rewrite. Append fixed
    neutralizers after every inherited entry so none of those extension points
    can receive the embedded credential while this command is in flight.
    """

    if _git_target_is_unsupported(credential_target):
        raise ValueError(
            "git clone target contains an unsupported query or fragment or an "
            "ambiguous Git transport identity"
        )
    if _strip_git_target_userinfo(credential_target) != safe_target:
        raise ValueError("git credential target does not match the safe clone target")
    if not credential_target or credential_target == safe_target:
        return env
    scheme = credential_target.partition("://")[0].lower()
    if scheme not in {"http", "https"}:
        # Only Git's built-in HTTP transport has a supported embedded-userinfo
        # credential contract here. Arbitrary schemes can dispatch a configured
        # remote helper or proxy; giving those processes the raw rewrite would
        # turn an unclassified extension point into a credential recipient.
        raise ValueError("embedded git credentials require an HTTP(S) target")

    transport_env = dict(env)
    try:
        config_count = int(transport_env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        # ``git`` would reject a malformed count too. Resetting the command-local
        # sequence is the useful fail-safe here: the caller's input mapping is not
        # mutated, and no inherited entry can displace the credential rewrite.
        config_count = 0
    if config_count < 0:
        config_count = 0
    command_config = (
        (f"url.{credential_target}.insteadOf", safe_target),
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
        ("core.askPass", ""),
        # Keep hooksPath last: within the command-scope config it must win over
        # a duplicate inherited entry as well as repository/global config.
        ("core.hooksPath", os.devnull),
    )
    for key, value in command_config:
        transport_env[f"GIT_CONFIG_KEY_{config_count}"] = key
        transport_env[f"GIT_CONFIG_VALUE_{config_count}"] = value
        config_count += 1
    transport_env["GIT_CONFIG_COUNT"] = str(config_count)
    return transport_env


def _normalize_git_target(url: str) -> str:
    """Canonical form used whenever a security decision compares clone URLs.

    Cosmetic variance between the separately-authored seed and catalog is a trailing
    ``/``, a trailing ``.git``, and the case of the scheme and host -- those three
    are normalised.

    **The PATH keeps its case.** Repository paths are case-sensitive on plenty of
    forges, so folding them makes two DIFFERENT repositories compare equal, and this
    predicate is what decides whether a catalog row may stand in for a bundled app.
    App trust is keyed by name, so a false "same target" here is the name-rebinding
    that requiring URL equality exists to prevent.
    """

    normalized = _strip_git_target_userinfo(url).rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    scheme, sep, rest = normalized.partition("://")
    if not sep:
        # No scheme to split on (scp-style or a bare path): fold nothing, since
        # the host cannot be told from the path without guessing.
        return normalized

    # Split the authority without parsing/re-serialising the rest of the URL.
    # ``urlsplit`` exposes convenient hostname/port properties, but rebuilding
    # from decoded components changes exact spelling. Only the URI scheme and
    # HOSTNAME are case-insensitive. The port and path remain byte-for-byte;
    # Query/fragment suffixes were removed above (and registry clone paths reject
    # them before transport). HTTP credentials and colon-bearing SSH userinfo were
    # removed; executable paths reject the latter because it changes routing. A
    # username-only SSH authority remains because dropping it changes which
    # endpoint Git actually invokes.
    suffix_at = len(rest)
    for delimiter in "/?#":
        found = rest.find(delimiter)
        if found >= 0:
            suffix_at = min(suffix_at, found)
    authority, suffix = rest[:suffix_at], rest[suffix_at:]

    username = ""
    hostport = authority
    userinfo, at, candidate_hostport = authority.rpartition("@")
    if at:
        username = f"{userinfo}@"
        hostport = candidate_hostport
    if hostport.startswith("["):
        # RFC URI IPv6 literals are bracketed. Preserve brackets and the exact
        # non-default port spelling while folding only the literal hostname.
        close = hostport.find("]")
        if close >= 0:
            hostport = f"[{hostport[1:close].lower()}]{hostport[close + 1:]}"
    elif hostport.count(":") <= 1:
        # Zero/one colon is an ordinary hostname with an optional port. More
        # than one is malformed/unbracketed IPv6; do not guess where its host
        # ends because a false equivalence here rebinds an execution grant.
        hostname, colon, port = hostport.rpartition(":")
        if colon:
            hostport = f"{hostname.lower()}:{port}"
        else:
            hostport = hostport.lower()

    return f"{scheme.lower()}://{username}{hostport}{suffix}"


def _same_git_target(a: str, b: str) -> bool:
    """Whether two clone URLs name the same repository."""

    if _git_target_is_unsupported(a) or _git_target_is_unsupported(b):
        return False
    return bool(a) and _normalize_git_target(a) == _normalize_git_target(b)


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
    owner_target = await asyncio.to_thread(_owner_designated_repo_target, entry)
    manifest = await _fetch_app_manifest(
        repo,
        branch,
        subdirectory,
        app_name=name,
        git_url=owner_target or git_url,
        owner_designated=bool(owner_target),
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
        # GitHub star count baked into the row by the publisher (git-type
        # third-party apps only). Reader: the frontend App Store list/detail
        # display. Display-only — ``_apply_trust_fields`` sanitizes it to a
        # non-negative int on EVERY row (the allowlist is not the only exit:
        # a failed manifest fetch passes the row through unchanged).
        "stargazersCount",
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
    raw_repo = entry.get("repo", "")
    repo = _strip_git_target_userinfo(raw_repo) if isinstance(raw_repo, str) else ""
    result = {k: v for k, v in entry.items() if k in _REGISTRY_ROW_KEYS}
    if isinstance(result.get("repo"), str):
        result["repo"] = _strip_git_target_userinfo(result["repo"])

    # Top-level display fields from app.json
    for key in (
        "displayName",
        "description",
        "version",
        "author",
        "tags",
        "highlights",
        "useCases",
        "configuration",
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


def _apply_trust_fields(
    entries: list[dict[str, Any]],
    *,
    trust_repositories: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Stamp server-computed trust fields on every row.

    SECURITY CONTRACT: these fields are the API trust boundary for
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
    - ``trustRepository``: the normalized clone target the server resolved for
      the app. It is OVERWRITTEN here, never copied from index or manifest
      content, because the consent modal sends it back as proof of what the
      operator reviewed. ``trust_repositories`` lets the catalog storefront
      supply coordinates resolved separately from its display-only rows.
    """
    for entry in entries:
        entry.pop("trustRepository", None)
        name = entry.get("name")
        if trust_repositories is None:
            trust_candidate = _entry_git_url(entry)
        elif isinstance(name, str):
            trust_candidate = trust_repositories.get(name, "")
        else:
            trust_candidate = ""
        # A semantic query cannot be removed from the consent identity while
        # remaining on the eventual Git transport. Do not mint a grant proof for
        # a target the install path must refuse.
        trust_repository = (
            ""
            if _git_target_is_unsupported(trust_candidate)
            else _normalize_git_target(trust_candidate)
        )
        if trust_repository:
            entry["trustRepository"] = trust_repository

        # These coordinates are display/provenance fields on the storefront
        # response. Installation resolves its own authoritative row again; the
        # browser neither needs nor may receive embedded clone credentials.
        for coordinate_key in ("gitUrl", "repo", "sourceUrl"):
            coordinate = entry.get(coordinate_key)
            if isinstance(coordinate, str):
                entry[coordinate_key] = _strip_git_target_userinfo(coordinate)

        registry_name = entry.get("_registry")
        if isinstance(registry_name, str):
            entry["_registry"] = _strip_git_target_userinfo(registry_name)

        # ``stargazersCount`` is a trust cue, so it follows the ``featured``
        # precedent below: only the publisher's bake step may mint it, and an
        # external index can never self-report one (a fabricated ``★ 50K`` on
        # a hostile git app would render identically to a signed-catalog
        # count — a false trust cue is worse than none). The shape check
        # still runs on EVERY row because this function is the only boundary
        # every row crosses (``_resolve_manifest`` returns the row unchanged
        # when the manifest fetch fails, so the allowlist projection is not a
        # guaranteed exit). ``bool`` is excluded (it IS an int subclass), and
        # the JS safe-integer bound is a layout guard: Python accepts a
        # 309-digit int that JavaScript renders as hundreds of digits.
        stars = entry.get("stargazersCount")
        if (
            not isinstance(stars, int)
            or isinstance(stars, bool)
            or stars < 0
            or stars > official_catalog._STARS_MAX
        ):
            entry.pop("stargazersCount", None)

        index_author = entry.pop("_index_author", None)
        folded_author = _fold_author(index_author)
        if entry.get("_registry"):
            entry["provenance"] = "external"
            entry["verified"] = False
            entry.pop("featured", None)
            entry.pop("stargazersCount", None)
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


def _trust_repository_bindings(
    entries: list[dict[str, Any]],
    installed_map: dict[str, dict[str, Any]],
    resolved: dict[str, str] | None = None,
) -> dict[str, str]:
    """Authoritative consent target for each storefront row.

    An installed app is bound to the source URL recorded at install time; that
    is also what the grant handler resolves first. A not-yet-installed catalog
    row may have display and install coordinates from separate documents, so its
    caller supplies the freshly resolved target in *resolved*. Every remaining
    row (seed and external registries) resolves through ``_entry_git_url`` so a
    legitimate ``gitUrl``/``repo`` difference follows the same precedence as
    clone/install rather than treating the display alias as authority.
    """
    bindings: dict[str, str] = {}
    resolved = resolved or {}
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        installed = installed_map.get(name)
        if installed is not None:
            # The listing row was resolved from the same authoritative source
            # as install. Supply it to the shared legacy fallback so storefront
            # proof and the grant handler cannot disagree, without performing a
            # second blocking catalog lookup on the event loop.
            coordinate = resolved.get(name)
            authoritative_entry = (
                {"gitUrl": coordinate} if coordinate is not None else entry
            )
            _, bindings[name] = resolve_installed_trust_repository(
                installed, registry_entry=authoritative_entry
            )
        elif name in resolved:
            bindings[name] = resolved[name]
        else:
            bindings[name] = _entry_git_url(entry)
    return bindings


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


def _credential_free_external_registry_value(value: Any) -> Any:
    """Recursively sanitize URI-shaped strings before a row is retained."""
    if isinstance(value, str):
        candidate = value.strip()
        if "://" in candidate:
            return _strip_git_target_userinfo(candidate)
        return value
    if isinstance(value, list):
        return [_credential_free_external_registry_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _credential_free_external_registry_value(item) for key, item in value.items()}
    return value


def _credential_free_external_registry_entries(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [_credential_free_external_registry_value(entry) for entry in entries]


def _external_registry_cache_path_for_identity(name: str) -> Path:

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


def _external_registry_cache_path(name: str) -> Path:
    safe_name = _credential_free_external_registry_value(name)
    return _external_registry_cache_path_for_identity(safe_name)


def _legacy_external_registry_cache_path(name: str) -> Path:
    """The pre-hardening path, used only to remove an exact legacy artifact."""
    return _external_registry_cache_path_for_identity(name)


def _remove_legacy_credential_registry_cache(name: str) -> None:
    """Best-effort removal of a cache whose old filename exposed URL userinfo."""
    legacy_path = _legacy_external_registry_cache_path(name)
    safe_path = _external_registry_cache_path(name)
    if legacy_path == safe_path:
        return
    try:
        legacy_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to remove a legacy credential-bearing registry cache")


def _read_external_registry_cache(
    name: str,
    *,
    ignore_ttl: bool = False,
) -> list[dict[str, Any]] | None:
    """Read cached external registry entries. Returns None if missing or stale.

    When *ignore_ttl* is True, returns data regardless of age — used by
    synchronous callers that cannot refresh the cache themselves.
    """
    _remove_legacy_credential_registry_cache(name)
    path = _external_registry_cache_path(name)
    if not path.is_file():
        return None
    try:
        original_stat = path.stat()
        is_stale = time.time() - original_stat.st_mtime > _EXTERNAL_REGISTRY_CACHE_TTL
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return None
        sanitized_data = _credential_free_external_registry_value(data)
        if sanitized_data != data:
            # Named registries keep the same cache path across this migration.
            # Rewrite their legacy payload in place so the secret is not merely
            # hidden from this read while remaining durable on disk. Preserve
            # the original timestamps so cleaning an expired cache cannot make
            # it appear fresh and bypass the TTL-driven network refresh.
            try:
                atomic_write(path, json.dumps(sanitized_data, indent=2) + "\n")
                os.utime(
                    path,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
            except OSError:
                # If migration cannot be completed, remove the old artifact
                # rather than leave credential-bearing JSON durable on disk.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove a credential-bearing legacy registry cache")
        data = sanitized_data
        if is_stale and not ignore_ttl:
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
                    "Dropping cached external registry entry with invalid name "
                    "(must be lowercase kebab-case)"
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
                    "Dropping cached external registry entry with unsafe subdirectory "
                    "(must be a contained relative path)"
                )
                continue
            safe.append(entry)
        return safe
    except (json.JSONDecodeError, OSError):
        return None


def _write_external_registry_cache(name: str, entries: list[dict[str, Any]]) -> None:
    """Write external registry entries to cache."""
    _remove_legacy_credential_registry_cache(name)
    _manifest_cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(
            _external_registry_cache_path(name),
            json.dumps(_credential_free_external_registry_entries(entries), indent=2) + "\n",
        )
    except OSError:
        logger.warning("Failed to cache external registry")


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
    un-reaped. The reap itself goes through the shared
    ``platform_compat.kill_and_reap``, which drains the pipes via
    ``communicate()`` under a bound so a killed child blocked writing into a
    full pipe cannot hang the caller.
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await platform_compat.kill_and_reap(proc)
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
        logger.warning("Rejecting non-cloneable external registry repo")
        return None
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-./]*$", branch) or ".." in branch:
        logger.warning("Rejecting invalid branch name: %r", branch)
        return None

    if _git_target_is_unsupported(repo):
        logger.warning("external registry fetch refused an unsupported clone target")
        return None
    credential_target = repo
    git_url = _strip_git_target_userinfo(credential_target)
    credentialed_transport = credential_target != git_url

    # SEL audit: log external subprocess invocation for traceability (best-effort).
    def _sel_outcome(outcome: str) -> None:
        if _sel_fn is None:
            return
        try:
            _sel_fn().log_api_access(
                caller="registry",
                operation="fetch_external_registry",
                outcome=outcome,
                resources=(f"repo={_strip_git_target_userinfo(repo)} branch={branch}"),
            )
        except Exception as exc:
            logger.debug("SEL audit log failed for fetch_external_registry: %s", exc)

    _sel_outcome("started")

    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-registry-")
        clone_path = Path(tmp_root)
        if credentialed_transport:
            # See `_git_fetch_branch`: a combined credentialed clone would let
            # checkout-time filters inherit the one-shot credential mapping.
            clone_path /= "branch"
            err = await _git_fetch_branch(
                git_url,
                branch,
                clone_path,
                [],
                credential_target=credential_target,
                clone_env=minimal_env(),
                sandbox_mode=_context_clone_sandbox_mode(git_url),
            )
            if err is not None:
                _sel_outcome("failed")
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
            sandboxed_cmd, _ = await wrap_argv_async(
                clone_cmd, mode=_context_clone_sandbox_mode(git_url), _prepare=wrap_argv
            )
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

        # Prefer an explicit app-registry.json index.
        index_path = clone_path / "app-registry.json"
        if index_path.is_file():
            try:
                data = json.loads(await asyncio.to_thread(index_path.read_text, "utf-8"))
                if isinstance(data, list):
                    # Keep only well-formed object entries — a malformed index
                    # item (e.g. a bare string) must never reach normalization.
                    _sel_outcome("success")
                    return _credential_free_external_registry_entries(
                        [item for item in data if isinstance(item, dict)]
                    )
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
        return _credential_free_external_registry_entries(result) if result else None

    except (asyncio.TimeoutError, OSError):
        logger.debug("Failed to fetch external registry")
        _sel_outcome("failed")
        return None
    finally:
        if tmp_root:
            await _rmtree_force_settled(tmp_root)


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
        if _same_git_target(_entry_git_url(entry), reg.repo):
            if warn and declared_branch is not None and declared_branch != reg.branch:
                logger.warning(
                    "External registry %s entry %r declares branch %r; using the "
                    "configured registry branch %r",
                    _public_registry_name(reg),
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
    # An unnamed legacy registry used its raw URL as the old cache identity,
    # which exposed HTTP userinfo in the filename. Remove that exact artifact
    # even when this is a fresh fetch with no preceding cache read.
    _remove_legacy_credential_registry_cache(reg.repo)
    public_registry_repo = _strip_git_target_userinfo(reg.repo)
    name = _public_registry_name(reg)
    entries = await _fetch_external_registry_index(reg.repo, reg.branch)
    if entries is None:
        return None
    # Defensively drop malformed (non-dict) index items before normalization:
    # a configured repo can return a valid JSON array containing a non-object
    # (e.g. ``["oops"]``), and ``entry.setdefault(...)`` on a str would raise
    # AttributeError — which, on the refresh path, escapes as an HTTP 500.
    entries = _credential_free_external_registry_entries(
        [e for e in entries if isinstance(e, dict)]
    )
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
        entry.setdefault("gitUrl", public_registry_repo)
        entry.setdefault("repo", public_registry_repo)
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
        cache_name = reg.name or reg.repo
        public_name = _public_registry_name(reg)

        # Try cache first
        cached = await asyncio.to_thread(_read_external_registry_cache, cache_name)
        if cached is not None:
            for entry in cached:
                entry["_registry"] = public_name
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
            cache_name,
            ignore_ttl=True,
        )
        if stale is not None:
            for entry in stale:
                entry["_registry"] = public_name
            _apply_configured_branch(stale, reg)
            return stale
        logger.warning(
            "Failed to load external registry %s from %s",
            public_name,
            _strip_git_target_userinfo(reg.repo),
        )
        return []

    results = await asyncio.gather(
        *[_load_one(reg) for reg in registries],
        return_exceptions=True,
    )
    for reg, result in zip(registries, results, strict=True):
        if isinstance(result, list):
            all_entries.extend(result)
        elif isinstance(result, Exception):
            logger.warning(
                "External registry load failed: %s",
                _loggable_git_transport_output(
                    str(result),
                    credentialed=_strip_git_target_userinfo(reg.repo) != reg.repo,
                ),
            )

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
        registries = [r for r in registries if _same_git_target(r.repo, repo)]
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
        display_name = _public_registry_name(reg)
        # Read the (possibly stale) prior index up front so we know which
        # per-app manifest caches this registry contributed, even if the
        # refetch changes/removes some entries.
        prior = await asyncio.to_thread(_read_external_registry_cache, name, ignore_ttl=True)
        # Fetch-then-swap: the cache is overwritten only on a successful fetch.
        entries = await _fetch_and_cache_external_registry(reg)
        if entries is None:
            failed.append(display_name)
            results.append({"name": display_name, "ok": False})
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
        refreshed.append(display_name)
        results.append({"name": display_name, "ok": True})

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
        denied = app_execution_denied(
            name,
            action="registry_detect_installed",
            caller="registry",
            repository=_entry_git_url(entry),
        )
        if denied:
            logger.debug("Skipping registry detectInstalled for %s: %s", name, denied)
            continue
        try:

            base_cmd = ["/bin/sh", "-c", detect_cmd]
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                base_cmd, mode="strict", _prepare=wrap_argv
            )
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

    trust_repositories = _trust_repository_bindings(entries, installed_map)
    return _apply_trust_fields(
        _enrich_with_install_status(entries, installed_map, detected),
        trust_repositories=trust_repositories,
    )


def _catalog_installable_rows() -> dict[str, dict[str, Any]]:
    """Catalog install rows by name, from a FRESH fetch -- never the cache.

    ``list_catalog_rows`` reads the cache under the data home, which is
    agent-writable. That is harmless while a cached row only re-dresses a row that
    exists anyway -- the posture ``annotate`` already documents -- and NOT harmless
    if a cached row could CREATE a listed row: a planted name would render with
    official provenance and deduplicate the real same-named external row out of the
    listing, so a consent prompt would describe an official app while the name grant
    it produces installs the external one.

    So the decision to LIST a catalog-only ``git`` name is authorised from the
    fetched document and never from the cache. ``fetch_inventory_entries`` is the
    only source allowed to materialise inventory, and it honours the module's
    failure memory, so an outage costs a refusal rather than a fresh timeout on
    every listing.

    Returns an empty mapping on ANY failure, which degrades the storefront to the
    seed's names -- the listing this path produced before the catalog could supply
    coordinates. Keeping the rows (rather than only their names) also lets the
    server show the exact resolved clone target in the consent dialog without a
    second network fetch.
    """
    try:
        entries = official_catalog.fetch_inventory_entries()
        return {
            row["name"]: row
            for row in official_catalog.inventory(entries)
            if isinstance(row.get("name"), str)
        }
    except Exception:  # noqa: BLE001 - degrade to the seed, never 500 the store
        logger.warning("cannot confirm the catalog's install coordinates", exc_info=True)
        return {}


async def list_catalog_apps() -> list[dict[str, Any]]:
    """Store rows built from the published catalog, enriched and trust-stamped.

    The JSON-only storefront path: when the published catalog is available its
    rows REPLACE the seed + per-app manifest fetch, so the store renders the
    published document's list and display copy. An empty result means the catalog
    was unavailable, and the caller falls back to ``list_registry`` offline.

    Install coordinates are the CATALOG's when it pins them: a ``git`` row is kept
    when the seed or an external registry names it, or when the catalog itself
    supplies validated pinned coordinates for it -- the same resolution
    ``inventory_for_install`` performs on the install path. Gating the listing on
    the seed alone made the two disagree, so a published app stayed invisible in
    the store until a release shipped a new seed -- the release-per-app cost
    ``inventory`` exists to remove. The row still carries no clone URL of its own;
    install resolves the coordinates by name. ``verified`` stays ``False`` for
    non-builtin rows until the catalog signature is checked, so this path never
    mints the first-party badge from a document trusted only as far as TLS.

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
    seed_by_name = {
        e["name"]: e
        for e in installable
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    }
    installable_names = set(seed_by_name)
    # Reserve every catalog name BEFORE the git filter, plus every seed name, so
    # an external row can never shadow a catalog/seed name — including a catalog
    # `git` row filtered out here for not being installable yet, whose name
    # install still resolves by.
    reserved_names: set[Any] = {row.get("name") for row in rows} | installable_names
    # A `git` row the seed does not name is STILL installable when the catalog
    # pins it -- that is exactly what `inventory_for_install` resolves on the
    # install path. Asking only the seed made the two resolvers disagree: install
    # accepted a catalog-only row while the storefront dropped it, so a published
    # app was unlistable, and therefore undiscoverable, until a release shipped a
    # new seed.
    #
    # Only paid when it can change the answer. With every `git` row already seeded
    # the fetch cannot unlock anything, so the storefront's hot path keeps costing
    # one cached read.
    fresh_installable: dict[str, dict[str, Any]] = {}
    if any(
        row.get("source", {}).get("type") == "git" and row.get("name") not in installable_names
        for row in rows
    ):
        fresh_installable = await asyncio.to_thread(_catalog_installable_rows)
        installable_names |= set(fresh_installable)
    rows = [
        row
        for row in rows
        if row.get("source", {}).get("type") != "git" or row.get("name") in installable_names
    ]

    # Catalog display rows intentionally carry no clone URL. Resolve the
    # consent target from the same install coordinates name-only install uses:
    # a bundled seed wins a different-repository catalog collision, while a
    # catalog-only row uses the freshly fetched pin row above.
    resolved_repositories: dict[str, str] = {}
    for row in rows:
        if row.get("source", {}).get("type") != "git":
            continue
        name = row.get("name")
        if not isinstance(name, str):
            continue
        install_row = seed_by_name.get(name) or fresh_installable.get(name)
        resolved_repositories[name] = (
            _entry_git_url(install_row) if install_row is not None else ""
        )

    installed = await asyncio.to_thread(list_installed_apps)
    installed_map = {a["name"]: a for a in installed}
    rows, detected = await _append_external_registry_apps(rows, reserved_names, installed_map)
    trust_repositories = _trust_repository_bindings(
        rows, installed_map, resolved_repositories
    )
    return _apply_trust_fields(
        _enrich_with_install_status(rows, installed_map, detected),
        trust_repositories=trust_repositories,
    )


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
    except official_catalog.CatalogUnavailable:
        catalog_failed = True
        # The caller may be classifying a config-derived grant name, and catalog
        # exceptions may include source coordinates.  Neither belongs in logs;
        # the fixed classification is enough to explain the fail-closed branch.
        logger.warning("official catalog coordinate lookup is unavailable")
    except Exception:  # noqa: BLE001 - fail closed: an unexpected error is not "no row"
        catalog_failed = True
        logger.warning("official catalog coordinate lookup failed unexpectedly")

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
            f"the requested app {detail}, but the official catalog could not be "
            "reached to confirm it — refusing to resolve it from another source. "
            "Retry when the catalog is reachable."
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


def resolve_installed_trust_repository(
    app: dict[str, Any],
    *,
    registry_entry: dict[str, Any] | None = None,
    allow_registry_lookup: bool = True,
) -> tuple[bool, str]:
    """Resolve the repository an installed app's trust prompt must bind to.

    New registry installs persist ``sourceUrl`` and are bound directly to that
    immutable install provenance.  Older installs predate that field, but retain
    the ``registry:<name>`` source marker; for those records, resolve the same
    current authoritative row a legacy update would use.  A genuinely local or
    self-registered app has neither form of registry provenance and remains a
    valid repository-less app.

    The boolean distinguishes that legitimate local case from a legacy registry
    record whose current source cannot be resolved.  Callers that grant execution
    trust must refuse the latter rather than silently creating a name-only grant.
    A caller that already resolved an authoritative storefront row can pass it as
    *registry_entry*, avoiding duplicate blocking catalog I/O while keeping the
    same coordinate precedence. ``CatalogUnavailable`` intentionally propagates
    when this function must perform the lookup itself, so a caller can fail closed.

    Runtime admission passes ``allow_registry_lookup=False``.  A legacy registry
    record without durable ``sourceUrl`` provenance is then unresolved instead of
    synchronously consulting the catalog from a request/startup event loop.  The
    storefront/grant path remains the only caller that may perform that blocking
    migration lookup, and already offloads it.
    """
    source_url = app.get("sourceUrl", "")
    source_url = source_url if isinstance(source_url, str) else ""
    if _git_target_is_unsupported(source_url):
        return False, ""
    repository = _normalize_git_target(source_url)
    if repository:
        return True, repository

    source = app.get("source", "")
    if not isinstance(source, str) or not is_registry_source(source):
        return True, ""

    name = app.get("name", "")
    if not isinstance(name, str) or not name:
        return False, ""
    if registry_entry is None and not allow_registry_lookup:
        return False, ""
    entry = registry_entry if registry_entry is not None else get_registry_app(name)
    if entry is None:
        return False, ""
    entry_url = _entry_git_url(entry)
    if _git_target_is_unsupported(entry_url):
        return False, ""
    repository = _normalize_git_target(entry_url)
    return bool(repository), repository


def _external_registry_row(name: str) -> dict[str, Any] | None:
    """The first row named *name* from an owner-configured external registry cache.

    Separate from the catalog/seed resolution above because these rows are a
    different trust class: they carry ``_registry``, which flips provenance and is
    attached here at the lookup boundary so a stale cache cannot omit it.
    """
    for reg in _effective_registries():
        cache_name = reg.name or reg.repo
        public_name = _public_registry_name(reg)
        cached = _read_external_registry_cache(cache_name, ignore_ttl=True)
        if cached:
            for entry in cached:
                if entry.get("name") == name:
                    # Repair a stale cache's branch before install reads it.
                    _apply_configured_branch([entry], reg)
                    # Old cache files may predate persisted origin tags. Restore
                    # the authoritative discriminator at the lookup boundary so
                    # privacy gates never mistake a custom source for official.
                    return {**entry, "_registry": public_name}
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
        # Credentials select how git fetches the source; they are not the source
        # identity. Rotation from one userinfo value to another must neither
        # rebind the grant nor strand an update of the same repository.
        if not _same_git_target(_entry_git_url(entry), want_url):
            continue
        current_registry = str(entry.get("_registry", "") or "")
        if current_registry != want_registry and not _same_git_target(
            current_registry, want_registry
        ):
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
        # ``pinned_url`` can contain clone credentials. It is comparison state,
        # never API/audit text: the caller returns and SEL-logs this reason.
        return None, (
            f"app {name!r} has no registry entry that matches its recorded source "
            "— refusing to update it from a different source"
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
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("repo"), str)
                    and _same_git_target(entry["repo"], repo)
                ):
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
        if isinstance(entry.get("repo"), str) and _same_git_target(entry["repo"], repo):
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
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("repo"), str)
                    and entry["repo"]
                ):
                    repos.add(_strip_git_target_userinfo(entry["repo"]))
    except Exception:  # fail open: the allowlist must never break blob serving
        logger.debug("_external_registry_repos: read failed", exc_info=True)
    return repos


def known_registry_repos() -> set[str]:
    """Repo names trusted by the ``/api/apps/blob`` SSRF gate.

    Union of the bundled registry and the user's external (federated)
    registries — external-registry apps resolve an ``/api/apps/blob`` iconUrl,
    so their repos must be allowlisted here or the App Store icon 403s.
    """
    bundled = {
        _strip_git_target_userinfo(e["repo"])
        for e in _load_registry_file()
        if isinstance(e.get("repo"), str) and e["repo"]
    }
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
    # All three reads below are BOUNDED: HEAD, the loose ref, and packed-refs
    # all live inside the checkout, so their sizes are attacker-controlled (an
    # app's build script can rewrite them). This is the SAME `.git/HEAD` file
    # that :func:`_read_clone_branch` bounds — closing the memory-exhaustion
    # class at that call site alone would leave it open here, on the install
    # path, which is the round-11 "next call site" lesson.
    raw_head = _read_git_metadata_bounded(git_dir / "HEAD", _HEAD_READ_LIMIT)
    if raw_head is None:
        return ""
    head = raw_head.strip()
    if not head.startswith("ref:"):
        # Detached HEAD holds the SHA directly.
        return head if _COMMIT_SHA_RE.match(head) else ""
    ref = head[len("ref:") :].strip()
    # git writes this file, not the cloned repo — belt-and-braces so a ref can
    # never be read as a path outside the clone's own .git directory.
    if not ref or ref.startswith("/") or ".." in ref.split("/"):
        return ""
    raw_loose = _read_git_metadata_bounded(git_dir / ref, _HEAD_READ_LIMIT)
    if raw_loose is not None:
        loose = raw_loose.strip()
        if _COMMIT_SHA_RE.match(loose):
            return loose
    # A repacked clone keeps no loose ref file.
    packed = _read_git_metadata_bounded(git_dir / "packed-refs", _PACKED_REFS_READ_LIMIT)
    if packed is not None:
        for line in packed.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and _COMMIT_SHA_RE.match(parts[0]):
                return parts[0]
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
            resources=(
                f"name={entry_name!r} declared={declared!r} "
                f"repo={_strip_git_target_userinfo(repo)}"
            ),
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
    origin_cmd, _cleanup = await wrap_argv_async(
        ["git", "remote", "get-url", "origin"],
        mode="strict",  # credential-free read; ~/.ssh stays hidden
        _prepare=wrap_argv,
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
    """Whether *dest* is a checkout of the same repository as *git_url*.

    Fails closed: an unreadable origin, a missing remote, or an empty
    *git_url* to compare against all return False. Embedded userinfo is transport
    authentication rather than identity, so credential rotation remains a match.
    """
    if not git_url:
        return False
    origin = await _clone_origin_url(dest)
    return origin is not None and _same_git_target(origin, git_url)


# Upper bound on any single git-metadata read of a file INSIDE a checkout
# (``.git/HEAD``, a loose ref). Those files are agent-writable — an app's own
# build script can rewrite them — so their size is ATTACKER-controlled: an app
# can replace one with (or symlink it to) a multi-gigabyte / sparse file, and
# an unbounded read would load it into gateway memory. A well-formed value here
# is a single line (a ``ref:`` line or a 40/64-char SHA), tens of bytes, so a
# few-hundred-byte cap makes the read a no-op for hostile content while a real
# ref fits comfortably. This bound is applied at EVERY checkout-resident git
# read, not just one call site — the round-11 lesson is that gating a single
# caller leaves the primitive exploitable from the next one.
_HEAD_READ_LIMIT = 512  # bytes

# Upper bound on the ``.git/packed-refs`` read. It is line-oriented (one ref per
# line) so it can legitimately be larger than a single ref file, but it is still
# agent-writable checkout content, so the read is capped rather than unbounded.
# A shallow single-branch app clone packs a handful of refs; this ceiling covers
# a realistic repo while still refusing a hostile multi-megabyte replacement.
_PACKED_REFS_READ_LIMIT = 1 << 20  # 1 MiB


def _read_git_metadata_bounded(path: Path, limit: int) -> str | None:
    """Read at most *limit* bytes of a git-metadata file, or None on failure.

    The read is BOUNDED because *path* lives inside a checkout whose contents an
    app's build script can rewrite (see :data:`_HEAD_READ_LIMIT`): reading a
    ref file whole would let an oversized/sparse/symlinked replacement exhaust
    gateway memory. Content that exactly fills the bound is treated as
    truncated/hostile and returns None, so a caller never acts on a partial
    token. Missing file, unreadable, or non-UTF-8 all fail closed to None.

    The read is also SYMLINK-CONTAINED: *path* is a checkout-resident file whose
    name (``.git/HEAD``, a loose ref, ``packed-refs``) an app's build script can
    replace with a symlink pointing at a protected file (``~/.aws/credentials``,
    an SSH key). A bare ``open()`` would follow that link and read the target
    through the sensitive-path ceiling, so the read is routed through
    :func:`kiro_crew.hooks.safe_read_prefix`, which canonicalizes via ``realpath``
    and refuses a resolved target ``is_sensitive_path`` flags before any read,
    then opens the canonical path ``O_NOFOLLOW`` as TOCTOU defense against a
    final-component symlink swap. A rejected (or unreadable) path fails closed to
    None, so the size bound and the containment gate share one fail-closed exit.
    """
    from kiro_crew import hooks

    raw = hooks.safe_read_prefix(str(path), limit)
    if raw is None:
        # Rejected by the sensitive-path gate, a followed symlink refused
        # O_NOFOLLOW, missing, or otherwise unreadable — all fail closed.
        return None
    try:
        data = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Filling the bound means the real content was larger — refuse rather than
    # act on a value that may have been cut mid-token. Measured on the decoded
    # text so a multi-byte tail cannot slip a value past the bound.
    if len(data) >= limit:
        return None
    return data


def _read_clone_branch(clone_dir: Path) -> str | None:
    """Read the current branch of an existing git clone.

    Returns the branch name (e.g. ``"main"``), or None if the clone does not
    exist, is in detached HEAD state, or the branch cannot be determined.
    Reads ``.git/HEAD`` directly (stdlib-only, no subprocess spawn) — mirrors
    the fail-closed posture of :func:`_clone_origin_matches`.

    The read is BOUNDED to :data:`_HEAD_READ_LIMIT` bytes. ``.git/HEAD`` lives
    inside a checkout an app's build script can rewrite, so its size is
    attacker-controlled; reading it whole would let a multi-gigabyte or sparse
    replacement exhaust gateway memory. A well-formed HEAD fits in a few
    hundred bytes, so a ``ref:`` line that does not resolve within the bound is
    treated as malformed and fails closed (returns None). This is what closes
    the memory-exhaustion class at EVERY call site, not just the ones a caller
    happens to gate.

    A ``.git`` that is a *file* (worktree / submodule gitfile) rather than a
    directory also fails closed (``is_file()`` on the nested path returns
    False), so no fast path is attempted for those layouts.
    """
    head_file = clone_dir / ".git" / "HEAD"
    if not head_file.is_file():
        return None
    raw = _read_git_metadata_bounded(head_file, _HEAD_READ_LIMIT)
    if raw is None:
        # Missing, unreadable, non-UTF-8, or oversized (filled the bound) — the
        # bounded reader already failed closed on hostile/truncated content.
        return None
    head_content = raw.strip()
    # A normal branch checkout has HEAD = "ref: refs/heads/<branch>"
    _REF_PREFIX = "ref: refs/heads/"
    if head_content.startswith(_REF_PREFIX):
        branch = head_content[len(_REF_PREFIX) :]
        if not branch:
            return None
        return branch
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
        await platform_compat.kill_and_reap(proc)


async def _git_fetch_ref(
    git_url: str,
    ref: str,
    dest: Path,
    log_lines: list[str],
    *,
    checkout_branch: str = "",
    credential_target: str | None = None,
    clone_env: dict[str, str],
    sandbox_mode: str,
) -> dict[str, Any] | None:
    """Materialise *dest* from one remote ref. Returns None on success.

    ``git clone --branch`` cannot take a commit id -- it exits 128 with
    ``Remote branch <sha> not found in upstream origin`` -- so a pinned entry
    needs fetch-by-SHA instead. The published catalog pins every third-party app
    to a commit, and this is the only path that honours that pin.

    Only the fetch invocation receives the one-shot credential rewrite. Init,
    remote setup and checkout run with *clone_env*: a remote tree can select an
    inherited filter driver through ``.gitattributes``, and an existing checkout
    can select hooks or other executable config. Keeping the credential out of
    every worktree operation is therefore the security boundary, not merely a
    subprocess-configuration detail.

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
    transport_target = credential_target or git_url
    if _git_target_is_unsupported(transport_target):
        return {
            "ok": False,
            "error": (
                "git clone target contains an unsupported query or fragment or an "
                "ambiguous Git transport identity"
            ),
        }

    if _strip_git_target_userinfo(git_url) != git_url:
        raise ValueError("_git_fetch_ref requires a credential-free git_url")
    credential_target = credential_target or git_url
    if _strip_git_target_userinfo(credential_target) != git_url:
        raise ValueError("credential_target does not match git_url")
    credentialed_transport = credential_target != git_url
    if credentialed_transport and credential_target.partition("://")[0].lower() not in {
        "http",
        "https",
    }:
        return {
            "ok": False,
            "name": dest.name,
            "error": "embedded git credentials require an HTTP(S) target",
        }

    async def run(
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: int,
        network: bool = False,
    ) -> tuple[int, str]:
        sandboxed, _cleanup = await wrap_argv_async(
            argv, mode=sandbox_mode, _prepare=wrap_argv
        )
        sandboxed = cgroup_scope_argv(sandboxed)
        process_env = (
            _git_transport_env(credential_target, git_url, clone_env) if network else clone_env
        )
        proc = await create_subprocess_limited(
            *sandboxed,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=process_env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            return 124, "timed out"
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            raise
        return (
            proc.returncode or 0,
            _loggable_git_transport_output(
                out.decode(errors="replace").strip(),
                credentialed=credentialed_transport if network else False,
            ),
        )

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
                # Human sentence in `error`, machine slug in `code`: the install
                # banner renders `result.error`, never `result.message`.
                "code": "destination_not_a_checkout",
                "error": (
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
            code, out = await run(["git", "init", "--quiet", "--template=", str(dest)], timeout=15)
            if code != 0:
                log_lines.append(f"git init failed (exit {code}): {out}")
                return {"ok": False, "name": dest.name, "error": "git init failed"}
            code, out = await run(["git", "remote", "add", "origin", git_url], cwd=dest, timeout=15)
            if code != 0:
                log_lines.append(f"git remote add failed (exit {code}): {out}")
                return {"ok": False, "name": dest.name, "error": "git remote add failed"}

        refspec = (
            f"+refs/heads/{checkout_branch}:refs/remotes/origin/{checkout_branch}"
            if checkout_branch
            else ref
        )
        description = f"branch {checkout_branch}" if checkout_branch else f"commit {ref[:12]}"
        log_lines.append(f"Fetching {_strip_git_target_userinfo(git_url)} at {description}...")
        code, out = await run(
            [
                "git",
                *hardening,
                "fetch",
                "--no-auto-maintenance",
                "--no-tags",
                "--depth",
                "1",
                git_url,
                refspec,
            ],
            cwd=dest,
            timeout=_CLONE_TIMEOUT,
            network=True,
        )
        log_lines.append(out)
        if code != 0:
            # Fail closed. A server that will not serve this object (unreachable
            # commit, or one no ref contains) must not degrade into "install the
            # default branch instead" -- that is the pin silently not applying.
            return {
                "ok": False,
                "name": dest.name,
                "error": f"git fetch failed (exit {code})",
            }

        checkout_cmd = (
            ["git", "checkout", "--quiet", "-B", checkout_branch, f"origin/{checkout_branch}"]
            if checkout_branch
            else ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"]
        )
        code, out = await run(checkout_cmd, cwd=dest, timeout=15)
        if code != 0:
            log_lines.append(f"git checkout failed (exit {code}): {out}")
            return {
                "ok": False,
                "name": dest.name,
                "error": "git checkout of fetched tree failed",
            }

        if checkout_branch:
            code, out = await run(
                [
                    "git",
                    "branch",
                    "--set-upstream-to",
                    f"origin/{checkout_branch}",
                    checkout_branch,
                ],
                cwd=dest,
                timeout=15,
            )
            if code != 0:
                log_lines.append(f"git branch tracking setup failed (exit {code}): {out}")
                return {
                    "ok": False,
                    "name": dest.name,
                    "error": "git branch tracking setup failed",
                }
            succeeded = True
            return None

        # The pin only becomes real here. `_resolved_clone_commit` degrades to "" on
        # any read failure, so an empty answer is a FAILURE rather than a pass: the
        # whole point is refusing to build a tree we cannot identify.
        landed = await asyncio.to_thread(_resolved_clone_commit, dest)
        if landed != ref:
            log_lines.append(
                f"pinned commit not honoured: asked for {ref}, checkout reports "
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
            await _rmtree_force_settled(dest)


async def _git_fetch_commit(
    git_url: str,
    commit: str,
    dest: Path,
    log_lines: list[str],
    *,
    credential_target: str | None = None,
    clone_env: dict[str, str],
    sandbox_mode: str,
) -> dict[str, Any] | None:
    """Materialise *dest* at exactly *commit*. Returns None on success."""
    return await _git_fetch_ref(
        git_url,
        commit,
        dest,
        log_lines,
        credential_target=credential_target,
        clone_env=clone_env,
        sandbox_mode=sandbox_mode,
    )


async def _git_fetch_branch(
    git_url: str,
    branch: str,
    dest: Path,
    log_lines: list[str],
    *,
    credential_target: str | None = None,
    clone_env: dict[str, str],
    sandbox_mode: str,
) -> dict[str, Any] | None:
    """Fetch and check out *branch* without exposing credentials to checkout."""
    return await _git_fetch_ref(
        git_url,
        branch,
        dest,
        log_lines,
        checkout_branch=branch,
        credential_target=credential_target,
        clone_env=clone_env,
        sandbox_mode=sandbox_mode,
    )


# Auth/permission failure classes on the clone-failure surface. The clone
# subprocess merges stderr into stdout (``stderr=STDOUT``), so this classifier
# sees git's auth-failure text. Kept as a STRICT allowlist of known
# auth-refusal phrasings so the credential-posture remedy ("private app repos
# must live inside the registry repo") only fires when withheld credentials
# are a plausible cause — an owner who hits a typo'd branch or a DNS blip must
# NOT be told to restructure their repositories. Matched case-insensitively.
_GIT_AUTH_FAILURE_MARKERS = (
    # SSH credential refusal ONLY. The bare token "permission denied" also
    # appears in a LOCAL filesystem error — an unwritable clone destination
    # emits `fatal: could not create work tree dir '…': Permission denied` —
    # so matching it mislabels a disk-permission failure as withheld remote
    # credentials and shows the "move the repo inside the registry" hint on an
    # error that has nothing to do with credentials. SSH's real auth-refusal
    # wording always carries the method parenthetical (`git@host: Permission
    # denied (publickey).`, also `(publickey,password)` /
    # `(publickey,keyboard-interactive)`), which a local errno `Permission
    # denied` never has — so anchor on `permission denied (publickey` (open
    # paren, no close, to catch every comma-separated method list).
    "permission denied (publickey",
    "authentication failed",
    "could not read username",
    "could not read password",
    "access denied",
    "fatal: authentication",
    "terminal prompts disabled",
    "invalid username or password",
)

# Known NON-auth failure classes, mapped to a fixed derived label. This is an
# allowlist emitting a CONSTANT string per class — never a slice of raw git
# output — so no credential-bearing or path-bearing stderr can reach the
# banner (PR-1418 lesson: free-text stderr passthrough cannot be closed by
# shape enumeration). Matched case-insensitively; first match wins.
_GIT_FAILURE_CLASS_LABELS: tuple[tuple[str, str], ...] = (
    ("could not resolve", "host could not be resolved"),
    ("connection timed out", "the connection timed out"),
    ("connection refused", "the connection was refused"),
    ("network is unreachable", "the network was unreachable"),
    ("remote branch", "the requested branch does not exist"),
    # Anchored on git's own ref-error phrasing ("couldn't find remote ref …",
    # "remote ref … does not exist"), NOT the bare token "does not exist": that
    # substring also appears in unrelated failures (e.g. a path/pathspec error),
    # and matching it would mislabel them "the requested ref does not exist".
    # "remote ref" occurs only in git's missing-ref messages, so it stays a
    # precise ref-error signal.
    ("remote ref", "the requested ref does not exist"),
    # Anchored on git/curl/(open|gnu)tls TLS-error phrasing, NOT the bare token
    # "ssl": that substring also appears in a repo URL (e.g. cloning
    # github.com/openssl/openssl, whose stderr echoes the URL), and matching it
    # would mislabel an ordinary auth/not-found failure "a TLS/SSL error
    # occurred" — the exact false-positive class this table guards against.
    # These phrases occur in genuine TLS handshake/verification errors
    # ("SSL certificate problem …", "SSL routines:…", "gnutls_handshake()
    # failed", "TLS handshake failed", "Unsupported SSL backend 'schannel'")
    # and never in a normal repo URL path segment. Deliberately no bare "ssl_"
    # anchor: a repo path like ".../ssl_utils" would match it. Likewise the
    # gnutls anchor carries git's full symbol "gnutls_handshake" rather than the
    # bare library name, so cloning github.com/gnutls/gnutls (whose stderr
    # echoes the URL) is not mislabeled a TLS error.
    ("ssl certificate", "a TLS/SSL error occurred"),
    ("ssl routines", "a TLS/SSL error occurred"),
    ("ssl backend", "a TLS/SSL error occurred"),
    ("gnutls_handshake", "a TLS/SSL error occurred"),
    ("tls handshake", "a TLS/SSL error occurred"),
)


def _git_output_is_auth_shaped(text: str) -> bool:
    """Whether *text* matches a known auth/permission failure class.

    Strict allowlist — see :data:`_GIT_AUTH_FAILURE_MARKERS`. Never echoes
    *text*; returns only a boolean.
    """
    low = text.lower()
    return any(marker in low for marker in _GIT_AUTH_FAILURE_MARKERS)


def _redacted_git_failure_class(text: str) -> str:
    """A fixed, derived label for a known non-auth failure class, or ``""``.

    Returns a CONSTANT allowlisted phrase — never a slice of *text* — so no
    credential-bearing or path-bearing subprocess output reaches the banner.
    """
    low = text.lower()
    for marker, label in _GIT_FAILURE_CLASS_LABELS:
        if marker in low:
            return label
    return ""


async def _git_clone_or_pull(
    git_url: str,
    branch: str,
    dest: Path,
    log_lines: list[str],
    *,
    credential_target: str | None = None,
    index_originated: bool = False,
    pending_cleanup: list[Path] | None = None,
    restorable_stale: list[Path] | None = None,
    commit: str = "",
) -> dict[str, Any] | None:
    """Clone credential-free *git_url*, or fast-forward it if already present.

    Returns None on success, or a ``{"ok": False, ...}`` error dict on failure.

    ``credential_target`` may carry embedded userinfo for the network request,
    but it is deliberately separate from *git_url*: the latter is repository
    identity and the only target allowed into sandbox argv, diagnostics, stored
    origin, and logs. The raw target reaches only the per-network-call transport
    environment created after :func:`wrap_argv` returns.

    If *pending_cleanup* is provided (a mutable list), any moved-aside directory
    that should be deleted after the caller's full install transaction succeeds
    is appended to it. The caller is responsible for cleaning up these paths
    on the happy path; on failure, the old checkout has already been restored
    by this function's finally block.

    If *restorable_stale* is provided (a mutable list), a moved-aside checkout
    that is the SAME repository as the active one (a branch drift, not a
    different repo) is additionally appended here. Only a path in BOTH
    *pending_cleanup* and *restorable_stale* is safe to hand back as
    ``restore_from`` on a later rejection — restoring an origin-mismatched
    move-aside would give the build the exact tree an earlier gate refused.

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
    transport_target = credential_target or git_url
    if _git_target_is_unsupported(transport_target):
        return {
            "ok": False,
            "error": (
                "git clone target contains an unsupported query or fragment or an "
                "ambiguous Git transport identity"
            ),
        }
    if _strip_git_target_userinfo(git_url) != git_url:
        raise ValueError("_git_clone_or_pull requires a credential-free git_url")
    credential_target = credential_target or git_url
    if _strip_git_target_userinfo(credential_target) != git_url:
        raise ValueError("credential_target does not match git_url")
    credentialed_transport = credential_target != git_url

    clone_env = anonymous_git_env() if index_originated else minimal_env()
    sandbox_mode = "strict" if index_originated else _context_clone_sandbox_mode(git_url)
    # SSRF gate: refuse to clone/pull from a host the owner does not explicitly
    # trust (public forge or configured registry). The git_url may originate
    # from an untrusted external registry index; this prevents a clone against
    # a loopback/internal destination it could inject. is_clone_host_trusted()
    # loads config from disk, so run it off the event loop.
    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        log_lines.append(
            "Refusing clone: host of "
            f"{_strip_git_target_userinfo(git_url)!r} is not a trusted forge/registry"
        )
        return {
            "ok": False,
            # Human sentence in `error`, machine slug in `code`: the install
            # banner renders `result.error`, never `result.message`.
            "code": "untrusted_clone_host",
            "error": (
                "Refusing to clone from an untrusted host "
                "(not a public forge or configured registry)."
            ),
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
        # remote: require the existing origin to resolve to the same clone
        # target as the vetted git_url. Userinfo is deliberately ignored here:
        # credential rotation must not turn the same repository into a rebind.
        # Any other mismatch moves the stale clone aside and re-clones from the
        # URL the posture decision was actually made for.
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
                # Human sentence in `error`, machine slug in `code`: the App
                # Store install banner renders `result.error` and never
                # `result.message`, so the slug must not sit in `error`.
                "code": "unreadable_clone_origin",
                "error": (
                    "The existing checkout's origin remote is unreadable. "
                    f"Remove or fix it manually at {dest} and retry the install."
                ),
            }
        if not _same_git_target(existing_origin, git_url):
            # Parity with the branch-mismatch path below: say WHY the checkout
            # is being replaced before doing it, naming the mismatched origin,
            # so the install log records the re-clone reason instead of a bare
            # move-aside line.
            log_lines.append(
                "Existing clone origin "
                f"{_strip_git_target_userinfo(existing_origin)!r} does not match "
                f"{_strip_git_target_userinfo(git_url)!r}; moving aside stale clone "
                "for re-clone"
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
                    "code": "stale_clone_not_removed",
                    "error": (
                        "A checkout of a different repository is present and could not be "
                        f"moved aside (a file at {dest} may be locked or in use). "
                        "Remove it manually and retry the install."
                    ),
                }
        elif credentialed_transport and not commit:
            # Never run a credentialed `git pull` inside an app-controlled
            # checkout. Pull can merge/checkout and invoke hooks or named filter
            # drivers from that repository while the raw URL rewrite is present.
            # Preserve the verified same-origin tree, then materialise a clean
            # replacement whose fetch alone receives the credential.
            log_lines.append(
                "Credentialed branch update requires an isolated fetch; moving "
                "the existing checkout aside for a restorable replacement"
            )
            moved_aside = await _move_checkout_aside(dest, log_lines)
            if moved_aside is None:
                return {
                    "ok": False,
                    "name": dest.name,
                    "code": "existing_checkout_not_moved_aside",
                    "error": (
                        "The existing app checkout could not be moved aside, so a "
                        "credentialed update cannot be performed safely. Remove or "
                        "move it manually and retry the install."
                    ),
                }
            moved_aside_is_restorable = True

    if not commit and dest.is_dir() and (dest / ".git").is_dir():
        # Branch re-convergence only applies to branch-tracking entries. A
        # commit-pinned install never reuses the existing tree (the `if commit:`
        # block below moves it aside and re-fetches detached regardless), so
        # reading its branch here is pointless work — and pointless attack
        # surface: the read touches ``.git/HEAD`` inside a checkout the app's
        # own build script can rewrite. Skipping it when `commit` is set keeps
        # the reconvergence read off the pinned-update path entirely (the read
        # itself is also bounded in :func:`_read_clone_branch`, so the fast
        # path at :func:`_clone_branch_matches` is safe too).
        #
        # Origin is verified — but the checked-out branch may have drifted
        # (e.g. a registry entry changed from branch A to branch B). If so,
        # the same move-aside/re-clone treatment applies: do NOT checkout in
        # place (local edits would be carried over silently), move the old
        # checkout aside so it is preserved for manual recovery, then fall
        # through to a fresh clone of the correct branch.
        #
        # IMPORTANT: Only move aside when a CONCRETE branch name was read AND
        # it differs from the requested branch. When the read returns None
        # (detached HEAD, unreadable .git/HEAD, gitfile layout) we fall
        # through to the pull path — this is the pre-PR behavior for that
        # checkout (non-destructive). Detached HEAD is the normal healthy
        # state for tag-pinned entries (and for any commit-pinned checkout,
        # which is always fetched detached — see :func:`_git_fetch_commit`);
        # treating it as a confirmed mismatch would destroy a working
        # checkout on every update cycle.
        clone_branch = await asyncio.to_thread(_read_clone_branch, dest)
        if clone_branch is None:
            # Unknown branch state — do not destroy the checkout.
            log_lines.append(
                f"Cannot determine branch of existing checkout at {dest} "
                f"(detached HEAD or unreadable .git/HEAD); skipping "
                f"branch re-convergence and proceeding with pull"
            )
        elif clone_branch != branch:
            log_lines.append(
                f"Existing clone branch {clone_branch!r} does not match "
                f"requested branch {branch!r}; moving aside for re-clone"
            )
            moved_aside = await _move_checkout_aside(dest, log_lines)
            if moved_aside is None:
                log_lines.append(
                    f"Refusing to build from the checkout on the wrong branch at {dest}"
                )
                return {
                    "ok": False,
                    "name": dest.name,
                    # Human sentence in `error`, machine slug in `code`: the
                    # install banner renders `result.error`, never `.message`.
                    "code": "stale_clone_not_removed",
                    "error": (
                        "A checkout on the wrong branch is present and could not be "
                        "moved aside. Remove it manually and retry the install."
                    ),
                }
            # Origin was already verified identical above, so this is the SAME
            # repository the user was on — only its branch drifted. That makes
            # it restorable on a later build/install failure, exactly like the
            # pinned-install move-aside below: a failed transaction must put
            # the user's own (possibly edited) branch-A tree back rather than
            # strand it as an undiscoverable `.stale-*` sibling.
            moved_aside_is_restorable = True

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
                    # Human sentence in `error`, machine slug in `code`: the
                    # install banner renders `result.error`, never `.message`.
                    "code": "existing_checkout_not_moved_aside",
                    "error": (
                        "The existing app checkout could not be moved aside, so a "
                        "pinned install cannot be performed safely. Remove or move it "
                        "manually and retry the install."
                    ),
                }
            # Fall through: `dest` no longer exists, so the pinned fetch below
            # creates it fresh inside the try/finally that owns restoration.
        else:
            # Already cloned from the verified origin AND branch (or the branch
            # state was unknown and re-convergence was skipped) — fetch and
            # fast-forward. (The origin-mismatch gate above guarantees this
            # checkout's origin is the same normalized target as git_url: a
            # mismatched checkout was moved aside and never reused. Pull from the
            # current registry URL through the command-scoped rewrite, so rotated
            # credentials take effect without ever being persisted in origin.)
            log_lines.append(
                f"Updating {_strip_git_target_userinfo(git_url)} (branch: {branch})..."
            )
            # Route through wrap_argv (OS sandbox) THEN cgroup_scope_argv, matching
            # the fresh-clone path below — the cgroup DoS ceiling is the outermost
            # layer but must not replace the wrap_argv sandbox on this
            # agent-influenced git spawn.
            pull_cmd, _cleanup = await wrap_argv_async(
                ["git", "pull", "--ff-only", git_url, branch],
                mode=sandbox_mode,
                _prepare=wrap_argv,
            )
            pull_cmd = cgroup_scope_argv(pull_cmd)
            pull_env = _git_transport_env(credential_target, git_url, clone_env)
            proc = await create_subprocess_limited(
                *pull_cmd,
                cwd=str(dest),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                env=pull_env,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                log_lines.append(
                    _loggable_git_transport_output(
                        stdout.decode(errors="replace").strip(),
                        credentialed=credentialed_transport,
                    )
                )
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
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if result is not None:
                return result
            clone_succeeded = True
            return None

        if credentialed_transport:
            result = await _git_fetch_branch(
                git_url,
                branch,
                dest,
                log_lines,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if result is not None:
                return result
            clone_succeeded = True
            return None

        log_lines.append(f"Cloning {_strip_git_target_userinfo(git_url)} (branch: {branch})...")
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
        sandboxed_cmd, _cleanup = await wrap_argv_async(
            clone_cmd, mode=sandbox_mode, _prepare=wrap_argv
        )
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
        transport_env = _git_transport_env(credential_target, git_url, clone_env)

        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=transport_env,
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
        clone_output = ""
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
            clone_output = _loggable_git_transport_output(
                stdout.decode(errors="replace").strip(),
                credentialed=credentialed_transport,
            )
            log_lines.append(clone_output)
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            await _rmtree_force_settled(dest)
            return {"ok": False, "name": dest.name, "error": "git clone timed out"}
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            await _rmtree_force_settled(dest)
            raise
        if proc.returncode != 0:
            await _rmtree_force_settled(dest)
            if index_originated:
                # The clone ran credential-free (anonymous_git_env + strict
                # sandbox) because the repo URL came from an external registry
                # index whose repo differs from the registry URL, so owner
                # credentials were withheld (confused-deputy defense). A private
                # sibling repo therefore fails to clone. BUT the credential-
                # posture remedy ("private app repos must live inside the
                # registry repo") is only honest when a withheld credential is a
                # plausible cause: gate it on an auth-shaped failure class. A
                # typo'd branch, a DNS blip, or a deleted public repo returns the
                # bare honest failure instead — being told to restructure
                # repositories for a transient error is the misleading-remedy
                # defect this gate closes.
                #
                # No raw clone output ever reaches the banner: the classifiers
                # return only booleans, and the appended failure class is a
                # CONSTANT allowlisted label, so credential-bearing or path-
                # bearing stderr cannot leak (PR-1418 lesson).
                if _git_output_is_auth_shaped(clone_output):
                    remedy_lead = "so owner credentials are withheld"
                elif "repository not found" in clone_output.lower():
                    # A private repo the caller cannot see reads as "repository
                    # not found", so on this credential-free clone a withheld
                    # credential is a *possible* (not certain) cause: keep the
                    # hint, softened to "a likely cause". Deliberately NARROW —
                    # a *branch* not found ("Remote branch X not found") is a
                    # definite typo, not a posture signal, so the bare token
                    # "not found" is excluded.
                    remedy_lead = "so a likely cause is that owner credentials are withheld"
                else:
                    remedy_lead = ""
                if remedy_lead:
                    # Human sentence in `error`, machine slug in `code`: the
                    # install banner renders `result.error`, never `.message`.
                    return {
                        "ok": False,
                        "name": dest.name,
                        "code": "git_clone_failed_no_credentials",
                        "error": (
                            "Git clone failed (cloned without credentials because "
                            "this app's repo URL differs from the registry URL, "
                            f"{remedy_lead}). Private app repos must live inside "
                            "the registry repo — see the monorepo layout in "
                            "docs/app-kit/publishing-guide.md."
                        ),
                    }
                # Not auth-shaped: bare honest failure, with the redacted
                # (allowlisted, constant) failure class when one is recognized.
                failure_class = _redacted_git_failure_class(clone_output)
                if failure_class:
                    return {
                        "ok": False,
                        "name": dest.name,
                        "code": "git_clone_failed",
                        "error": f"Git clone failed: {failure_class}.",
                    }
                return {
                    "ok": False,
                    "name": dest.name,
                    "code": "git_clone_failed",
                    "error": "git clone failed",
                }
            return {
                "ok": False,
                "name": dest.name,
                "code": "git_clone_failed",
                "error": "git clone failed",
            }
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
                await _rmtree_force_settled(dest)
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


def _restorable_or_none(pending: list[Path] | None, restorable: list[Path] | None) -> Path | None:
    """Return the moved-aside checkout a refusal may restore, or None.

    ``pending`` mirrors ``_pending_stale_cleanup`` (every move-aside this run,
    regardless of reason) while ``restorable`` mirrors ``_restorable_stale``
    (the same-repository subset — branch drift, not a different repo). Only a
    path present in BOTH is safe to hand back as ``restore_from``: restoring
    an origin-mismatched move-aside would give a rejection the exact tree an
    earlier gate already refused.
    """
    if not pending:
        return None
    candidate = pending[0]
    return candidate if candidate in (restorable or []) else None


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
    # stamped onto the result inside `_clone_build_app_locked`. That function has
    # several exits and the state was only attached on the successful one, so a
    # post-fetch failure -- a subdirectory that escapes containment, an identity
    # mismatch, a rejected admission -- dropped it: the caller's `finally` had
    # nothing to restore from AND `_report_retained_stale_checkouts` iterated an
    # empty list, so a non-restorable (origin-mismatch) checkout was stranded as a
    # `.stale-*` sibling, unreported, until the retention sweep deleted it. Two
    # lists the callee fills and this one exit reads cannot be forgotten by a new
    # exit: `pending_cleanup` is every move-aside this run, `restorable_stale` the
    # same-origin subset a failure-path restore may put back.
    pending_cleanup: list[Path] = []
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
            pending_cleanup=pending_cleanup,
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
        # The restore above puts back the same-origin subset; the NON-restorable
        # move-asides (origin-mismatch tree-asides, deliberately kept) are left on
        # disk as `.stale-*` siblings. On this exception path there is no result
        # dict, so the caller's `finally`-owned reporter never learns of them and
        # the age-based sweep would delete a checkout the user was never told
        # about. Report them through the SHARED reporter -- the one owner of the
        # "Previous checkout retained at" wording -- so this path and the finally
        # can never drift apart. `filter_restorable=True` skips the same-origin
        # subset the restore above just put back, matching the finally's
        # post-restore call. Synchronous: the reporter only appends to a list and
        # logs, so it needs no loop (awaiting during cancellation re-enters a
        # closing loop -- see the SYNCHRONOUS note above).
        _report_retained_stale_checkouts(
            {
                "_pending_stale_cleanup": pending_cleanup,
                "_restorable_stale": restorable_stale,
            },
            log_lines,
            filter_restorable=True,
        )
        raise
    if isinstance(result, dict):
        # Stamp the FULL move-aside state on EVERY dict result crossing this
        # single exit -- refusals included -- so the caller's
        # `_report_retained_stale_checkouts` names a retained non-restorable
        # checkout instead of dropping it. This is report/restore metadata only:
        # it changes no path that gets restored or deleted.
        if pending_cleanup:
            result["_pending_stale_cleanup"] = list(pending_cleanup)
        if restorable_stale:
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

    *manifest_relpath* is the untrusted registry-declared manifest path the
    caller built (``f"{subdirectory}/app.json"``, or plain ``app.json`` when
    no subdirectory was declared). A build step or ``onInstall`` script runs
    with write access to the checkout BEFORE some callers reach this cleanup,
    and can plant a symlink at the manifest path — the subdirectory OR the
    leaf — after an earlier containment check already passed; this restore
    then runs unsandboxed as the Kiro Crew process, so it must not trust that
    earlier check. Containment of the FULL manifest path is re-verified HERE,
    at the point of the write, against the CURRENT on-disk state: on a
    failure the manifest restore (both the raw-write and the git-checkout
    fallback) is skipped so neither can be redirected outside *pkg_dir*
    through a symlink planted after the caller's check. The pre-pull
    ``git reset`` above is unaffected — it targets the whole checkout, not
    the manifest path.
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
        cmd, _cleanup = await wrap_argv_async(argv, mode="standard", _prepare=wrap_argv)
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
    if _contained_join(pkg_dir, manifest_relpath) is None:
        # manifest_relpath (the FULL path, e.g. "sub/app.json") no longer
        # resolves inside pkg_dir RIGHT NOW — some callers reach this point
        # after a build step or onInstall script ran with write access to the
        # checkout, so a containment check the caller made earlier cannot be
        # trusted here. Checking only `subdirectory` (the directory, and only
        # when non-empty) misses a symlink planted at the manifest LEAF itself
        # -- `subdirectory/app.json`, or plain `app.json` when there is no
        # subdirectory -- which is exactly what the raw write and the
        # git-checkout fallback below target; either would follow such a
        # symlink and write outside pkg_dir as this unsandboxed process. Skip
        # the manifest restore entirely rather than risk that write; the
        # rollback above already ran and stands. Unconditional (no
        # `if subdirectory` gate): the same leaf-symlink attack works with an
        # empty subdirectory too, where manifest_relpath is just "app.json".
        log_lines.append(
            f"WARNING: {manifest_relpath!r} no longer resolves inside "
            "the checkout; skipping manifest restore to avoid writing through "
            "a symlink escape. A retry may keep rejecting until the source is "
            "repaired."
        )
        return
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
    pending_cleanup: list[Path],
    restorable_stale: list[Path] | None = None,
) -> dict[str, Any]:
    """Inner implementation of _clone_build_app, called under per-app lock.

    *pending_cleanup* and *restorable_stale* are caller-owned mutable lists
    (see :func:`_clone_build_app`): this function fills them so the wrapper's
    single return can stamp the full move-aside state onto EVERY dict result,
    refusals included. *pending_cleanup* is REQUIRED — the sole production
    caller always threads its own list through so the wrapper's single exit
    can read the move-aside state, and every test constructs one too; an
    optional-with-``None`` shape would only invite a caller to drop the list
    and silently lose that state, so there is no default to fall back to.
    """
    credential_target = git_url
    if _git_target_is_unsupported(credential_target):
        return {
            "ok": False,
            "name": app_name,
            "error": (
                "git clone target contains an unsupported query or fragment or an "
                "ambiguous Git transport identity"
            ),
        }
    git_url = _strip_git_target_userinfo(credential_target)
    if not _looks_like_git_url(git_url):
        return {
            "ok": False,
            "name": app_name,
            "error": (f"{_strip_git_target_userinfo(git_url)!r} is not a cloneable git URL"),
        }

    pkg_dir = app_source_dir(app_name)
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
        credential_target=credential_target,
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
            restore_from=_restorable_or_none(pending_cleanup, restorable_stale),
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
            restore_from=_restorable_or_none(pending_cleanup, restorable_stale),
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
        # the old checkout. The paths are surfaced to the caller by
        # `_clone_build_app`'s single-exit stamp (every dict result carries
        # `_pending_stale_cleanup`), so no explicit stamping is needed here.
        # The dirs are harmless siblings swept by _sweep_stale_checkouts()
        # after _STALE_CHECKOUT_RETENTION_DAYS (best-effort, runs at the
        # start of the next install_from_registry call).
        pass
    else:
        # Build failed — restore the old checkout so the user's local edits
        # survive. Remove the (successfully cloned but unbuildable) new dest
        # and rename the moved-aside dir back.
        #
        # But ONLY for RESTORABLE move-asides. `pending_cleanup` carries every
        # move-aside this run made — both same-origin/branch-drift asides
        # (restorable: restoring them is the point) AND origin-mismatch asides
        # the identity gate deliberately refused to serve. Restoring the latter
        # would re-seat a repository the gate just rejected into the active
        # source slot the instant its replacement's build fails — the exact
        # confused-deputy residue the restorable/pending split exists to close.
        # Membership is tested against `restorable_stale`, the caller-owned list
        # populated at the same move-aside site (identity `in`, comparing the
        # Path objects both lists share — never a re-derived string that path
        # aliasing could spoof). A non-restorable aside stays in
        # `pending_cleanup` untouched so the single-exit stamp carries it and
        # the finally-owned `_report_retained_stale_checkouts` names it.
        # `restorable_stale or []`: a missing list means NOTHING is restorable,
        # so every move-aside is retained rather than restored — the fail-closed
        # default (the production caller always threads a real list; this only
        # guards a caller that omits it from re-seating a checkout by accident).
        restorable_set = set(restorable_stale or [])
        restored_paths: list[Path] = []
        for stale_path in pending_cleanup:
            if stale_path not in restorable_set:
                # Refused-origin checkout: never restored into the active slot.
                # Left in pending_cleanup so it is reported retained, not swept
                # silently and not re-seated as the live app source.
                log_lines.append(
                    "Build failed; origin-mismatched checkout NOT restored, "
                    f"retained at: {stale_path}"
                )
                continue
            if stale_path.exists():
                await asyncio.to_thread(shutil.rmtree, pkg_dir, True)
                try:
                    await asyncio.to_thread(stale_path.rename, pkg_dir)
                    log_lines.append(
                        "Build failed; previous checkout restored from " f"{stale_path.name}"
                    )
                    restored_paths.append(stale_path)
                except OSError as exc:
                    log_lines.append(
                        f"Build failed; could not restore previous checkout "
                        f"from {stale_path}: {exc}. Recover your files from "
                        f"{stale_path}"
                    )
        # Drop the checkouts actually put back from the caller-owned pending
        # list: a restored checkout is no longer a retained `.stale-*` sibling,
        # so `_clone_build_app`'s single-exit stamp must not carry it and the
        # caller's `_report_retained_stale_checkouts` must not name it. A rename
        # that FAILED above stays in the list so it is still reported stranded.
        for restored in restored_paths:
            pending_cleanup.remove(restored)
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
        sandboxed_cmd, _cleanup = await wrap_argv_async(
            cmd, mode="standard", _prepare=wrap_argv
        )
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


def _report_retained_stale_checkouts(
    build_result: dict[str, Any] | None,
    log_lines: list[str],
    *,
    filter_restorable: bool,
) -> None:
    """Log a "Previous checkout retained at" line for each moved-aside
    checkout that will actually stay retained after this call.

    CONTRACT: this is the ONLY owner of the "Previous checkout retained at"
    wording — no exit re-implements the string. It has exactly TWO call sites,
    and neither is a per-exit copy of the other:

    - the ``finally`` of :func:`install_from_registry`, AFTER that ``finally``
      has run its restore block. This is the ordinary path: it reaches EVERY
      normal exit (success and refusal alike) via the single ``finally``,
      passing the returned ``build_result`` and ``filter_restorable=not
      durable_success`` so the flag is derived once, never hand-mirrored.
    - the exception handler in :func:`_clone_build_app` (the ``except`` that
      re-raises a build error). That path produces NO result dict — the
      exception propagates instead of returning — so the ``finally`` above
      never sees the move-aside state. This second call synthesises a minimal
      dict from that scope's ``pending_cleanup``/``restorable_stale`` and
      passes ``filter_restorable=True`` (the handler restored the same-origin
      subset just above it), so a non-restorable ``.stale-*`` on the
      exception path is still named instead of being silently swept.

    Both routes funnel the wording through here precisely so they can never
    drift: the reporter used to be hand-replicated across every exit with a
    ``filter_restorable`` flag manually mirrored to ``durable_success`` at each
    one, which is the scattered-per-exit stranding class the caller's
    move-aside bookkeeping exists to avoid — a new exit could forget the call
    or pass the wrong flag and silently strand or double-report a checkout.
    Every normal exit now reaches the single ``finally`` call and derives the
    flag once; the only other caller is the exception path that no ``finally``
    return can cover.

    ``_pending_stale_cleanup`` collects every move-aside regardless of
    reason, but ``_restorable_stale`` (a subset) is put back by the
    enclosing ``finally`` — and ONLY when the exit leaves ``durable_success``
    False. The single call passes ``filter_restorable=not durable_success``,
    exactly the restore condition, so the flag can never drift from it:

    - On a failure exit (``durable_success`` False) the ``finally`` restored
      the restorable stale just before this call, so ``filter_restorable`` is
      True and that path — now back in place on disk — is filtered out rather
      than misreported as retained.
    - On a durable-success exit (``durable_success`` True) the ``finally``
      restores nothing, so ``filter_restorable`` is False and a restorable
      stale genuinely retained at ``.stale-*`` is reported instead of sitting
      unlogged until the age-based sweep — the possible-data-loss case this
      covers. A durable-success exit includes one where provenance
      persistence raised AFTER ``durable_success`` was set: the generic
      ``except`` catches it, the ``finally`` still sees ``durable_success``
      True, and this reporter names the retained stale.
    """
    if build_result is None:
        return
    restorable = set(build_result.get("_restorable_stale") or []) if filter_restorable else set()
    for stale in build_result.get("_pending_stale_cleanup") or []:
        if stale in restorable:
            continue
        log_lines.append(f"Previous checkout retained at: {stale}")
        logger.info("Retained stale checkout: %s", stale)


async def _retained_startup_refusal(
    name: str, log_lines: list[str]
) -> dict[str, Any] | None:
    """Return a retryable refusal while old-version startup code remains live."""
    # Deferred to avoid registry -> hooks_integration -> manager import cycles at
    # module load. The dispatcher exists only in the gateway process; without it
    # there is no in-process retained startup task to own.
    from kiro_crew.apps.hooks_integration import stop_retained_startup_hooks

    if await stop_retained_startup_hooks(name, bounded=True):
        return None
    message = (
        f"cannot reinstall {name!r} while its timed-out startup hook is still "
        "running; retry after it exits"
    )
    log_lines.append(message)
    return {
        "ok": False,
        "name": name,
        "error": message,
        "code": "startup_hook_still_running",
        "retryable": True,
    }


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
    # Reject an inadmissible name BEFORE the registry lookup and any
    # clone/build/onInstall work. The manifest/self-registration gates repeat
    # this check, but for a self-managed app they only fire at runtime
    # self-registration — without this early refusal the install would clone,
    # build, and run onInstall, then report success while leaving an
    # unregisterable checkout behind. Name admissibility is independent of
    # registry contents, so this precedes _resolve_install_entry.
    name_error = app_name_error(name)
    if name_error:
        outcome_early: dict[str, Any] = {
            "ok": False,
            "name": name,
            "error": name_error,
            "log": "",
        }
        # `code` only for the reserved-name refusals — same contract as the
        # register_external_app path (is_reserved_app_name gates the code there).
        if is_reserved_app_name(name):
            outcome_early["code"] = RESERVED_APP_NAME_CODE
        return outcome_early

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
    if _git_target_is_unsupported(git_url):
        return {
            "ok": False,
            "name": name,
            "error": (
                "app registry clone URL contains an unsupported query or fragment or "
                "an ambiguous Git transport identity"
            ),
            "code": "invalid_registry_source",
        }
    persisted_git_url = _strip_git_target_userinfo(git_url)

    # A per-app execution grant is consent to the repository the operator saw,
    # not to whichever repository later claims the same app name. New grants
    # record that coordinate; a legacy name-only grant needs one-time re-consent
    # before repository-backed bytes can be fetched or executed. This gate runs
    # before manifest fetch, credential selection, clone, build, or setup code.
    granted_repository = trusted_app_repository(name)
    trust_denied = repository_bound_grant_denied(name, repository=git_url)
    if trust_denied:
        # The exact coordinates are comparison inputs, not log/API data. Clone
        # URLs can contain userinfo credentials; every copy of this reason is
        # audited or returned to the dashboard, so keep it credential-free.
        reason = trust_denied
        # A bound mismatch must first be revoked. A legacy unbound grant instead
        # needs the normal consent dialog, whose stable trigger is the execution
        # denial code. Keep both existing wire behaviours explicit.
        code = (
            "app_trust_repository_mismatch"
            if granted_repository
            else "app_execution_denied"
        )
        audit_operation = (
            "trust_repository_mismatch"
            if granted_repository
            else "trust_repository_binding_required"
        )
        try:
            sel().log_api_access(
                caller="app_install_from_registry",
                operation=audit_operation,
                outcome="rejected",
                resources=f"name={name!r}",
                error=reason,
            )
        except Exception as exc:  # an audit failure must never mask the refusal
            logger.debug("SEL audit failed for %s trust repository mismatch: %s", name, exc)
        return {
            "ok": False,
            "name": name,
            "error": reason,
            "code": code,
        }

    raw_repo = entry.get("repo", "")
    repo = _strip_git_target_userinfo(raw_repo) if isinstance(raw_repo, str) else ""
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
    index_originated = _remote_controlled_url(
        entry
    )  # OFFICIALNESS is decided from `_registry` ALONE, and BEFORE the
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
    source_registry = _strip_git_target_userinfo(str(entry.get("_registry", "") or ""))
    owner_designated_target = (
        await asyncio.to_thread(_owner_designated_repo_target, entry)
        if index_originated
        else ""
    )
    if index_originated and owner_designated_target:
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
        git_url=owner_designated_target or git_url,
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

    startup_refusal = await _retained_startup_refusal(name, log_lines)
    if startup_refusal is not None:
        return startup_refusal

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
    execution_denied = app_execution_denied(
        name,
        action="registry_install",
        caller="registry",
        repository=git_url,
    )
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
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                base_cmd, mode="strict", _prepare=wrap_argv
            )
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
    # Every `return` below assigns here first (named `outcome`, not `result` —
    # the kirocrew-managed path below already uses `result` for the
    # install_app/update_app return value). `"log"` is stamped from
    # `log_lines` at assignment time, but the `finally` backstop can append to
    # `log_lines` (a restore confirmation, or the restore-failed WARNING) AFTER
    # that value is already computed — a `return`'s expression is evaluated
    # before `finally` runs, and `str.join` produces an immutable copy, so a
    # later append never reaches an already-built "log" string. Because
    # dicts ARE mutable, holding the same object here and re-stamping
    # `outcome["log"]` at the end of `finally` (below) closes that gap instead
    # of the WARNING silently never reaching the log the user sees.
    outcome: dict[str, Any] | None = None
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
            owner_designated_target or git_url,
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
            # A pre-build refusal (identity/admission gate inside
            # _clone_build_app), a failed clone, or a failed build may have left
            # a non-restorable origin-mismatch checkout moved aside. Retained-stale
            # reporting and restorable-stale restoration are both owned by the
            # single `finally` below: it runs on every exit, knows durable_success,
            # and re-stamps outcome["log"], so no per-exit report or log join is
            # needed here.
            outcome = {**build_result}
            return outcome

        app_source = build_result["pkg_dir"]
        clone_root = app_source
        if subdirectory:
            # ``subdirectory`` is untrusted index-controlled content. Join it
            # under the cloned source root with symlink-resolving containment so
            # an absolute/``..``/symlink value cannot point app.json (and thus
            # setup.onInstall) at an attacker-selected path outside the clone.
            contained = _contained_join(app_source, subdirectory)
            if contained is None:
                # subdirectory FAILED containment here — by definition it is
                # an escaping value (absolute, "..", or a symlink pointing
                # outside app_source). It must never be joined onto pkg_dir
                # for a filesystem write; that is exactly what
                # _contained_join guards against. The manifest-restore step
                # of _unpoison_rejected_checkout writes to
                # ``pkg_dir / manifest_relpath`` when checkout_preexisted is
                # True, so passing the raw subdirectory as manifest_relpath
                # there would let a symlinked subdirectory redirect that
                # write outside the sandboxed checkout.
                #
                # A successful clone+build already ran (build_result["ok"] is
                # True), so any moved-aside checkout from a branch/origin
                # re-convergence must not be silently stranded by this
                # refusal — but only the delete-this-run's-checkout /
                # restore-previous-checkout branch of the helper (taken when
                # checkout_preexisted is False) is safe here: it never
                # touches manifest_relpath. When the checkout PRE-existed,
                # skip cleanup entirely and return the refusal as-is rather
                # than risk that write.
                if not build_result.get("_checkout_preexisted"):
                    # Restoring a moved-aside checkout here means giving the
                    # rejected clone's own pkg_dir back to the CALLER as the
                    # active checkout, even though the containment gate just
                    # refused it. That is only safe for a restorable
                    # (same-origin, branch-drift) stale, never for a
                    # non-restorable (origin-mismatch, different repository)
                    # one — restoring an origin-mismatched stale here is
                    # exactly the "hand the build the tree the gate refused"
                    # case _restorable_stale exists to prevent, so it must be
                    # filtered out the same way every other restoration site
                    # in this module filters it.
                    await _unpoison_rejected_checkout(
                        name,
                        app_source_dir(name),
                        log_lines,
                        checkout_preexisted=False,
                        pre_pull_commit="",
                        restore_from=_restorable_or_none(
                            build_result.get("_pending_stale_cleanup"),
                            build_result.get("_restorable_stale"),
                        ),
                    )
                # The _unpoison above restores only a restorable stale, so an
                # origin-mismatch one is stranded here — the `finally`-owned
                # reporter names it and re-stamps outcome["log"].
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"unsafe subdirectory {subdirectory!r} escapes the app source root",
                }
                return outcome
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
            outcome = await _refuse_identity_mismatch(
                name,
                str((manifest_data or {}).get("name", "") or ""),
                _strip_git_target_userinfo(repo),
                clone_root,
                log_lines,
                created_this_run=not bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
            )
            # Retained-stale reporting for this refusal is owned by the
            # `finally` below (it re-stamps outcome["log"] on every exit).
            return outcome

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
            # _unpoison restores only the restorable subset; the `finally`-owned
            # reporter names any stranded non-restorable move-aside from
            # on-disk truth after this restore and re-stamps outcome["log"].
            await _unpoison_rejected_checkout(
                name,
                app_source_dir(name),
                log_lines,
                checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
            )
            outcome = {
                "ok": False,
                "name": name,
                "error": f"blocked by admission policy: {denied}",
            }
            return outcome

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
                _strip_git_target_userinfo(repo),
            )
            try:
                sel().log_api_access(
                    caller="registry",
                    operation="app_install_script",
                    outcome="started",
                    resources=f"{name} repo={_strip_git_target_userinfo(repo)}",
                )
            except Exception as exc:
                logger.debug("SEL audit failed for app %s install: %s", name, exc)
            # Wrap with safe defaults:
            #   set -e  — exit on first error
            #   set -u  — treat unset variables as errors (prevents rm -rf $EMPTY/)
            #   set -o pipefail — propagate pipe failures
            safe_script = f"set -euo pipefail\n{install_script}"

            base_cmd = ["/bin/bash", "-c", safe_script]
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                base_cmd, mode="standard", _prepare=wrap_argv
            )
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
                # Retained-stale reporting and restorable-stale restoration are
                # owned by the `finally` below (it re-stamps outcome["log"]).
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"install script timed out after {_SCRIPT_TIMEOUT}s",
                }
                return outcome

            lines = stdout.decode(errors="replace").strip().split("\n")
            if len(lines) > 50:
                log_lines.append(f"... ({len(lines) - 50} lines truncated)")
                log_lines.extend(lines[-50:])
            else:
                log_lines.extend(lines)

            if proc.returncode != 0:
                # Retained-stale reporting and restorable-stale restoration are
                # owned by the `finally` below (it re-stamps outcome["log"]).
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"install script failed (exit {proc.returncode})",
                }
                return outcome

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
                outcome = await _refuse_identity_mismatch(
                    name,
                    str((manifest_data or {}).get("name", "") or ""),
                    _strip_git_target_userinfo(repo),
                    clone_root,
                    log_lines,
                    created_this_run=not bool(build_result.get("_checkout_preexisted")),
                    pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                    manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                    manifest_snapshot=build_result.get("_pre_update_manifest"),
                    restore_from=_restorable_or_none(
                        build_result.get("_pending_stale_cleanup"),
                        build_result.get("_restorable_stale"),
                    ),
                )
                # Retained-stale reporting for this post-script refusal is owned
                # by the `finally` below (it re-stamps outcome["log"]).
                return outcome
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
                # _unpoison restores only the restorable subset; the
                # `finally`-owned reporter names any stranded non-restorable
                # move-aside from on-disk truth and re-stamps outcome["log"].
                await _unpoison_rejected_checkout(
                    name,
                    app_source_dir(name),
                    log_lines,
                    checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                    pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                    manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                    manifest_snapshot=build_result.get("_pre_update_manifest"),
                    restore_from=_restorable_or_none(
                        build_result.get("_pending_stale_cleanup"),
                        build_result.get("_restorable_stale"),
                    ),
                )
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"blocked by admission policy: {denied}",
                }
                return outcome

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

        # A clone/build/install script can take minutes. Recheck at the shared
        # replacement boundary so startup execution that became retained during
        # that work cannot overlap either managed file replacement or
        # self-managed metadata replacement.
        startup_refusal = await _retained_startup_refusal(name, log_lines)
        if startup_refusal is not None:
            outcome = startup_refusal
            return outcome

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
                source_repository=persisted_git_url,
            )
            if reg_result.ok:
                set_app_provenance(
                    name,
                    source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                    url=persisted_git_url,
                    registry=source_registry,
                    commit=source_commit,
                    signer=source_signer,
                )

            log_lines.append("Pre-registered from cloned manifest (self-managed)")
            log_lines.append("App will update its own registration on next launch")
            # Retained moved-aside checkouts (the user can recover local edits;
            # swept after _STALE_CHECKOUT_RETENTION_DAYS) are reported by the
            # `finally`-owned reporter: durable_success is True, so it runs with
            # filter_restorable=False and names the genuinely-retained restorable
            # stale rather than letting it sit unlogged until the sweep.
            if official_entry:
                install_receipt.dispatch(
                    name,
                    official=True,
                    kind=(
                        install_receipt.KIND_UPDATE if was_installed else install_receipt.KIND_FRESH
                    ),
                )
            outcome = {
                "ok": True,
                "name": name,
                "message": (
                    f"installed {name} from {_strip_git_target_userinfo(repo)} "
                    "(self-managed)"
                ),
            }
            return outcome

        # Kirocrew-managed: copy to ~/.kiro/crew/apps/ and register resources
        log_lines.append("Installing app...")
        # Lock-free: the route handler holds app_lifecycle_lock(name) across
        # the whole transaction (clone/build → copy → register → backend
        # start); asyncio.Lock is not reentrant, so no acquisition here.
        existing = get_app(name)
        # Off-loop: install_app/update_app do a blocking filesystem copy
        # that can take minutes on large source trees — on the loop it
        # would trip the loop-stall watchdog and kill the gateway.
        # Preserve the long-standing one-positional-argument manager contract.
        # The scoped coordinate is copied into asyncio.to_thread's context, so
        # the manager still performs its final repository-binding check and
        # writes safe provisional provenance without trusting app.json.
        with registry_source_repository(persisted_git_url):
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
                url=persisted_git_url,
                registry=source_registry,
                commit=source_commit,
                signer=source_signer,
            )
            # Retained moved-aside checkouts are reported by the `finally`-owned
            # reporter (durable_success is True, filter_restorable=False), so a
            # genuinely-retained restorable stale is named rather than sitting
            # unlogged at `.stale-*` until the sweep. NOTE set_app_provenance
            # above runs while durable_success is already True: if it raises, the
            # generic `except` catches it, the `finally` does NOT restore (durable
            # success), and it reports with filter_restorable=not durable_success
            # = False — so the restorable stale is reported, not stranded.
            if official_entry:
                # Detached best-effort telemetry runs only after durable success.
                install_receipt.dispatch(
                    name,
                    official=True,
                    kind=(
                        install_receipt.KIND_UPDATE if was_installed else install_receipt.KIND_FRESH
                    ),
                )
        # Install/update failed AFTER a successful clone+build: durable_success
        # stays False, so the `finally` restores the restorable stale and its
        # reporter filters it out — no per-exit report is needed here.

        outcome = {
            "ok": result.ok,
            "name": name,
            "message": result.message,
            "error": result.error,
        }
        return outcome

    except Exception as exc:
        logger.exception("Failed to install %s from registry", name)
        # Retained-stale reporting and restorable-stale restoration are owned by
        # the `finally` below. It reports with filter_restorable=not
        # durable_success, which is precisely why an exception raised AFTER
        # durable_success was set (e.g. set_app_provenance) still names the
        # genuinely-retained restorable stale instead of stranding it.
        outcome = {"ok": False, "name": name, "error": str(exc)}
        return outcome
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

        # THE reporter, owned by this `finally` and nowhere else. Placed AFTER
        # the restore block above so it reports on-disk truth: a restorable stale
        # the restore just put back must not then be named as retained. The flag
        # is derived, not hand-mirrored at each exit — `not durable_success` is
        # exactly the restore condition above, so a failure exit (restored) files
        # its restorable stale out and a durable-success exit (never restored)
        # keeps it. This is the whole point of the consolidation: a new exit
        # added to this function cannot forget the report or pass the wrong flag,
        # because there are no per-exit reports left to forget. A no-op unless a
        # move-aside exists (pre-clone exits and the happy-path-with-no-stale
        # pass through untouched).
        _report_retained_stale_checkouts(
            build_result, log_lines, filter_restorable=not durable_success
        )

        # Re-stamp AFTER the restore and the report above: each `return` built its
        # `outcome` dict WITHOUT a "log" key, deferring it to here so the restore
        # confirmation, the restore-failed WARNING, and the retained-stale lines
        # just produced all reach the caller. `outcome` is the SAME dict object
        # being returned (dicts are mutable), so setting its "log" key here is
        # what the caller receives. Pre-clone exits return bare dicts that already
        # carry their own "log" and never set `outcome`, so they skip this
        # backstop and keep their join.
        if outcome is not None:
            outcome["log"] = "\n".join(log_lines)
            # Scrub the internal move-aside/transaction bookkeeping keys from
            # the dict that leaves this function. They are consumed ABOVE (the
            # restore block and the reporter both read them off `build_result`,
            # never off `outcome`), so removing them here deprives no consumer.
            # Two of them -- `_pending_stale_cleanup` and `_restorable_stale` --
            # are `list[Path]`, which is not JSON-serializable, so a build
            # refusal that spreads `{**build_result}` into `outcome` used to make
            # the API/SSE layer raise `TypeError` when it serialized the refusal.
            # Scrubbing the CLASS (every `_`-prefixed key) rather than those two
            # names closes it at the single seam: `_checkout_preexisted`,
            # `_pre_pull_commit`, and `_pre_update_manifest` are internal gate
            # state too, and no current or future exit can leak any of them once
            # they are stripped here. Underscore keys are internal by
            # convention; a response field the caller needs is never named `_x`.
            for _internal_key in [k for k in outcome if k.startswith("_")]:
                outcome.pop(_internal_key, None)
