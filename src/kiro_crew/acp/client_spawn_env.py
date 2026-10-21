"""Prepare an ``AcpClient`` spawn before the process exists.

Resolves the kiro-cli and codex-acp binaries, repairs and scrubs the child's environment
(SSH agent, Kerberos, pod HOME remap and bundle swap, session identity), finishes a
suspended Windows spawn, hands the launch tail its tools, and stages the workspace,
``agent.env`` and ``--agent`` argv for the spawn in progress.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_agent``, ``_agent_env_snapshot``, ``_mcp_ref_spec``.

Instance fields read: ``_native_skill_projection``, ``_session_key``,
``_stub_session_token``, ``_work_dir``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from kiro_crew import agent_scratch, platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.agent_env import agent_env_overlay
from kiro_crew.acp.launch import LaunchTools
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_POD_HOME_REMAP,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    NODE_ADAPTER_ENTRY_SEGMENTS,
)
from kiro_crew.browser_cli.launch import browser_session_env, browser_socket_env
from kiro_crew.env import augmented_path, describe_search_path
from kiro_crew.identity_stores import IDENTITY_STORE_ROOTS
from kiro_crew.kiro_cli import known_kiro_cli_dirs, resolve_kiro_cli
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.resource_status import inject_xdist_auto_cap
from kiro_crew.sandbox import (
    _forward_ssh_auth_sock,
    apply_windows_resource_ceiling,
    bind_voice_safe_agent_workspace_async,
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
    wrapped_by_crew_sandbox,
)

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


# Every adapter/harness executable name is READ from the backend registry rather than
# spelled here or in the host adapters under ``acp/harness``. The registry is also what the reclaim sweep projects its
# marker set from (``session_pid._MANAGED_AGENT_MARKERS``), and a name written in both
# places is a name that can drift -- a rename here that missed the table would leave
# the sweep unable to recognise the process this module spawns, which spares an orphan
# and then drops its tracking entry. The import direction is the allowed one: the ACP
# layer may read ``agent_sdk.backends`` (a stdlib-only leaf it already imports for
# ``launch_for``), while ``session_pid`` may not import the ACP layer at all --
# ``scripts/check_agent_sdk_boundary.py`` counts even a type-only import as knowledge.
# The one name NOT read from the table, and the reason is what happens on a miss. An
# index raises at import, so a registry that stopped carrying this key would stop the
# whole module importing -- and this is the DEFAULT backend, so that failure takes the
# path a user reaches with no configuration at all, for a name that has never varied. The
# bespoke Node adapters are indexed because their construction is already
# registry-driven; kiro's is not, and coupling it here would buy one fewer literal at the
# cost of a new import-time failure mode. Equality with the table is asserted by
# ``test_pid_lifecycle``, so the two cannot drift silently.
KIRO_CLI_BIN = "kiro-cli"
KIRO_CLI_SUBCMD = "acp"


# ── codex-acp (ACP_BACKEND_CODEX) ──
# A Node stdio server that boots the Codex app server and translates ACP onto its
# operations.  The ``codex`` CLI does not serve ACP itself -- it reads ``acp`` as a
# prompt -- so the adapter is the transport, not an optimization.  It takes no argv
# beyond its own path: any invocation enters stdio-server mode and blocks on stdin.
CODEX_ACP_BIN = ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_CODEX]
CODEX_ACP_NPM_PKG = ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_CODEX]
_CODEX_ACP_PKG_ENTRY = Path(CODEX_ACP_NPM_PKG, *NODE_ADAPTER_ENTRY_SEGMENTS)
# The hoisted-dependency completeness check every Node adapter shares: codex-acp
# imports @agentclientprotocol/sdk, so a root carrying the entry script without it
# dies at ESM import time -- after the child is spawned.
_CODEX_ACP_DEP_MARKER = launch_mod.ACP_SDK_DEP_MARKER
# Explicit override, spelled the way the adapter's own documentation spells it.
_ENV_CODEX_ACP_BIN = "CODEX_ACP_BIN"


class _KiroExecutableTrustError(RuntimeError):
    """Resolved Kiro CLI bytes are not approved for credential-bearing ACP."""


# Named by the path callers import it from: an error chain renders each raised class
# as ``module.qualname`` (``subagent._describe_exception``), and that text reaches the
# Subagents panel and tracebacks, so this module's name must not leak into it.
_KiroExecutableTrustError.__module__ = "kiro_crew.acp.client"


def _resolve_kiro_bin(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str | None:
    """Resolve the user's installed Kiro CLI, to be launched in place.

    Returns the installed binary's own path. Kiro Crew never copies the CLI and
    executes the copy: Kiro CLI 2.15+ dispatches subcommands by exec'ing a
    sibling executable resolved relative to its own path, which a copy into a
    private directory destroys.

    *environ* and *home* exist so a caller that also needs to REPORT the search
    can pin both to one reading of the environment. ``known_kiro_cli_dirs`` is a
    pure function of ``(platform, home, environ)``, so passing the same mapping
    here and to the diagnostic guarantees the directories named in a "not found"
    message are the directories that were actually searched. Both default to the
    live values, so every other caller is unchanged.
    """

    executable = resolve_kiro_cli(environ=environ, home=home)
    if not executable:
        return executable
    # Deferred to keep the low-level resolver import graph acyclic:
    # kiro_prerequisite imports sandbox helpers that this module also uses.
    from kiro_crew.kiro_prerequisite import snapshot_trusted_acp_executable

    try:
        if platform_compat.IS_WINDOWS:
            snapshot = snapshot_trusted_acp_executable(
                executable,
                platform_name="win32",
                environ=os.environ,
            )
        else:
            snapshot = snapshot_trusted_acp_executable(executable)
    except (OSError, ValueError) as exc:
        raise _KiroExecutableTrustError(str(exc)) from exc
    return snapshot.launch_path


async def _resolve_kiro_bin_for_spawn(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str | None:
    """Resolve the Kiro CLI path off the event loop.

    Plain ``to_thread`` — deliberately NOT shielded. The shield existed only to
    reclaim a snapshot descriptor when the caller was cancelled mid-resolve;
    with the CLI launched in place there is no resource to reclaim. Keeping the
    shield would actively harm: ``asyncio.shield`` only marks the inner task's
    result retrieved when the OUTER future is cancelled, but here it is the
    awaiting task that gets cancelled, so a resolve that raises concurrently
    (e.g. mid-self-update, when the binary transiently fails the runnable check)
    leaves an unretrieved exception. That surfaces at GC as "Task exception was
    never retrieved" and the gateway's asyncio handler writes a full false
    ASYNCIO UNHANDLED record to crash.log for an ordinary tab close.
    """

    return await asyncio.to_thread(_resolve_kiro_bin, environ=environ, home=home)


def kiro_cli_not_found_message(
    *,
    environ: Mapping[str, str],
    home: Path,
) -> str:
    """The one message for "the Kiro CLI is not where we looked".

    Every spawn path that resolves the CLI has to answer the same two-way
    question a bare name cannot: is the binary not installed, or is its install
    directory outside the search? :func:`env.describe_search_path` exists for
    that, and this function is where the ACP paths get it, so the client and the
    runtime cannot drift into reporting the same failure differently (the same
    single-source rule ``_resolve_kiro_bin_for_spawn`` and
    ``_drain_oversize_line`` are already shared under).

    Two properties are deliberate:

    * The directories are computed from the *caller's* ``environ`` and ``home``,
      not a fresh read. ``known_kiro_cli_dirs`` is a pure function of
      ``(platform, home, environ)``, so a caller that passes the same mapping it
      resolved against gets a message naming the directories actually searched —
      the same guarantee the Claude adapter carries.
    * The message never says "in PATH". Resolution also walks
      ``%LOCALAPPDATA%\\Kiro-Cli``, ``%ProgramFiles%\\Kiro-Cli`` and the
      ``KIROCREW_KIRO_BIN`` override, so "not found in PATH" sent a reader whose
      install was simply uncovered off to check the one thing that was not the
      question. It can send them further still: ``where kiro-cli`` finds nothing,
      the app bundle DOES contain a ``kirocrew.exe`` — Kiro Crew's OWN console
      script — and the conclusion invited is that the agent CLI has been renamed
      and this lookup left stale. Naming the searched directories and the
      override makes both wrong turns unavailable.

    Off the event loop when called from async code: expanding the inherited PATH
    touches the filesystem.
    """
    # Local import: ``kiro_prerequisite`` pulls in the sandbox plane that this
    # module also feeds, and the sibling ``snapshot_trusted_acp_executable``
    # import above is deferred for the same acyclicity reason.
    from kiro_crew.kiro_prerequisite import OFFICIAL_INSTALL_DOCS_URL

    searched_dirs = known_kiro_cli_dirs(sys.platform, home, environ)
    return (
        f"{KIRO_CLI_BIN} not found "
        f"({describe_search_path(os.pathsep.join(searched_dirs))}). "
        f"Install it from {OFFICIAL_INSTALL_DOCS_URL}, or point KIROCREW_KIRO_BIN "
        f"at the binary."
    )


_codex_acp_argv_cache: tuple[list[str] | None, str] | object = launch_mod._UNRESOLVED


def _resolve_codex_acp_bin() -> tuple[list[str] | None, str]:
    """Find the codex-acp Node entry script and the PATH searched for it.

    The shared ladder with this adapter's parameters; ``CODEX_ACP_BIN`` is the
    override.
    """
    return launch_mod._resolve_node_adapter_argv(
        bin_name=CODEX_ACP_BIN,
        override_env=_ENV_CODEX_ACP_BIN,
        vendored_entry=lambda: launch_mod._vendored_adapter_entry(
            _CODEX_ACP_PKG_ENTRY, _CODEX_ACP_DEP_MARKER
        ),
    )


def codex_acp_not_found_message(search_path: str) -> str:
    """The one wording for "the codex adapter is not installed".

    Both transports that spawn codex-acp raise it, so it is authored once: two
    copies drift, and this text is the operator's only instruction for fixing the
    install. *search_path* is what the resolver actually walked -- passed in
    rather than re-read, so a "searched ..." line can never name a directory the
    search skipped.
    """
    return (
        f"{CODEX_ACP_BIN} not found "
        f"({describe_search_path(search_path)}). Install it with "
        f"'npm i -g {CODEX_ACP_NPM_PKG}' (or add it as a project "
        f"dependency), or set {_ENV_CODEX_ACP_BIN} to its entry script. "
        f"The 'codex' CLI alone does not serve ACP."
    )


#: The data-root override variables the identity store consults, DERIVED from
#: ``identity_stores.IDENTITY_STORE_ROOTS`` so this set cannot drift from the table
#: that actually resolves the store. Today: ``XDG_DATA_HOME`` (POSIX),
#: ``LOCALAPPDATA`` and ``APPDATA`` (Windows). macOS rows carry no env var (fixed
#: anchor), so nothing is scrubbed for them. Consumed by
#: :func:`_apply_pod_home_remap`, which pops each one from a pod child's env.
IDENTITY_STORE_ROOT_ENV_VARS: frozenset[str] = frozenset(
    root.env_var for root in IDENTITY_STORE_ROOTS if root.env_var
)


#: Credential POINTERS: variables whose VALUE is an absolute path or URL the AWS
#: SDK chain dereferences to obtain credentials. Popped from a pod child's env by
#: :func:`_apply_pod_home_remap`.
#:
#: Why this is an explicit list and not a derivation. ``IDENTITY_STORE_ROOT_ENV_VARS``
#: is derived from ``identity_stores.IDENTITY_STORE_ROOTS`` and correctly does NOT
#: cover these: a store ROOT is a directory the product's own layout hangs off, while
#: these name a credential FILE or a credential ENDPOINT directly. No table in this
#: repo enumerates them, so deriving them would mean inventing one whose only
#: consumer is this scrub -- a list with extra steps. They are enumerated here, with
#: the rule for extending it stated rather than implied: a variable belongs here when
#: its value is a LOCATION that yields credentials when followed.
#:
#: Why not ``sandbox._SENSITIVE_ENV_PREFIXES``, which is the repo's one global scrub.
#: That set covers ``AWS_SECRET`` / ``AWS_SESSION`` -- variables that CARRY a secret --
#: and deliberately stops there, because the standard sandbox tier leaves the real
#: ``~/.aws`` visible so the AWS CLI and ``credential_process`` keep working for
#: non-pod agent turns. Adding pointers there would break that supported path
#: everywhere to fix a pod-only exposure. The exposure IS pod-only: outside a pod the
#: pointer and the fence agree about where credentials live, while inside one ``HOME``
#: moves and ``.aws/config`` / ``.aws/credentials`` / ``.aws/cli`` are empty-masked
#: under the new home -- so an inherited ABSOLUTE pointer at the host path walks
#: around the relocation entirely and the agent reads the operator's real credentials
#: by dereferencing it. One philosophy, two scopes: secrets are scrubbed globally,
#: pointers are scrubbed where the thing they point at has been relocated.
#:
#: REMOVED rather than re-anchored, for the same reason as the store roots: deleting
#: the variable lets the SDK's own ``$HOME``-relative default resolve under the pod
#: home (where the masks apply), and a wrong re-anchored value would fail OPEN.
CREDENTIAL_POINTER_ENV_VARS: frozenset[str] = frozenset(
    {
        # Credential/config FILES the SDK reads directly.
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        # An OIDC token file exchanged for role credentials (web-identity flow).
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        # The container credential provider: a URL the SDK GETs for credentials,
        # plus the bearer that authorizes that GET. Not a filesystem path, so no
        # mask or HOME remap can reach any of them -- which is exactly why they
        # have to be dropped from the env rather than fenced.
        #
        # The authorization token has TWO spellings and both must go. The ``_FILE``
        # form names a file holding the bearer; the bare form carries the bearer
        # IN THE VALUE, in plain text. Per the SDK reference the bare form is the
        # documented alternative used when ``_FILE`` is unset (and is what Lambda
        # SnapStart sets), so scrubbing only ``_FILE`` leaves the strictly worse
        # variable in the child's environment: a live bearer the agent can read
        # straight out of ``env`` with no file to open and no path to fence.
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    }
)


def _apply_pod_home_remap(env: dict[str, str], *, pod_home_remap: bool) -> dict[str, str]:
    """Remap *env*'s ``HOME`` to the pod's own OAuth-grant tree, for a
    pod-spawned kiro-cli child ONLY. Mutates *env* in place and returns it.

    Gated on BOTH the pod marker (``KIROCREW_POD``, set by
    ``pod.runtime.build_pod_env`` for the whole pod gateway) and
    *pod_home_remap* (membership in ``ACP_BACKENDS_POD_HOME_REMAP`` --
    harness-parity H6/H7, never a bare backend-name comparison), so this touches
    nothing outside a pod and nothing for a harness whose credentials do not
    follow ``$HOME``. That set is deliberately NOT
    ``ACP_BACKENDS_INTERNAL_SANDBOX``: the two answer different questions --
    "does this harness carry its own OS sandbox?" versus "does relocating this
    harness's HOME move its credential store?" -- so reusing the sandbox set
    would hand a harness added there for sandbox reasons credential-relocation
    semantics it never opted into.

    The marker is compared EXACTLY to ``"1"`` rather than tested for
    truthiness. Every non-empty string is truthy in Python, so an inherited
    ``KIROCREW_POD=false`` or ``KIROCREW_POD=0`` would otherwise remap ``HOME``
    for a child that is not in a pod at all -- the inverse of what the value
    says.

    kiro-cli derives its MCP OAuth artifact directory
    (``mcp_grant.kiro_oauth_cache_dir()``) from the SPAWNED PROCESS's real
    ``$HOME`` -- there is no env var that
    relocates just that one subtree (see ``config.paths.kiro_oauth_cache_home``
    for why ``KIRO_HOME`` does not help either) -- so the only way to make
    kiro-cli's OWN writes land in the pod's tree is to remap the child's
    ``HOME`` at spawn time. ``KIROCREW_OS_HOME`` names the SAME directory
    ``config.paths.kiro_oauth_cache_home()`` resolves for the pod's ``mcp_grant``
    reads, which is what keeps kiro-cli's writer and every ``mcp_grant`` reader
    looking at one tree instead of two independent derivations of "where do
    grants live" (see ``mcp_grant.kiro_oauth_cache_dir``'s docstring for the
    split this closes).

    A remap absent ``KIROCREW_OS_HOME`` (the marker set with no pod-scoped
    directory to point at -- a malformed pod env, or a caller that set the
    marker without the directory) leaves ``env`` untouched: an unset ``HOME``
    on a spawned child would break far more than OAuth grants, so the fail
    mode here is "kiro-cli reads the real host home", the status quo, not a
    broken spawn.

    Three obligations come with moving ``HOME``, each closed here:

    * kiro-cli's own sign-in must keep working. ``pod.runtime._seed_pod_os_home``
      mirrors the AGENT RUNTIME's identity store (``~/.local/share/kiro-cli`` and
      its per-platform siblings, derived from ``identity_stores``) into this tree
      at pod boot, which is where the harness actually resolves its access token.
      The host's ``.aws/sso/cache`` is NOT copied: that staging existed in an
      earlier revision and was deleted, so a pod's ``.aws/sso/cache`` starts empty
      and holds only grants the pod itself mints.
    * ``USERPROFILE`` moves WITH ``HOME`` (Windows spelling of the same
      concept) so a Windows pod does not read one remapped path and the other
      unremapped.
    * **AWS file-based credentials deliberately do NOT follow the child into the
      pod.** ``AWS_CONFIG_FILE`` / ``AWS_SHARED_CREDENTIALS_FILE`` default to
      ``$HOME/.aws/{config,credentials}`` when unset, so a remapped ``HOME``
      sends the credential chain at the pod's own tree. An earlier revision
      pinned both variables back to the REAL home so a pod agent turn could
      still reach the operator's profiles. That pin is REMOVED, because naming
      those files in the child environment is itself the leak: ``security.py``
      matches command TEXT and performs no variable expansion, so the exported
      name is a working alias for a path the sensitive-path fence refuses by
      name -- and the alias is retrievable through an unbounded set of
      spellings (``$VAR``, ``${VAR}``, ``%VAR%``, ``$env:VAR``,
      ``os.environ['VAR']``, ``$(printenv VAR)``, ``eval``, indirect expansion,
      a helper script). Closing one spelling at a time cannot close the class:
      a text matcher cannot see through them.
      which is the only fix that does not depend on out-matching command
      substitution.

      What this costs, stated rather than implied: **an ACP agent turn inside a
      pod has no inherited AWS credentials on any path.** Both legs are closed,
      and an earlier revision of this comment got the second one wrong:

      * FILE credentials: removing the exports means ``~/.aws/{config,credentials}``
        resolves under the remapped (empty) pod home, so a profile that lives only
        in a file -- including a ``credential_process`` profile -- does not resolve.
      * ENVIRONMENT credentials: ``sandbox.scrub_agent_subprocess_env`` scrubs
        ``_SENSITIVE_ENV_PREFIXES``, which includes ``AWS_SECRET`` and
        ``AWS_SESSION``, from every Kiro/ACP child. So ``AWS_SECRET_ACCESS_KEY``
        and ``AWS_SESSION_TOKEN`` never reach the agent turn even when the
        operator has them. ``AWS_ACCESS_KEY_ID`` survives (no ``AWS_ACCESS``
        prefix), but a key id without its secret is not a credential.

      ``build_pod_env`` keeps ``AWS_*``, which does NOT mean env-credentialed
      turns are unaffected: ``build_pod_env`` shapes the pod GATEWAY's
      environment, and the ACP child is scrubbed AFTER it, so the two statements
      are about different processes. The scrub is the stronger posture, and it is
      the intended one for a throwaway instance whose whole purpose is to not
      hold machine-level credentials.

      An operator who needs AWS from inside a pod therefore cannot get it by
      exporting credentials into their shell; that is a deliberate property of the
      agent-subprocess scrub, not something this function can or should undo.

      An operator who sets either pointer variable in their OWN environment has it
      REMOVED here too, which is the half a later round corrected: whose file the
      variable names does not change what the pod's agent obtains by dereferencing
      it, and an absolute host pointer walks around the ``HOME`` relocation
      entirely. ``CREDENTIAL_POINTER_ENV_VARS`` carries the whole family, so the
      scrub below covers an inherited pointer and a manufactured one alike.
    """
    if not pod_home_remap or env.get("KIROCREW_POD") != "1":
        return env
    os_home = env.get("KIROCREW_OS_HOME")
    if not os_home:
        return env
    env["HOME"] = os_home
    env["USERPROFILE"] = os_home
    # Remapping HOME alone is NOT enough. The identity store's root is
    # ``$HOME``-relative only when no OVERRIDE is set: ``identity_stores``
    # resolves each store from ``StoreRoot.env_var`` first (``XDG_DATA_HOME`` on
    # POSIX, ``LOCALAPPDATA`` / ``APPDATA`` on Windows; macOS anchors are fixed and
    # carry no env var). An inherited override pointing at a host path therefore
    # made the pod's kiro-cli read AND WRITE the HOST identity store, so its
    # sign-in state survived ``pod down`` -- the exact escape this remap exists to
    # prevent, reached around the side.
    #
    # REMOVED rather than re-anchored, deliberately. Deleting the variable lets the
    # product's own ``$HOME``-relative default resolve under ``os_home``, which is
    # the behaviour already tested and already staged into; re-anchoring would
    # invent a second spelling of "where the store lives" that has to stay in sync
    # with ``identity_stores`` forever, and a wrong value fails OPEN (a live store
    # somewhere unintended) instead of closed. The set is DERIVED from the store
    # table rather than restated, so a platform or product added there is scrubbed
    # here without a second edit.
    for var in IDENTITY_STORE_ROOT_ENV_VARS:
        env.pop(var, None)
    # Credential pointers, same removal for a different reason: their value is a
    # LOCATION that yields credentials when followed, and an operator-set absolute
    # one still names the HOST's file after ``HOME`` has moved. See
    # ``CREDENTIAL_POINTER_ENV_VARS`` for why this set is explicit and why it is not
    # folded into the repo's global secret scrub.
    for var in CREDENTIAL_POINTER_ENV_VARS:
        env.pop(var, None)
    return env


#: The executable name inside a toolbox kiro-cli bundle. The installed
#: ``kiro-cli`` is frequently a SHIM that prefers ``exec aim sandbox --client
#: kiro-cli "$@"`` and falls back to ``exec "$KIRO_CLI_PATH"``, which it derives
#: as ``<bundle root>/kiro-cli`` from its own resolved symlink chain. Naming the
#: same executable here keeps :func:`_kiro_cli_bundle_binary` single-sourced with
#: the shim's own fallback instead of hardcoding one install layout.
_KIRO_CLI_BUNDLE_EXECUTABLE = "kiro-cli"


def _kiro_cli_bundle_binary(
    executable: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """The bundle binary the kiro-cli shim's OWN fallback would exec, or ``None``.

    Mirrors the shim's two-step resolution rather than inventing a third one:
    an executable ``KIRO_CLI_PATH`` wins outright (the shim honors a pre-set
    value before it computes anything), otherwise the shim resolves its own
    symlink chain, takes ``<dirname>/..`` as the bundle root and execs
    ``<bundle root>/kiro-cli``.

    Returns ``None`` -- meaning "spawn what was resolved, unchanged" -- for every
    case where the swap is not provably available: *executable* IS already the
    bundle binary (the computed candidate resolves back to it), the candidate
    does not exist, or it is not executable. A caller therefore never has to
    handle a path that cannot be spawned, and a host with no toolbox bundle keeps
    the status quo instead of failing at spawn time.
    """
    env = os.environ if environ is None else environ
    pinned = env.get("KIRO_CLI_PATH")
    if pinned and os.path.isfile(pinned) and os.access(pinned, os.X_OK):
        return pinned
    try:
        real = os.path.realpath(executable)
        candidate = os.path.join(
            os.path.dirname(os.path.dirname(real)), _KIRO_CLI_BUNDLE_EXECUTABLE
        )
    except OSError:  # pragma: no cover - defensive; a spawn must not fail on this
        return None
    if os.path.realpath(candidate) == real:
        return None  # already the bundle binary; the shim is not in the chain
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


#: The path tail a macOS ``.app`` install puts the kiro-cli binary at. A multi-call
#: kiro-cli locates the sibling it execs by searching for this tail in its OWN
#: executable path, so it is the one layout where resolving a symlink is safe.
_MACOS_APP_BUNDLE_SUFFIX = ".app"
_MACOS_APP_BUNDLE_TAIL = ("Contents", "MacOS")


def _kiro_cli_app_bundle_target(executable: str) -> str | None:
    """The macOS ``.app`` kiro-cli binary *executable* symlinks to, or ``None``.

    Verifies the LAYOUT, not just that the link resolves: each condition is one the
    target must satisfy for the child's own sibling search to succeed afterwards.
    Same basename keeps an ``argv[0]``-dispatching multiplexer out; the
    ``<basename>-`` sibling proves this is the multi-call binary.
    """
    try:
        real = os.path.realpath(executable)
        if real == executable:
            return None
        name = os.path.basename(real)
        if name != os.path.basename(executable):
            return None
        macos_dir = os.path.dirname(real)
        contents_dir = os.path.dirname(macos_dir)
        app_dir = os.path.dirname(contents_dir)
        tail = (os.path.basename(contents_dir), os.path.basename(macos_dir))
        if tail != _MACOS_APP_BUNDLE_TAIL:
            return None
        if not os.path.basename(app_dir).endswith(_MACOS_APP_BUNDLE_SUFFIX):
            return None
        if not os.path.isfile(real) or not os.access(real, os.X_OK):
            return None
        siblings = os.listdir(macos_dir)
    except OSError:  # pragma: no cover - defensive; a spawn must not fail on this
        return None
    if not any(entry.startswith(f"{name}-") for entry in siblings):
        return None
    return real


def apply_pod_bundle_spawn(
    argv: list[str],
    *,
    backend: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[str], bool]:
    """Resolve a pod child's kiro-cli spawn: which binary, and who sandboxes it.

    Returns ``(argv, delegate_internal_sandbox)``. The second element is what
    callers pass as ``wrap_argv``'s ``is_kiro_cli``, so the binary choice and the
    sandbox-ownership choice cannot drift apart -- they are one decision with one
    cause, made here once for both ACP transports rather than restated in each.

    **Outside a pod this is a no-op by construction**: *argv* is returned
    unchanged and ``delegate_internal_sandbox`` is plain membership in
    ``ACP_BACKENDS_INTERNAL_SANDBOX``, byte-identical to what both call sites
    computed inline before. The exception is stated POSITIVELY (pod condition
    true) and gated on exactly the conditions that make
    :func:`_apply_pod_home_remap` fire -- the pod marker compared exactly to
    ``"1"``, membership in ``ACP_BACKENDS_POD_HOME_REMAP``, and a
    ``KIROCREW_OS_HOME`` to point at -- never on a backend negation, so a harness
    added to a set for some other reason cannot inherit this behaviour by
    accident (harness-parity H6/H7).

    **Why a pod child must not run the shim.** The remap gives the child a
    pod-owned ``HOME`` so kiro-cli's OAuth grants die with the pod. The installed
    ``kiro-cli`` is a shim that prefers ``aim sandbox``, and toolbox's sandbox
    builds its mount plan around the REAL user home: under a remapped ``HOME`` it
    fails to construct at all, exiting before kiro-cli starts (observed as
    ``Failed to spawn child process: Device or resource busy (os error 16)``, and
    inside a pod as ``toolbox: Unable to run aim: Command "aim" doesn't appear to
    be associated with any tool`` followed by a broken ACP pipe). Staging state
    into the pod home does not help -- an empty os-home, one carrying a
    ``.toolbox`` symlink, and one carrying a real ``.toolbox`` skeleton all fail
    identically -- because the obstacle is toolbox's mount plan, not a file the
    child cannot find. The bundle binary the shim itself falls back to has no
    such dependency: under the same remapped ``HOME`` it starts and reaches its
    normal "not logged in" state, which is precisely the state
    ``pod.runtime._seed_pod_os_home``'s staged SSO token answers.

    **So Kiro Crew's own sandbox takes over for that child.** Skipping the
    seatbelt/delegation is only sound while kiro-cli's internal sandbox actually
    runs; bypassing the shim means it does not, so ``delegate_internal_sandbox``
    goes ``False`` and ``wrap_argv`` wraps the child in Crew's launcher instead.
    The substitution is per-pod-child and never reaches a host session.

    If the bundle binary cannot be located, one further layout resolves: a symlinked
    ``argv[0]`` whose target :func:`_kiro_cli_app_bundle_target` verifies as a macOS
    ``.app`` binary. Its exec'd sibling shares that directory -- which is why the
    fixed-depth candidate above misses it -- so the real name puts the sibling beside
    the child instead of under the remapped ``HOME``.
    ``delegate_internal_sandbox`` goes ``False``, though not for the swap's reason:
    no shim is in this chain, and delegating would instead skip Crew's seatbelt for
    an internal sandbox whose behaviour under the pod's remapped ``HOME`` Crew cannot
    verify -- that remap already broke the other sandbox in this chain. Uncertainty
    resolves toward our own layer, the direction
    :func:`sandbox.kiro_internal_sandbox_enabled` states for itself. Failing both,
    the pair degrades to the status quo (shim, internal sandbox) rather than spawning
    something unlaunchable; a pod whose child cannot bootstrap is meant to be refused
    loudly at ``pod up``, not papered over here.
    """
    delegate = backend in ACP_BACKENDS_INTERNAL_SANDBOX
    env = os.environ if environ is None else environ
    if env.get("KIROCREW_POD") != "1":
        return argv, delegate
    if backend not in ACP_BACKENDS_POD_HOME_REMAP or not env.get("KIROCREW_OS_HOME"):
        return argv, delegate
    if not argv:  # pragma: no cover - defensive; callers always pass argv[0]
        return argv, delegate
    bundle = _kiro_cli_bundle_binary(argv[0], environ=env)
    if bundle is None:
        bundle = _kiro_cli_app_bundle_target(argv[0])
    if bundle is None:
        return argv, delegate
    return [bundle, *argv[1:]], False


def finish_suspended_spawn(process: asyncio.subprocess.Process, pid: int, *, label: str) -> None:
    """Apply the Windows resource ceiling to a just-spawned child, then resume it.

    **Call this from an executor, never inline on the event loop.** On Windows it
    reads the config file (through ``apply_windows_resource_ceiling``) and walks
    two Toolhelp snapshots — the process table for the ownership check and the
    system-wide THREAD table to resume — so a slow config store or a loaded
    machine would otherwise stall every other session on that loop. Both ACP
    spawn sites wrap it in ``run_in_executor(subprocess_executor(), ...)``. It
    stays synchronous rather than becoming a coroutine because every step is
    blocking ctypes work with no await point to offer.

    Both ACP spawn sites (:meth:`AcpClient._spawn` and ``AcpRuntime._spawn``)
    create the session host with ``creationflags |=
    platform_compat.CREATE_SUSPENDED`` and call this immediately afterwards. On
    POSIX every step is a no-op — ``CREATE_SUSPENDED`` is 0 there, so the child
    was never suspended — which keeps one code path for both platforms.

    Why suspended: ``cgroup_scope_argv`` is a no-op on Windows (no systemd), so
    without this the agent and every MCP server it spawns would run with NO
    fork-bomb and NO memory-DoS ceiling. A Job object cannot be an argv prefix,
    so it must be attached to a live pid — and job membership covers a member's
    FUTURE descendants only. Attaching to an already-running kiro-cli would
    therefore leave a window in which it could spawn an MCP server that escapes
    the ceiling. ``CREATE_SUSPENDED`` closes that window by construction: the
    child has not executed a single instruction, so it provably has no
    descendants. Assign the job, then resume.

    A resume failure is FATAL, but only when the child is actually there: a
    process that exists yet cannot be resumed is alive-but-frozen, and letting it
    masquerade as a running agent would hang the session on the ACP handshake
    with no diagnosis. Kill it and raise instead. If the pid is already gone
    there is nothing frozen to worry about — it exited on its own — so note it
    and let the handshake surface the real error.

    The ceiling itself fails SOFT (``apply_windows_resource_ceiling`` logs a
    SECURITY warning and returns False): a missing ceiling must not break the
    gateway. Only the resume may abort the spawn, which is why it runs from a
    ``finally`` — a raising ceiling must still leave the child resumed or killed,
    never frozen.

    The two DESTRUCTIVE steps are gated on confirmed ownership: the pid's parent
    must be this process. A Job object would impose a process and memory ceiling
    on a stranger, and the unresumable branch KILLS what it is holding, so
    neither may ever act on a pid we did not create. The resume itself is NOT
    gated, because ``ResumeThread`` on a thread that is not suspended is a
    documented no-op (its suspend count is already 0) — and leaving our own child
    frozen would hang the session forever on the handshake with no diagnosis.
    That asymmetry is deliberate: an unconfirmed pid loses only its ceiling,
    which already fails soft by contract, while nothing can wedge or die by
    mistake.
    """
    owned = not platform_compat.IS_WINDOWS or platform_compat.get_ppid(pid) == os.getpid()
    try:
        if owned:
            apply_windows_resource_ceiling(pid)
        else:
            logger.debug(
                "PID %d is not a confirmed child of this process; skipping the Windows "
                "resource ceiling rather than bounding a foreign process",
                pid,
            )
    finally:
        if platform_compat.IS_WINDOWS and not platform_compat.resume_process_main_thread(pid):
            if owned and platform_compat.pid_exists(pid):
                logger.error(
                    "Could not resume suspended %s (PID %d); killing it rather than "
                    "leaving a frozen process that looks like a live agent",
                    label,
                    pid,
                )
                try:
                    process.kill()
                except Exception:
                    logger.debug("kill of unresumable child failed", exc_info=True)
                raise AcpError(
                    f"failed to resume {label} (PID {pid}) after applying Windows "
                    f"Job object resource limits"
                )
            logger.debug(
                "Nothing to resume for PID %d — it is gone, or not ours to kill; the "
                "handshake will report the real failure",
                pid,
            )


def _exit_origin(code: int | None, sent: tuple[int, str] | None) -> str:
    """Name the signal behind a negative exit code and whether this client sent it.

    A signal this client did not send is not necessarily an outside one: the
    child raises SIGSEGV or SIGABRT on itself, and another Crew path (the
    orphan sweep) signals it without touching ``sent``. The client cannot tell
    those apart, so it claims only that it did not send the signal.
    """
    if code is None or code >= 0:
        return ""
    try:
        name = signal.Signals(-code).name
    except ValueError:
        name = f"signal {-code}"
    who = f"killed by Crew ({sent[1]})" if sent and sent[0] == -code else "not sent by this client"
    return f", {name}, {who}"


def _launch_tools() -> LaunchTools:
    """The launch tail's collaborators, as THIS module binds them right now.

    Read at each launch, so a test that rebinds one of these names on
    ``kiro_crew.acp.client`` reaches the launch this client starts.
    """
    return LaunchTools(
        logger=logger,
        platform_compat=platform_compat,
        agent_scratch=agent_scratch,
        apply_pod_bundle_spawn=apply_pod_bundle_spawn,
        forward_ssh_auth_sock=_forward_ssh_auth_sock,
        wrap_argv_async=wrap_argv_async,
        wrap_argv=wrap_argv,
        wrapped_by_crew_sandbox=wrapped_by_crew_sandbox,
        cgroup_scope_argv=cgroup_scope_argv,
        augmented_path=augmented_path,
        scrub_agent_subprocess_env=scrub_agent_subprocess_env,
        apply_pod_home_remap=_apply_pod_home_remap,
        browser_session_env=browser_session_env,
        browser_socket_env=browser_socket_env,
        inject_xdist_auto_cap=inject_xdist_auto_cap,
        bind_voice_safe_agent_workspace_async=bind_voice_safe_agent_workspace_async,
        create_subprocess_limited=create_subprocess_limited,
        agent_env_overlay=agent_env_overlay,
    )


def _prepare_spawn_workspace(self: AcpClient) -> None:
    """Create the session's work dir, then snapshot its spec for the detector.

    Two blocking reads folded into ONE executor hop, and the fold is the
    point: the mkdir was already awaited here, so carrying the snapshot with
    it means the unresolved-ref detector adds no suspension point to any
    backend's construction path -- kiro-cli's included, which is what
    harness-parity H13 protects. Nothing is deferred or reordered: the mkdir
    still runs first and still raises, because the spawn genuinely cannot
    proceed without the directory.

    The snapshot half is best-effort and comes SECOND for that reason. It
    cannot fail the spawn (see :meth:`_read_mcp_ref_spec`), and it is skipped
    outright when the mkdir raises -- a session with no work dir has no
    diagnostic to report.
    """
    ensure_directory(self._work_dir)  # 0700 only when it is in the data home
    # A stored skill-view name is never the agent to launch: it maps back to
    # the agent it was built from, whose current view the spawn prepares.
    # Folded into this hop for the reason above (it may read one sidecar).
    from kiro_crew.acp.skill_projection import RetiredSkillView, source_agent_name

    try:
        self._agent = source_agent_name(self._agent)
    except RetiredSkillView as exc:
        raise AcpError(str(exc)) from exc
    self._mcp_ref_spec = self._read_mcp_ref_spec()


def _agent_env_for_spawn(self: AcpClient) -> dict[str, str]:
    """``agent.env`` for the spawn in progress, read at most once per spawn.

    A harness's tool-gate read-back and the launch tail both take it from
    here, so a config save between them cannot point the child at a config
    the read-back never checked. Blocking on the first call (reads config);
    callers run it off the loop.
    """
    if self._agent_env_snapshot is None:
        self._agent_env_snapshot = agent_env_overlay()
    return self._agent_env_snapshot


def _apply_session_identity_env(self: AcpClient, env: dict[str, str]) -> None:
    """Put this session's identity on the child's environment.

    The two values are NOT symmetric, and the asymmetry is the point.

    The TOKEN is carried unconditionally, because a warm-pool client is spawned
    with no session key at all — that is what makes it poolable — and a child's
    environment is fixed at spawn. A token withheld there can never be added
    later, so conditioning it on the key would strip it from exactly the children
    that need it most: the pooled ones, whose identity has to survive a
    ``rekey()``. It survives because the token is minted in ``__init__`` and its
    signed mapping is (re)published when the session key becomes known, so a
    child holding a token and no key still resolves — through the mapping — to
    whichever session claimed the process.

    The KEY is conditioned, because it is a value and not a pointer: an inherited
    one names a session this client is not serving, and the resolver would read
    it as identity. The token cannot go stale the same way — its mapping is
    rewritten on every rekey, so the same token names the current owner — which
    is also why the resolver prefers it.

    Overwriting rather than popping is what keeps a reused ``env`` mapping honest:
    whatever token it arrived with, it leaves carrying THIS client's.

    The process environment is the right carrier HERE and only here: one
    ``AcpClient`` drives one child serving one session, so the process names
    exactly one session. On the shared runtime the same env would name whichever
    session claimed the process, which is why identity travels per-element there
    (``AcpRuntime._own_stub_session`` and the mirror projections).

    Mutates *env* in place, matching the surrounding block in :meth:`_spawn`.
    """
    if self._stub_session_token:
        env[STUB_SESSION_TOKEN_ENV] = self._stub_session_token
    else:
        # No token to name this client with (a test double, a build that could
        # not mint one). Carrying an empty value would present a token the
        # mapping can never verify, which the resolver would try FIRST.
        env.pop(STUB_SESSION_TOKEN_ENV, None)
    if self._session_key:
        env["KIROCREW_SESSION_KEY"] = self._session_key
    else:
        env.pop("KIROCREW_SESSION_KEY", None)


def _resolve_spawn_agent_argv(self: AcpClient) -> str:
    """The ``--agent`` value for the launch, converting a refusal to ``AcpError``.

    ``spawn_agent`` resolves a projected name/stem but raises a bare
    ``ValueError`` for an ``errors`` entry (an authored ``@kirocrew-core``
    exclusion, or the "its spec could not be read" refusal row). Raised raw out
    of ``_spawn`` that ``ValueError`` escapes ``ensure_ready``'s transport
    ladder (``AcpToolGateUnroutable``, ``(AcpTimeoutError, AcpError, OSError)``)
    uncaught, skipping ``_cleanup_failed_live_spawn``/``_reset_state`` and
    losing the actionable sentence -- the exact class the sandbox-floor
    boundary above documents as already fixed once. Convert it to ``AcpError``
    so the refusal takes the same cleanup+requeue path, carrying its own
    message.
    """
    assert self._native_skill_projection is not None
    try:
        return self._native_skill_projection.spawn_agent(self._agent)
    except ValueError as exc:
        raise AcpError(str(exc)) from exc
