"""The environments App Registry subprocesses start with.

``minimal_env`` hands an install, build or lifecycle child only the location hints
it needs, never the gateway's secrets. ``anonymous_git_env`` is the credential-free
variant for a clone whose URL came from content the owner did not type, and
``_detect_probe_env`` narrows that further for a ``detectInstalled`` probe, whose
command text is untrusted. Each allowlist carries the reason for its names.
"""

from __future__ import annotations

import os
import sys

from kiro_crew import platform_compat
from kiro_crew.sandbox import scrub_env

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
# Windows child also had no writable temp dir. Matched through
# ``platform_compat.env_key_allowed`` (see ``_is_safe_env_key``); kept in the
# allowlist shape so the credential-scrubbing property is unchanged — these are
# location hints, not secrets.
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
      action gates them. The one browse-time clone that carries credentials is
      not one of these: ``store_art._prewarm_owner_tier_store_assets`` runs on
      the rows a FRESH fetch of a build-pinned ``owner``-tier index just
      returned, in the same call, which is the authority the install path
      re-fetches that index to obtain. It fills the manifest and blob caches
      these two automatic paths then read; on a miss they still clone
      anonymously.
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


#: Env names a ``detectInstalled`` probe may receive. Deliberately an explicit
# KEEP set rather than ``_SAFE_ENV_KEYS`` minus a few names: that allowlist serves
# operator-initiated spawns -- installs, app backends, lifecycle scripts -- so it
# carries toolchain configuration an operator may legitimately have loaded with a
# secret (``MAVEN_OPTS`` with a ``-D`` password, ``GRADLE_USER_HOME`` pointing at a
# credential store, a ``PYTHONPATH``/``NODE_PATH`` tree that lets a probe import
# code). Subtracting each such name as it is noticed leaves the next one in, and a
# probe is the one spawn here whose command text is untrusted, so the axis is
# closed instead: only location hints a ``command -v`` / ``test -x`` style check
# needs to run, plus the Windows names a process needs to start at all (a child
# without ``SystemRoot`` dies before ``main()``). A probe that genuinely needs a
# toolchain variable reads it from the app's own config, not from the operator's
# shell.
_DETECT_PROBE_ENV_KEYS = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        # Windows equivalents, spelled as in `_SAFE_ENV_KEYS`.
        "COMSPEC",
        "PATHEXT",
        "ProgramFiles",
        "PROGRAMFILES",
        "SystemRoot",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
        # Set BY `anonymous_git_env`, not copied from the operator: the git
        # suppression that keeps a credential helper from firing must survive the
        # filter below, or dropping it would undo that suppression.
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_SSH_COMMAND",
    }
)


def _detect_probe_env() -> dict[str, str]:
    """Environment for a ``/bin/sh -c <detectInstalled>`` probe.

    A probe's command string is NOT operator-authored: it comes from an
    app-registry manifest, which is untrusted content, and the listing path runs it
    automatically at browse time. So a probe gets the credential-free treatment an
    index-originated clone gets (:func:`anonymous_git_env`), narrowed further to
    :data:`_DETECT_PROBE_ENV_KEYS`.

    What that closes, in the order the layers apply: :func:`anonymous_git_env`
    drops the agent socket and any ``GIT_SSH``/``GIT_SSH_COMMAND`` override, so
    manifest code cannot authenticate through the operator's keys; it disables
    system and global git config, so a configured credential helper -- macOS ships
    ``osxkeychain`` in its system config, and the ``cache`` helper's socket is
    reachable through an allowlisted ``XDG_CACHE_HOME`` -- never fires for a
    manifest-chosen remote; and it turns prompting off, so a probe fails rather
    than asking the operator for a password. :func:`scrub_env` removes the
    credential-bearing prefixes. The keep set then leaves only location hints, so a
    toolchain variable carrying a secret has no route in.

    All of this matters on one host shape: the sandbox launcher strips the socket
    in every mode, so these names only ever survive where no launcher runs --
    Windows, and a POSIX host with no sandbox backend plus
    ``agent.sandbox_allow_unsandboxed_exec``. ``PATH`` and ``HOME`` are kept, so a
    detect command still resolves programs and still reads its own per-user config.
    """
    scrubbed = scrub_env(anonymous_git_env())
    return {k: v for k, v in scrubbed.items() if _is_probe_env_key(k)}


def _is_probe_env_key(key: str) -> bool:
    """Whether *key* may reach a probe, honoring Windows' case-insensitive env.

    Same matching convention as :func:`_is_safe_env_key` -- exact on POSIX,
    case-folded on Windows -- so a literal membership test cannot silently drop
    ``SystemRoot`` there.
    """
    return platform_compat.env_key_allowed(key, _DETECT_PROBE_ENV_KEYS)
