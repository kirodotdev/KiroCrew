"""OS-level sandbox for agent child processes.

Hides sensitive credential paths (``~/.aws``, ``~/.gnupg``, etc.) from the
kiro-cli subprocess tree and exposes ``~/.ssh/known_hosts`` while hiding
other SSH files (keys, config, etc.), using platform-native isolation:

- **Linux**: fork → ``unshare(CLONE_NEWUSER)`` → parent writes identity
  UID/GID map → ``unshare(CLONE_NEWNS)`` → bind-mount empty dirs → exec.
  The child retains the real UID so all toolchains work normally.
- **macOS**: ``sandbox-exec`` with a Seatbelt profile that denies reads

The parent KiroCrew process is completely unaffected — isolation applies
only to the spawned child.  Falls back gracefully to no sandbox when the
OS mechanism is unavailable (logged as warning).

Config: ``"sandbox": "auto" | "off"`` in ``~/.kiro/crew/config.json``.
``"auto"`` (default) uses namespace sandbox on Linux, seatbelt on macOS.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import errno
import functools
import hashlib
import json
import logging
import os
import re
import select
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.identity_stores import AUTH_SQLITE_DB, AUTH_SQLITE_SIDECAR_SUFFIXES
from kiro_crew.pinned_fs import fd_real_path
from kiro_crew.platform import current_context

try:
    import resource as _resource_mod
except ImportError:  # non-POSIX (Windows)
    _resource_mod = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from concurrent.futures import ThreadPoolExecutor
    from typing import Any

logger = logging.getLogger(__name__)

# Launcher scripts and seatbelt profiles are read exactly once at child exec.
# Any file older than this threshold is garbage regardless of PID liveness.
_LAUNCHER_MAX_AGE_SECONDS = 3600

# Bind-mount SOURCES staged by the namespace launcher (empty dirs/files bound
# over credential paths, plus the SSH shadow dir). The kernel pins a source for
# the mount's lifetime, so the launcher cannot unlink them and they orphan when
# the sandboxed process exits. The launcher names them with this prefix plus
# its own pid ("kirocrew_sb_<pid>_..."); the pid is the liveness key the
# janitor probes. The age threshold backstops recycled pids for the removals
# that stay safe against a live mount (plain files and empty dirs).
_MOUNT_SOURCE_PREFIX = "kirocrew_sb_"
_MOUNT_SOURCE_MAX_AGE_SECONDS = 24 * 3600

# Pin-scan stabilization budget: a mount-namespace holder can fork a successor
# and exit between the /proc listing and its own mountinfo read, so a pass
# that observed a vanish rescans newly appeared pids; past this many passes
# coverage is reported as unproven instead of looping.
_PIN_SCAN_MAX_PASSES = 3


class _PinScanCoverage:
    """Whether the pin scan read every task that could hold a sandbox source.

    Filled by :func:`_mount_pinned_source_names` beside its host-wide
    ``complete`` flag. That flag drops for reasons that cannot involve a
    sandbox source -- another user's unreadable task, or one departing on the
    final pass -- and on a busy host it can stay down indefinitely, which is
    how the directory class came to accumulate without bound. ``covered`` asks
    the narrower question the gate needs: was every task that could hold a
    source this uid staged read, and did none depart between the final
    listing and its read? Those are this uid's tasks (the sandboxed child
    keeps this uid with NO_NEW_PRIVS), the overflow uid's (how a nested user
    namespace stats) and root's (root can ``nsenter`` any namespace) -- so a
    ``hidepid`` procfs, which hides root's tasks, clears it too. A task that
    departs on the FINAL pass may have handed its namespace to a child forked
    after that listing, which only a re-listing could have seen and none
    followed, so such a departure clears ``covered``; one on an earlier pass
    was followed by a re-listing and is accounted for. When ``covered`` holds
    the pinned set is authoritative for every possible holder.
    """

    __slots__ = ("covered",)

    def __init__(self) -> None:
        self.covered = True


def _task_uid(proc_root: str, name: str) -> int | None:
    """The uid ``/proc/<name>`` stats as, or None once it is gone."""
    try:
        return os.stat(os.path.join(proc_root, name)).st_uid
    except OSError:
        return None


# The overflow uid: what /proc/<pid> stats to for a process whose uid has no
# mapping in the reader's user namespace. Such a holder CAN be binding our
# sources, so it is a coverage gap, not a foreign user. The value is a
# writable sysctl, so it is read from the host; an unreadable sysctl answers
# None, and the scan then treats EVERY unreadable non-own uid as a gap.
_OVERFLOW_UID_SYSCTL = "/proc/sys/kernel/overflowuid"


@functools.lru_cache(maxsize=1)
def _overflow_uid() -> int | None:
    try:
        return int(Path(_OVERFLOW_UID_SYSCTL).read_text().strip())
    except (OSError, ValueError):
        return None


# Legacy sandbox launcher directory (before migration to <config_dir>/run/).
_LEGACY_LAUNCHER_DIR = "/tmp"

# Sensitive directories to hide from the agent subprocess tree.
# "strict" mode hides all; "standard" mode only hides non-workflow dirs.
#: The cache leaf, mirroring ``policy_distribution.CACHE_DIR_LEAF``; spelled here so
#: this module needs no import from the governance engine.  Pinned equal by
#: ``test_governance_distribution``.
_POLICY_CACHE_LEAF = "policy_cache"
#: Gateway-only runtime subtree that holds authenticated macOS decoder images.
#: The gateway opens these outside the agent sandbox; every sandbox mode must
#: hide the whole subtree while the image is still writable and through spawn.
_VOICE_RUNTIME_LEAF = os.path.join("run", "voice-runtime")
#: The data home the ``$HOME``-relative entries below assume.
_CREW_HOME_DEFAULT = ".kiro/crew"

#: Both data-home spellings every crew-relative rule below has to cover: a host that
#: has not run the ``~/.kirocrew`` -> ``~/.kiro/crew`` migration still holds the real
#: bytes at the legacy path, and ``config_dir()`` can resolve to either.
_CREW_HOME_PREFIXES: tuple[str, ...] = (".kiro/crew", ".kirocrew")

# ── The crew data home's governance tree, reconciled with security.py ──
#
# ``security.sensitive_home_dirs()`` is the AGENT-TOOL gate: it is what
# ``is_sensitive_path`` refuses for a file_read/file_write tool call. The lists in this
# module are a SEPARATE, OS-level gate — a spawned ``python -c`` or a shell command does
# an ``open()`` that never routes through the tool gate, so a path fenced only there is
# readable through any sandbox mode. Two entries (``.vault``, ``policy_cache``) were
# already carried in both; the rest of the governance tree was not, which left the
# ceiling itself (``security_policy.json``) readable and WRITABLE from an app lifecycle
# script, a script hook, or a command cron.
#
# Reconciling the two is NOT a union, and the reason is specific: Kiro Crew's own MCP
# servers (``mcp_core``, ``mcp_cron``, ``mcp_computer``) are spawned by kiro-cli UNDER
# this launcher and share the agent's mount namespace, as does a script cron's
# ``boot_platform()``. Whatever they open at OS level cannot be masked. So each crew-home
# leaf gets one of three dispositions:
#
#   HIDDEN    Nothing that runs inside the sandbox reads it. An empty dir/file is
#             bind-mounted over it, in EVERY mode — the treatment ``.vault`` already
#             gets, for the same reason.
#   READONLY  In-sandbox code READS it and a WRITE would let the agent choose its own
#             ceiling. Hiding a ceiling is the WRONG direction: an absent policy file
#             resolves to the permissive standalone default, so masking
#             ``security_policy.json`` from the process that enforces it REMOVES the
#             ceiling instead of protecting it. Exposed read-only instead, which is what
#             ``policy_cache`` already does.
#   VISIBLE   In-sandbox code needs READ *and* WRITE, so no OS rule can apply without
#             breaking it. These stay on the tool gate alone.
#
# ``test_sandbox_governance_mask.py`` pins the union of the three equal to the crew-home
# half of ``security.sensitive_home_dirs()``, so a leaf added there cannot silently land
# in none of them. Spelled here rather than imported so this low-level module keeps not
# importing the 7k-line security module (the ``_POLICY_CACHE_LEAF`` convention above).

#: Crew-home leaves with no legitimate in-sandbox reader — bind-masked in every mode.
_CREW_HIDDEN_LEAVES: tuple[str, ...] = (
    # Channel credentials. Already file-masked in cc/strict via ``_CC_FILES``; listing
    # it here extends the same treatment to standard, where a spawned command could
    # otherwise read every Slack/Discord token off disk.
    ".env",
    # App data holding live credentials or owner-authorization bits. Whole DIRECTORY,
    # not the leaf file, because an atomic write renames a sibling temp into place.
    "apps/aws-control/data",
    "apps/meetings/data/edits",
    "whatsapp",
    # The Notes state files below are OWNED by the md-notebook backend, which is itself
    # a sandboxed spawn (`apps/backend.py`), so the mask alone would break the app: the
    # registry write's final rename gets EPERM and attach/clone always fails (#8762).
    # The backend spawn therefore passes them back as ``extra_visible_dirs`` via
    # :func:`app_backend_visible_targets` — the mask still applies to every OTHER
    # sandboxed process, which is the population it exists to fence.
    "workspace/md-notebook/pat",
    "workspace/md-notebook/vaults.json",
    "workspace/md-notebook/settings.json",
    # Browser session material. The extension token reaches the CLI through the
    # environment, never by ``open()``, so masking the file costs nothing; the other
    # four are retired leaves with no reader left in the tree. The LIVE browser paths
    # (``browser-state``, ``playwright-snapshots``, ``pw``, ``playwright-cli-config.json``)
    # are deliberately absent from the sensitive list and stay fully visible.
    "browser-cookies.txt",
    "playwright-storage-state.json",
    "playwright-extension-token",
    "browser-mode-enabled",
    "browser-engine",
    # Cross-session state whose authorization model is "a session reaches only its own",
    # enforced by the HTTP routes. Every legitimate reader goes through the gateway, so
    # OS-masking closes the sideways path without touching a live consumer.
    "ledger",
    "cron-history",
    "workflow_library",
    "agentcore-inbound",
    "routing",
    "webhooks",
    "live_target.json",
    "backup",
    "mcp-apps",
    # Auth stores and signing keys owned by the gateway web server alone.
    "token_signing.key",
    "refresh_chains.json",
    "kas",
    "ops_mission_control_secrets.json",
    "ops_mission_control_policy.json",
    # No producer and no consumer left in the tree; masked so a backup restore that
    # resurrects a stale file cannot make it readable either.
    ".kiro_cli_binary_trust.json",
    # The identity/auth SQLite store and its WAL/SHM/journal sidecars, whose bytes
    # are a live bearer token. Nothing inside the sandbox opens the crew home's copy:
    # the in-sandbox CLI reads the STAGED store under ``.kiro/crew-auth-staging``, and
    # the gateway-side readers resolve the kiro-cli / amazon-q locations, all of which
    # are fenced elsewhere -- so masking costs no live consumer while closing a
    # spawned ``sqlite3`` or ``open()``. Named from the canonical filename constant
    # (a stdlib-only leaf module, so this stays clear of the security module) so the
    # mask cannot drift from the tool gate that fences the same store.
    AUTH_SQLITE_DB,
    *(f"{AUTH_SQLITE_DB}{suffix}" for suffix in AUTH_SQLITE_SIDECAR_SUFFIXES),
)

#: Crew-home CEILINGS: read by in-sandbox code, never writable by it. Exposed
#: read-only rather than hidden — see the READONLY note above for why hiding a
#: ceiling inverts its effect.
_CREW_READONLY_LEAVES: tuple[str, ...] = (
    # The governance ceiling and its trust root. ``boot_platform()`` resolves both
    # inside the sandbox for a script cron, and an absent file means "no ceiling".
    "security_policy.json",
    "admission_policy.json",
    "profiles",
    "app_admission.json",
    # Opt-out and consent ceilings the config loader reads in-sandbox. A writable
    # ``denied_commands.json`` lets an auto-approved agent set ``disable_all`` and
    # defeat the deny gate after a restart; a writable ``computer_use.json`` lets it
    # turn computer use on for itself.
    "denied_commands.json",
    "computer_use.json",
    "oauth_endpoints.json",
    "aws_service_consent.json",
    # The app dev-mode AUTHORIZATION record (operator grants binding each dev
    # app to its resolved ui root — see apps/dev_mode.py). Sealing it makes
    # "operator, not agent" kernel-enforced: a sandboxed process cannot mint,
    # extend, or rewrite a grant no matter how the toggle is spelled (the
    # deny-list's text/argv tiers can be evaded by runtime construction —
    # ``$(printf ...)`` — but a Seatbelt/namespace write denial cannot). The
    # gateway ensures the file exists at startup (see apply_dev_mode's
    # reconcile) because the Linux launcher can only seal an EXISTING target.
    "apps/.dev-grants.json",
)

#: Crew-home leaves that MUST stay read-write for a sandboxed process. Every entry is
#: a deliberate exception a reviewer should re-check, not an oversight.
_CREW_SANDBOX_VISIBLE_LEAVES: tuple[str, ...] = (
    # Holds this launcher itself (``<config_dir>/run/kirocrew_sandbox_*.py``), so the
    # child cannot exec if it is masked. Already sealed READ-ONLY through
    # ``_voice_runtime_parent_paths()``, with only the ``run/voice-runtime`` leaf hidden.
    "run",
    # The SEL trust root and its append targets. ``verify_session_pid`` reads
    # ``trust/sel_hmac.key`` inside the sandbox to resolve the strict session identity,
    # ``skill_search`` reads ``trust/project-skills.json``, and the in-sandbox MCP
    # servers append to the log directly — a masked log turns an audit-or-deny write
    # into a denial of the action it was auditing.
    "trust",
    "sel_hmac.key",
    "security_events.jsonl",
    "security_events.d",
    # How an in-sandbox MCP server authenticates back to the dashboard. Masking it
    # breaks cron triggering, screencast, and the Sage review driver.
    ".local_secret",
    # ``mcp_cron`` builds a ``CronService(base_dir=config_dir())`` in-sandbox and both
    # reads and rewrites the job store through it.
    "crons.json",
)


def _crew_home_entries(leaves: tuple[str, ...]) -> list[str]:
    """Expand *leaves* across both data-home spellings."""
    return [f"{prefix}/{leaf}" for prefix in _CREW_HOME_PREFIXES for leaf in leaves]


#: Bind-masked in every mode.
_CREW_HIDDEN_DIRS: list[str] = _crew_home_entries(_CREW_HIDDEN_LEAVES)
#: Exposed read-only in every mode.
_CREW_READONLY_TARGETS: list[str] = _crew_home_entries(_CREW_READONLY_LEAVES)

#: Hidden crew-home leaves one app's OWN backend must read and write.
#:
#: These leaves sit in ``_CREW_HIDDEN_LEAVES`` to fence AGENT subprocesses (a
#: prompt-injected shell must not repoint a vault's push target or lift the PAT), but
#: the named app's backend is each leaf's only legitimate reader/writer — and that
#: backend is itself a sandboxed spawn, so the blanket mask breaks the app (#8762).
#: ``apps/backend.py`` passes the resolved paths back as ``extra_visible_dirs`` when
#: spawning exactly that app's backend; every other sandboxed process keeps the mask.
#:
#: Keyed by app name as ``apps/backend.py`` spawns it. Only apps with a SPAWNED
#: backend belong here — an in-process builtin (routes/hooks) runs unsandboxed in the
#: gateway and needs no exemption.
_APP_BACKEND_OWNED_LEAVES: dict[str, tuple[str, ...]] = {
    "md-notebook": (
        "workspace/md-notebook/pat",
        "workspace/md-notebook/vaults.json",
        "workspace/md-notebook/settings.json",
    ),
}


def app_backend_visible_targets(app_name: str) -> tuple[str, ...]:
    """Absolute paths of the hidden leaves *app_name*'s own backend owns.

    Resolved the same way the launcher and seatbelt builders spell their hidden
    targets — ``$HOME``-joined across both data-home spellings, plus the relocated
    paths when ``KIROCREW_HOME`` moves the data home — so each returned path matches
    its mask entry exactly and ``_hidden_path_contains_visible_path`` lifts it.

    Returns ``()`` for an app with no owned leaves, leaving its spawn unchanged.
    """
    leaves = _APP_BACKEND_OWNED_LEAVES.get(app_name)
    if not leaves:
        return ()
    home = str(Path.home())
    targets = [os.path.join(home, entry) for entry in _crew_home_entries(leaves)]
    targets.extend(_relocated_crew_targets(leaves))
    return tuple(targets)


#: The subset of ``_CREW_READONLY_LEAVES`` the launcher may CREATE in order to seal.
#:
#: ``mount(2)`` cannot target a path that does not exist, so the READONLY seal below
#: skips an absent ceiling and leaves the data home writable at that name — which is
#: the whole hole on a default install, where none of these has been written yet.
#: Materialising the path first closes it, and that is only sound for a leaf that
#: clears BOTH of the following.
#:
#: 1. An EMPTY document must mean what an ABSENT file means to the reader:
#:
#:    * ``profiles`` — an empty dir yields no profile, same as no dir;
#:    * ``computer_use.json`` — ``computer_use.enable_state.load_state`` reads ``{}``
#:      as DISABLED, which is what an absent keystone means;
#:    * ``oauth_endpoints.json`` — ``security._validate_operator_oauth_entries``
#:      extends trust by nothing for ``{}``;
#:    * ``aws_service_consent.json`` — ``aws_consent._read_all`` returns ``{}`` for
#:      both absent and empty, so every service stays unconfirmed.
#:
#: 2. A STALE read of that empty document must fail toward refusal. The seal is a
#:    bind mount, which pins the INODE for the sandbox's lifetime, while every
#:    dashboard writer publishes through ``atomic_write`` (temp + rename), i.e. a NEW
#:    inode. So a sandboxed reader keeps seeing the empty document even after the
#:    operator writes the real one. For the three files above that freezes them at
#:    "disabled" / "no consent" / "no extra endpoints" — narrower than the truth. The
#:    empty ``profiles`` dir is exempt from the concern entirely: a directory bind
#:    shows live contents, so a profile added later is visible.
#:
#: DELIBERATELY EXCLUDED, and each for a different one of those two reasons:
#:
#:   * ``denied_commands.json`` — clears (1) but fails (2), which is the direction
#:     that matters: in-sandbox ``mcp_cron`` reads it to decide whether a command is
#:     denied, so a pinned ``{}`` would report "nothing is denied" for the rest of the
#:     sandbox's life even after the operator denies a command. Sealing it would trade
#:     an agent-authored deny list for a stale one, so it keeps the pre-existing gap;
#:   * ``security_policy.json`` — fails (1). ``governance.load_security_policy`` reads
#:     the file whenever it exists and fails CLOSED on a parse or version mismatch, so
#:     a ``{}`` stub raises ``PlatformCompositionError`` out of a function that runs at
#:     boot AND per app callback;
#:   * ``app_admission.json`` — fails (1). Absent means ``open_default()`` (admit),
#:     while present-but-unreadable means deny-all; a stub would refuse every app;
#:   * ``admission_policy.json`` — already seeded at first run by
#:     ``platform.admission.seed_default_policy``, so it is not absent to begin with.
#:
#: The same ``mount(2)`` limit leaves the ``SENSITIVE_DIRS`` / ``SENSITIVE_FILES``
#: mask loops skipping their own absent targets. That is a real sibling gap, not one
#: this list closes: a mask needs the opposite treatment (an empty bind OVER the
#: name), and ``_CREW_HIDDEN_LEAVES`` has no reader to prove an empty document is
#: absent-equivalent, so each leaf needs its own argument.
_CREW_PRECREATE_READONLY_DIR_LEAVES: tuple[str, ...] = ("profiles",)
_CREW_PRECREATE_READONLY_FILE_LEAVES: tuple[str, ...] = (
    "computer_use.json",
    "oauth_endpoints.json",
    "aws_service_consent.json",
)

#: What a materialised ceiling holds — the empty JSON object every reader above
#: already treats as its absent default. NOT a zero-byte file, which is not valid
#: JSON and would read as CORRUPT rather than as absent.
_EMPTY_CEILING_DOCUMENT: bytes = b"{}\n"


def _sealable_absent_ceilings() -> tuple[list[str], list[str]]:
    """Resolved (dir, file) ceiling paths that may be created so the seal can apply.

    Resolved through ``config_dir()`` — the LIVE data home — rather than expanded over
    both ``_CREW_HOME_PREFIXES`` the way the deny lists are. A deny rule covers both
    spellings because either tree may still hold bytes; creation has the opposite
    requirement, since a stub in the deprecated ``~/.kirocrew`` of a migrated host is a
    file nothing will ever read. Whichever spelling ``config_dir()`` resolves to is
    already in the launcher's ``READONLY_DIRS``: both ``$HOME``-relative prefixes are
    listed there, and ``_relocated_crew_targets`` adds a data home that escapes
    ``$HOME``.

    Never raises: an unresolvable data home yields nothing and the seal behaves exactly
    as it did before this function existed.
    """
    try:
        root = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for ceiling sealing", exc_info=True)
        return ([], [])
    return (
        [os.path.join(root, leaf) for leaf in _CREW_PRECREATE_READONLY_DIR_LEAVES],
        [os.path.join(root, leaf) for leaf in _CREW_PRECREATE_READONLY_FILE_LEAVES],
    )


class SandboxCeilingUnsealable(RuntimeError):
    """A governance ceiling could not be made sealable, so the sandbox refuses to launch.

    The seal exists because an unsealed ceiling is a self-elevation hole: a sandboxed
    process that can write ``computer_use.json`` turns on desktop control for itself.
    Launching anyway would run the agent with that hole open while every log line said
    the ceiling was protected, so this is the ``_mount_or_die`` case rather than the
    best-effort one — a control was requested and could not be established.

    Raised out of ``namespace_argv``, so it surfaces to whichever ``wrap_argv`` caller
    asked for the spawn. Those callers report a failed operation; none of them falls back
    to running the command unconfined, which is what makes refusing safe here.

    The two states that reach it are both actionable by an operator, and the message
    names the path for that reason: a DANGLING SYMLINK squatting a ceiling path (either
    tampering, or a link whose destination went away), and a data home where creation
    itself fails (a read-only mount, or a filesystem with no hardlink support).
    """


def _warn_unsealed_ceiling(target: str, exc: "OSError | None") -> None:
    """Say WHY the spawn is being refused: this ceiling could not be made sealable.

    Called immediately before :class:`SandboxCeilingUnsealable` is raised, so the spawn
    does NOT proceed — the log line carries the path and the errno that the exception
    message alone would not, and an operator reading it is the only one who can fix the
    data home. ``warning`` rather than ``debug`` for that reason: a refused spawn with no
    explanation is indistinguishable from an unrelated failure.

    Per spawn rather than once per process, matching the launcher's own ``EXPOSE_FILES``
    pre-read warning — a host where this keeps happening has a real problem, and
    de-duplicating it would hide how often the control cannot be established.
    """
    logger.warning(
        "sandbox: REFUSING to launch — could not create the governance ceiling %s (%s). "
        "mount(2) cannot seal a path that does not exist, so proceeding would leave it "
        "writable inside the sandbox",
        target,
        exc if exc is not None else "publish failed",
    )


def _warn_if_alias_backed(target: str) -> None:
    """Warn when an ALREADY-PRESENT ceiling is reachable under a second name.

    ``MS_RDONLY`` binds a MOUNT, not an inode, so the seal only covers the path it was
    established on. Two shapes therefore survive it, and both are invisible to the seal
    loop because the path resolves and reads as present:

    * the ceiling is a **symlink**. The launcher seals the inode it resolves to, but the
      link NAME lives in the writable data home, so a sandboxed process can unlink it and
      put a real file of its own there instead;
    * the ceiling is a **regular file with another hardlink**. The alias is a different
      path, so it is outside the read-only mount, and a write through it changes the very
      inode the ceiling exposes.

    Reported, not refused, and deliberately so. Refusing would break the ordinary reasons
    a config file has a second name — a dotfile manager such as chezmoi or GNU stow, or a
    snapshot tool holding a hardlink — by turning them into a hard spawn failure, which is
    a much wider blast radius than the exposure. Neither shape is introduced here either:
    the ceilings this module publishes end at ``st_nlink == 1`` and are never symlinks, so
    this is a PRE-EXISTING property of every entry in ``READONLY_DIRS``, reachable only on
    a host where something else already created the ceiling that way. Closing it needs the
    data-home root sealed, which is a different change.

    The warning exists because the alternative is worse than the hole: without it the log
    says the ceiling is sealed while it is writable under another name.
    """
    try:
        info = os.lstat(target)
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode):
        logger.warning(
            "sandbox: the governance ceiling %s is a SYMLINK. The seal covers the file it "
            "resolves to, but the link itself sits in a writable directory, so a sandboxed "
            "process can replace the name. Make it a regular file to close that.",
            target,
        )
    elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        logger.warning(
            "sandbox: the governance ceiling %s has %d hardlinks. The seal covers this "
            "path only, so a write through another name reaches the same inode. Remove the "
            "extra link to close that.",
            target,
            info.st_nlink,
        )


def _refuse_if_dangling_symlink(target: str) -> None:
    """Refuse the spawn when *target* is a symlink that resolves to nothing.

    A RESOLVING symlink is left alone deliberately: it reads as present, so the launcher
    seals the inode it resolves to. The residual exposure there — the link NAME stays
    replaceable in a writable parent — is pre-existing for every ceiling, not specific to
    one this function materialises, and closing it needs the data-home root sealed.
    """
    if not os.path.islink(target) or os.path.exists(target):
        return
    pointed_at = "(unreadable)"
    with contextlib.suppress(OSError):
        pointed_at = os.readlink(target)
    raise SandboxCeilingUnsealable(
        f"the governance ceiling {target} is a DANGLING symlink -> {pointed_at}. "
        "mount(2) cannot seal it and it would leave the path writable inside the "
        "sandbox. Remove or repoint it, or lower sandbox_level to run without the seal "
        "deliberately."
    )


def _publish_empty_ceiling(target: str, parent: str) -> bool:
    """Write the empty document to a sibling temp file, then link it into place.

    Two steps rather than ``open(target, O_CREAT | O_EXCL)`` followed by a write,
    because the one-step form publishes the NAME before the BYTES: a crash, a full
    disk, or a signal in between leaves a zero-length file at the ceiling path, and
    zero length is not valid JSON — the reader would see corrupt where this function
    means absent. Here the target only ever appears once its content is complete.

    ``os.link`` is the publish because it is the no-clobber one: unlike ``os.replace``
    it fails with ``EEXIST`` instead of overwriting, so a racing spawn — or an operator
    writing the real document in the same instant — keeps its file. That is also why
    the ``os.path.exists`` pre-check upstream is an optimisation and not the guard.

    ``mkstemp`` creates the temp file 0o600 before the first byte, so no separate
    lockdown call is needed (and none may be added: a lockdown applied after content
    reaches a published path is the defect ``scripts/check_lockdown_before_publish.py``
    refuses). The mode needs no reassertion either — a umask can only clear bits, never
    add them.

    Returns ``False`` on any failure, having published nothing. The caller decides what
    a failure means; this function's only contract is that the ceiling path is either
    absent or holds the complete document.
    """
    fd = -1
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".kirocrew-ceiling-", suffix=".tmp")
        # ``os.write`` is not obliged to consume the whole buffer, and a short write is
        # not an error — it returns a count. Taking that count for success would link a
        # TRUNCATED document, which reads as corrupt rather than as absent and is the
        # exact outcome the temp-then-link shape exists to prevent. Loop, and treat zero
        # progress as an error so a filesystem that accepts nothing cannot spin here.
        view = memoryview(_EMPTY_CEILING_DOCUMENT)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write to a ceiling temp file", tmp)
            view = view[written:]
        os.close(fd)
        fd = -1
        os.link(tmp, target)
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _materialize_sealable_ceilings() -> list[str]:
    """Create every absent sealable ceiling; return the paths actually created.

    Runs on the Linux spawn path only, immediately before the launcher builds its
    ``READONLY_DIRS`` mount sequence, so a ceiling that did not exist a moment ago is
    a read-only mountpoint by the time the sandboxed command runs.

    **Fail-closed.** If a ceiling cannot be made sealable this raises
    :class:`SandboxCeilingUnsealable` and the spawn does not happen. That is a
    deliberate reversal of an earlier best-effort version, which warned and continued:
    continuing means the launcher's ``os.path.exists`` guard skips the path, so the
    sandboxed process runs with a writable governance keystone and nothing downstream
    notices. An unsealed ceiling is the one thing this function exists to prevent, so it
    refuses for the same reason ``_mount_or_die`` refuses a failed hiding mount.

    Two states trigger it:

    * a **dangling symlink** squatting a ceiling path. ``os.path.exists`` follows
      symlinks, so it reads as absent to this function AND to the launcher's guard,
      while ``os.link`` refuses the name as ``EEXIST`` — the sandboxed process's write
      then follows the link and the host reads the result back through the ceiling path.
      It is not removed here: ``islink`` followed by ``unlink`` is not atomic, and the
      dashboard publishes a real keystone over that same name with ``atomic_write``, so
      a removal racing a validated operator write would delete the operator's new
      settings. POSIX offers no unlink-only-if-still-a-symlink, so the safe answer is to
      refuse and let a human resolve it;
    * a **creation failure** other than ``EEXIST`` — a read-only mount, or a filesystem
      with no hardlink support.

    ``EEXIST`` is the one benign outcome, in both loops: the racing spawn that got there
    first, or the operator's real document. Either way the path now exists, so the
    launcher seals it and there is nothing to report.

    Never TRUNCATES and never REMOVES: an existing ceiling is left byte-for-byte alone,
    so this can only ever add the absent default.
    """
    created: list[str] = []
    dir_targets, file_targets = _sealable_absent_ceilings()

    for target in dir_targets:
        _refuse_if_dangling_symlink(target)
        if os.path.exists(target):
            # Present, so the launcher will seal it -- but say so when the seal is
            # reachable around rather than through this path.
            _warn_if_alias_backed(target)
            continue
        if not os.path.isdir(os.path.dirname(target)):
            continue
        try:
            # 0o700 needs no reassertion: a umask can only clear bits, never add them.
            os.mkdir(target, 0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            _warn_unsealed_ceiling(target, exc)
            raise SandboxCeilingUnsealable(
                f"cannot create the governance ceiling {target}: {exc}"
            ) from exc
        created.append(target)

    for target in file_targets:
        parent = os.path.dirname(target)
        _refuse_if_dangling_symlink(target)
        if os.path.exists(target):
            _warn_if_alias_backed(target)
            continue
        if not os.path.isdir(parent):
            continue
        if _publish_empty_ceiling(target, parent):
            created.append(target)
        elif not os.path.exists(target):
            # Absent after a failed publish, so nothing won the race: the seal really
            # did not apply. ``exists`` rather than a plumbed-through errno because the
            # publish is two syscalls and only the OUTCOME decides whether this matters.
            _warn_unsealed_ceiling(target, None)
            raise SandboxCeilingUnsealable(
                f"cannot publish the governance ceiling {target}; it would stay writable "
                "inside the sandbox"
            )

    return created


_STRICT_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".config/gh",
    ".azure",
    ".docker",
    ".kube",
    # Encrypted secret vault (PR 1 of #2351). The ``.vault`` dir is also a
    # keystone leaf in ``security._CREW_SECRET_LEAVES`` (which blocks the
    # agent's in-process tool-call file access), but a spawned ``python -c``
    # subprocess does an OS ``open()`` that never routes through that gate — so
    # the vault dir must ALSO be bind-mount-hidden here, exactly as ``.env`` is
    # in ``_CC_FILES``. Without this a same-UID agent subprocess could read
    # ``.vault/.vault_key`` and decrypt the store.
    ".kiro/crew/.vault",
    ".kirocrew/.vault",
    # The centrally-distributed governance ceiling's cache
    # (``platform/policy_distribution.py``). Bind-mount-hidden in every mode for the
    # reason the vault above is: ``is_sensitive_path`` is the shared read+write gate for
    # the agent's in-process tool calls, but a spawned ``python -c`` does an OS
    # ``open()`` that never routes through it. That matters more here than for the
    # policy FILE, which this cache copies: on a fleet using the environment channel
    # there is no ``security_policy.json`` on disk at all, so the cache is the only
    # on-disk copy of the ceiling — and its metadata records the SOURCE, which the
    # loader trusts when deciding whether the cache is this host's last-known-good.
    ".kiro/crew/policy_cache",
    ".kirocrew/policy_cache",
    ".kiro/crew/run/voice-runtime",
    ".kirocrew/run/voice-runtime",
]
_STRICT_DIRS += _CREW_HIDDEN_DIRS
_STRICT_DIRS += [".midway"]

_STANDARD_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
    # Secret vault — hidden in every mode (see _STRICT_DIRS note above).
    ".kiro/crew/.vault",
    ".kirocrew/.vault",
    # The centrally-distributed governance ceiling's cache
    # (``platform/policy_distribution.py``). Bind-mount-hidden in every mode for the
    # reason the vault above is: ``is_sensitive_path`` is the shared read+write gate for
    # the agent's in-process tool calls, but a spawned ``python -c`` does an OS
    # ``open()`` that never routes through it. That matters more here than for the
    # policy FILE, which this cache copies: on a fleet using the environment channel
    # there is no ``security_policy.json`` on disk at all, so the cache is the only
    # on-disk copy of the ceiling — and its metadata records the SOURCE, which the
    # loader trusts when deciding whether the cache is this host's last-known-good.
    ".kiro/crew/policy_cache",
    ".kirocrew/policy_cache",
    ".kiro/crew/run/voice-runtime",
    ".kirocrew/run/voice-runtime",
]
_STANDARD_DIRS += _CREW_HIDDEN_DIRS

# CC mode: hides all credential dirs including .aws, but selectively exposes
# .aws/config (needed for credential_process → Bedrock auth). All other .aws
# files (credentials, sso cache, etc.) are filesystem-hidden via bind mount.
_CC_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
    ".kube",
    # Secret vault — hidden in every mode (see _STRICT_DIRS note above).
    ".kiro/crew/.vault",
    ".kirocrew/.vault",
    # The centrally-distributed governance ceiling's cache
    # (``platform/policy_distribution.py``). Bind-mount-hidden in every mode for the
    # reason the vault above is: ``is_sensitive_path`` is the shared read+write gate for
    # the agent's in-process tool calls, but a spawned ``python -c`` does an OS
    # ``open()`` that never routes through it. That matters more here than for the
    # policy FILE, which this cache copies: on a fleet using the environment channel
    # there is no ``security_policy.json`` on disk at all, so the cache is the only
    # on-disk copy of the ceiling — and its metadata records the SOURCE, which the
    # loader trusts when deciding whether the cache is this host's last-known-good.
    ".kiro/crew/policy_cache",
    ".kirocrew/policy_cache",
    ".kiro/crew/run/voice-runtime",
    ".kirocrew/run/voice-runtime",
]
_CC_DIRS += _CREW_HIDDEN_DIRS
_CC_DIRS += [".midway"]


def _relocated_crew_targets(leaves: tuple[str, ...]) -> list[str]:
    """The RESOLVED crew-home paths for *leaves*, when the data home is not under ``$HOME``.

    Every entry in the dir lists is ``$HOME``-relative and joined with ``Path.home()``, so
    ``KIROCREW_HOME=/srv/crew`` moves the data home out from under all of them and no rule
    matches the real governance tree. :func:`_relocated_policy_cache_dirs` already closes
    that hole for the one directory it was written for; the ceiling and the secret leaves
    need it for the same reason, so the resolution is shared here instead of restated.

    Returns only the paths that DIFFER from the ``$HOME``-relative spelling the lists
    already carry, so the default layout gains no duplicate rule.

    ``normpath``, never ``realpath`` — this runs inside ``_build_launcher_script`` and the
    seatbelt builder, which execute on the event loop for every async spawn, and a
    link-resolving syscall on a stalled NFS home would freeze the gateway with its
    liveness heartbeat. A symlinked home therefore reports as relocated and yields a
    redundant rule for a path that is covered either way, never a missing one.

    Never raises: a data home that cannot be resolved yields nothing and the
    ``$HOME``-relative entries still apply.
    """
    try:
        home_root = os.path.join(str(Path.home()), _CREW_HOME_DEFAULT)
        resolved_root = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for sandbox masking", exc_info=True)
        return []
    out: list[str] = []
    for leaf in leaves:
        try:
            resolved = os.path.normpath(os.path.join(resolved_root, leaf))
            default = os.path.normpath(os.path.join(home_root, leaf))
        except Exception:  # pragma: no cover - defensive
            continue
        if resolved != default:
            out.append(resolved)
    return out


def _relocated_policy_cache_dirs() -> list[str]:
    """The governance cache's RESOLVED path, when it is not under ``$HOME``.

    Every entry in the dir lists above is ``$HOME``-relative and joined with
    ``Path.home()``, so ``KIROCREW_HOME=/srv/crew`` moves the data home out from under
    all of them. That limitation is pre-existing and shared with the vault entries, but
    this one directory must not inherit it: on a fleet using the environment channel
    there is no ``security_policy.json`` on disk at all, so the cache is the ONLY on-disk
    copy of the ceiling, and its metadata records the source the next boot trusts. An
    agent subprocess able to rewrite it on a relocated home could hand itself a ceiling.

    Returns the path only when it differs from the ``$HOME``-relative form the lists
    already cover, so the default layout gains no duplicate rule.

    **Compared with ``normpath``, not ``realpath``, and that is deliberate.** This runs
    inside ``_build_launcher_script`` / the seatbelt builder, which run on the event loop
    for every async spawn — the same reason the launcher pushes its ``isdir`` checks into
    the child (see the note there): on a stalled NFS home a link-resolving syscall here
    freezes the gateway and its liveness heartbeat. ``normpath`` is pure string work.

    The cost is precise and one-directional: where the home is a symlink (``/home/u`` →
    ``/local/home/u`` is ordinary on managed hosts) the two spellings no longer compare
    equal, so a DEFAULT layout is reported as relocated and the resolved path is masked in
    addition to the ``$HOME``-relative one. That is a redundant rule for a directory that
    should be masked either way, never a missing one — the comparison was only ever
    de-duplication. Never raises: a data home that cannot be resolved yields nothing and
    the ``$HOME``-relative entry still applies.
    """
    try:
        resolved = os.path.normpath(os.path.join(str(config_dir()), _POLICY_CACHE_LEAF))
        default = os.path.normpath(
            os.path.join(str(Path.home()), _CREW_HOME_DEFAULT, _POLICY_CACHE_LEAF)
        )
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the policy-cache path for sandbox masking", exc_info=True)
        return []
    return [] if resolved == default else [resolved]


_voice_runtime_paths_lock = threading.Lock()
_voice_runtime_paths_cache: (
    tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None
) = None


def _ensure_voice_runtime_directory(path: str) -> None:
    """Create one gateway-owned runtime directory without following its leaf."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OSError(f"voice runtime path is not a real directory: {path}")
    os.chmod(path, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only and the tightest traversable directory mode; Semgrep's suggested 0o644 would remove directory traversal and grant reads to other users.  # noqa: E501  # fmt: skip


def _literal_ancestor_guards(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return every rename-sensitive ancestor below the filesystem root."""
    guards: list[str] = []
    for item in paths:
        current = os.path.normpath(item)
        while True:
            parent = os.path.dirname(current)
            if parent == current:
                break
            if current not in guards:
                guards.append(current)
            current = parent
    return tuple(guards)


def prime_voice_runtime_sandbox_paths() -> str:
    """Cache and create the canonical agent-denied decoder runtime off-loop.

    ``config_dir()`` deliberately preserves a supported symlinked default data
    home. Seatbelt rules are path-based, so both that lexical spelling and the
    canonical target must be denied. Realpath resolution and directory creation
    happen here. Async agent startup reaches this through
    :func:`bind_voice_safe_agent_workspace_async`, which performs the work in a
    worker thread so gateway readiness is never gated on data-home filesystem IO.
    """
    global _voice_runtime_paths_cache

    lexical_home = os.path.normpath(str(config_dir()))
    cached = _voice_runtime_paths_cache
    if cached is not None and cached[0] == lexical_home:
        return cached[1]
    with _voice_runtime_paths_lock:
        cached = _voice_runtime_paths_cache
        if cached is not None and cached[0] == lexical_home:
            return cached[1]

        canonical_home = os.path.realpath(lexical_home)
        home_info = os.lstat(canonical_home)
        if not stat.S_ISDIR(home_info.st_mode) or stat.S_ISLNK(home_info.st_mode):
            raise OSError("Kiro Crew data home does not resolve to a real directory")

        canonical_run = os.path.join(canonical_home, "run")
        canonical_root = os.path.join(canonical_home, _VOICE_RUNTIME_LEAF)
        _ensure_voice_runtime_directory(canonical_run)
        _ensure_voice_runtime_directory(canonical_root)

        lexical_run = os.path.join(lexical_home, "run")
        lexical_root = os.path.join(lexical_home, _VOICE_RUNTIME_LEAF)
        roots = tuple(dict.fromkeys((lexical_root, canonical_root)))
        parents = tuple(dict.fromkeys((lexical_run, canonical_run)))
        guards = _literal_ancestor_guards(parents)
        _voice_runtime_paths_cache = (
            lexical_home,
            canonical_root,
            roots,
            parents,
            guards,
        )
        return canonical_root


def _voice_runtime_sandbox_paths() -> tuple[str, ...]:
    """Return lexical and canonical snapshot roots, priming as a safe fallback."""
    prime_voice_runtime_sandbox_paths()
    assert _voice_runtime_paths_cache is not None
    return _voice_runtime_paths_cache[2]


def _voice_runtime_parent_paths() -> tuple[str, ...]:
    """Return runtime parents that agent processes may read but never write."""
    prime_voice_runtime_sandbox_paths()
    assert _voice_runtime_paths_cache is not None
    return _voice_runtime_paths_cache[3]


def _voice_runtime_ancestor_guards() -> tuple[str, ...]:
    """Return literal paths an agent must not rename around path-based rules."""
    prime_voice_runtime_sandbox_paths()
    assert _voice_runtime_paths_cache is not None
    return _voice_runtime_paths_cache[4]


_VOICE_GUARD_REMEDY = "Pick a project subdirectory that does not contain the Kiro Crew data home."

_VoiceGuardRelationship = Literal["contains", "inside", "alias", "cannot-verify"]


def _voice_runtime_guard_message(
    workspace_path: str,
    runtime_path: str,
    relationship: _VoiceGuardRelationship,
    failed_path: str | None = None,
    failure_reason: str | None = None,
) -> str:
    """Build every variant of the voice-runtime workspace refusal.

    Each variant leads with the two concrete absolute paths, keeps its own
    distinguishing detail, and ends with the same remedy sentence, so a user
    who picked ``~`` (an ancestor of the default ``~/.kiro/crew`` data home)
    sees exactly which two paths collide and what to choose instead. The
    message is operator-facing and may reach logs: it carries only the two
    paths the caller already knows (plus, on the cannot-verify variant, the
    path whose filesystem check failed).
    """
    if relationship == "contains":
        return (
            f"macOS agent workspace {workspace_path!r} overlaps Kiro Crew's "
            f"protected voice runtime {runtime_path!r}: the workspace contains "
            f"the voice runtime / data home. {_VOICE_GUARD_REMEDY}"
        )
    if relationship == "inside":
        return (
            f"macOS agent workspace {workspace_path!r} overlaps Kiro Crew's "
            f"protected voice runtime {runtime_path!r}: the workspace is the "
            f"voice runtime / data home or lives inside it. {_VOICE_GUARD_REMEDY}"
        )
    if relationship == "alias":
        return (
            f"macOS agent workspace {workspace_path!r} aliases Kiro Crew's "
            f"protected voice runtime {runtime_path!r}: by filesystem identity "
            "(a case, normalization, symlink, or firmlink alias) one of these "
            f"paths is the other or an ancestor of the other. {_VOICE_GUARD_REMEDY}"
        )
    return (
        f"cannot verify that macOS agent workspace {workspace_path!r} is "
        f"separate from Kiro Crew's protected voice runtime {runtime_path!r}: "
        f"a filesystem check failed on {failed_path!r} ({failure_reason}), "
        "so the guard cannot prove the paths are disjoint and fails closed "
        f"rather than start an agent it cannot isolate. {_VOICE_GUARD_REMEDY}"
    )


def _lexical_runtime_overlap(
    workspace_paths: tuple[str, ...], runtime_paths: tuple[str, ...]
) -> tuple[str, str, _VoiceGuardRelationship] | None:
    """First lexical containment hit between workspace and runtime spellings.

    THE shared containment scan: :func:`voice_runtime_workspace_conflict` (the
    non-raising pre-flight) and :func:`assert_voice_runtime_outside_agent_workspace`
    (the fail-closed spawn guard) both call this, so the pre-flight cannot
    silently drift from the guard it mirrors (Design/FP review round 2 — the
    two previously carried independent copies of this loop).

    Returns ``(workspace_path_to_name, runtime_path, relationship)`` for the
    first hit, or ``None``. Naming convention is the guard's: refusals always
    name the workspace as the caller spelled it (``workspace_paths[0]``); a
    hit found only on a non-original spelling (the realpath of a symlinked
    workspace) is an ``"alias"`` relationship — formatting the resolved path
    instead would print the runtime path twice and omit the path the user
    actually configured.
    """
    original = workspace_paths[0]
    for workspace_path in workspace_paths:
        for runtime_path in runtime_paths:
            try:
                common = os.path.commonpath((workspace_path, runtime_path))
            except ValueError:
                continue
            if common not in (workspace_path, runtime_path):
                continue
            if workspace_path != original:
                return (original, runtime_path, "alias")
            return (
                original,
                runtime_path,
                "inside" if common == runtime_path else "contains",
            )
    return None


def voice_runtime_workspace_conflict(workspace: str | os.PathLike[str]) -> str | None:
    """Pre-flight: describe why *workspace* would be rejected, or ``None``.

    A non-raising lexical version of
    :func:`assert_voice_runtime_outside_agent_workspace` for validation
    surfaces (the project endpoint, pickers) that want to warn BEFORE a
    session exists. Lexical containment only — the descriptor/identity walks
    stay in the spawn-time guards, which remain authoritative; a ``None`` here
    is a pre-flight pass, not a security verdict. Darwin-gated to MATCH the
    guards it pre-flights: every spawn-time guard early-returns off macOS, so
    a workspace that overlaps the data home spawns fine on Linux/Windows
    today — refusing it here would remove a working configuration to prevent
    a macOS-only harm (and with macOS-worded copy).

    Messages come from :func:`_voice_runtime_guard_message` (#7407) and the
    containment scan is shared with the spawn-time guard
    (:func:`_lexical_runtime_overlap`), so the pre-flight warning and the
    spawn-time refusal read identically and cannot drift apart.
    """
    if sys.platform != "darwin":
        return None
    workspace_paths = tuple(
        dict.fromkeys(
            (
                os.path.abspath(os.fspath(workspace)),
                os.path.realpath(os.fspath(workspace)),
            )
        )
    )
    try:
        runtime_paths = tuple(
            dict.fromkeys(os.path.abspath(path) for path in _voice_runtime_sandbox_paths())
        )
    except OSError:
        # Pre-flight only: if the runtime paths cannot be resolved here, let
        # the spawn-time guard (which fails closed) produce the verdict.
        return None
    hit = _lexical_runtime_overlap(workspace_paths, runtime_paths)
    if hit is not None:
        return _voice_runtime_guard_message(*hit)
    return None


def assert_voice_runtime_outside_agent_workspace(workspace: str | os.PathLike[str]) -> None:
    """Fail closed when a macOS agent workspace can reach decoder snapshots.

    Kiro's internal macOS sandbox cannot nest inside Kiro Crew's Seatbelt
    profile, so delegated Kiro agents do not inherit our voice-runtime deny
    rules. A workspace that is the voice root, lives below it, or contains it
    would therefore let a same-UID agent replace a verified named Mach-O image
    before ``posix_spawn`` opens it. Check both lexical and canonical spellings
    before either ACP agent path delegates isolation to Kiro.
    """
    if sys.platform != "darwin":
        return

    def _identity_in_ancestor_chain(identity: tuple[int, int], path: str) -> bool:
        current = os.path.abspath(path)
        while True:
            info = os.stat(current)
            if (info.st_dev, info.st_ino) == identity:
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent

    raw_workspace_paths = tuple(
        dict.fromkeys(
            (
                os.path.abspath(os.fspath(workspace)),
                os.path.realpath(os.fspath(workspace)),
            )
        )
    )
    raw_runtime_paths = tuple(
        dict.fromkeys(os.path.abspath(path) for path in _voice_runtime_sandbox_paths())
    )
    # Refusals always name the workspace as the caller spelled it. A hit found
    # only on the canonical (realpath) spelling of a symlinked workspace is an
    # alias relationship from the caller's own spelling -- formatting the
    # resolved path instead would print the runtime path twice and omit the
    # path the user actually configured. The scan itself is shared with the
    # non-raising pre-flight (_lexical_runtime_overlap), so the two surfaces
    # cannot drift apart.
    original_workspace_path = raw_workspace_paths[0]
    lexical_hit = _lexical_runtime_overlap(raw_workspace_paths, raw_runtime_paths)
    if lexical_hit is not None:
        raise RuntimeError(_voice_runtime_guard_message(*lexical_hit))

    # Path spelling is only a fast reject. Compare filesystem identities too,
    # walking both ancestor directions so case, normalization, symlink, and
    # firmlink aliases on an existing APFS workspace cannot evade the guard.
    try:
        workspace_identities = tuple(
            (info.st_dev, info.st_ino) for info in (os.stat(path) for path in raw_workspace_paths)
        )
        runtime_identities = tuple(
            (info.st_dev, info.st_ino) for info in (os.stat(path) for path in raw_runtime_paths)
        )
        for workspace_identity in workspace_identities:
            for runtime_path in raw_runtime_paths:
                if _identity_in_ancestor_chain(workspace_identity, runtime_path):
                    raise RuntimeError(
                        _voice_runtime_guard_message(original_workspace_path, runtime_path, "alias")
                    )
        for runtime_path, runtime_identity in zip(raw_runtime_paths, runtime_identities):
            for workspace_path in raw_workspace_paths:
                if _identity_in_ancestor_chain(runtime_identity, workspace_path):
                    raise RuntimeError(
                        _voice_runtime_guard_message(original_workspace_path, runtime_path, "alias")
                    )
    except OSError as exc:
        raise RuntimeError(
            _voice_runtime_guard_message(
                raw_workspace_paths[0],
                raw_runtime_paths[0] if raw_runtime_paths else "<unknown>",
                "cannot-verify",
                failed_path=getattr(exc, "filename", None) or "<unknown path>",
                failure_reason=getattr(exc, "strerror", None) or str(exc),
            )
        ) from exc


def _open_directory_descriptor(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
    """Open a directory identity without making its descriptor inheritable."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(os.fspath(path), flags, dir_fd=dir_fd)


def _directory_ancestor_identities(descriptor: int) -> tuple[tuple[int, int], ...]:
    """Walk directory ancestors by descriptor, immune to pathname retargeting."""
    current = os.dup(descriptor)
    identities: list[tuple[int, int]] = []
    try:
        while True:
            current_info = os.fstat(current)
            current_identity = (current_info.st_dev, current_info.st_ino)
            identities.append(current_identity)
            parent = _open_directory_descriptor("..", dir_fd=current)
            parent_info = os.fstat(parent)
            parent_identity = (parent_info.st_dev, parent_info.st_ino)
            if parent_identity == current_identity:
                os.close(parent)
                break
            os.close(current)
            current = parent
        return tuple(identities)
    finally:
        os.close(current)


def bind_voice_safe_agent_workspace(
    workspace: str | os.PathLike[str],
) -> tuple[str, int | None]:
    """Bind a verified macOS workspace identity for delegated Kiro startup.

    A pathname-only overlap check has an unavoidable check/use window: another
    sandboxed process can retarget a workspace symlink after ``stat`` and before
    Kiro initializes its own sandbox. On macOS, open the workspace first and
    compare directory ancestry entirely through descriptors.

    The descriptor is returned ALONGSIDE the pathname, never baked into it. The
    child enters it with ``fchdir`` (see ``create_subprocess_limited``'s
    ``chdir_fd``), so nothing re-resolves the name. Handing the spawn a
    ``cwd="/dev/fd/<n>"`` pathname instead does not work: only Linux publishes
    those entries as symlinks to the target, and on macOS -- the only platform
    that binds here at all -- ``chdir()`` on one is refused (``EACCES`` on one
    reporting host, ``ENOTDIR`` on macOS 26), which is every delegated spawn on a
    packaged build.

    The returned descriptor must stay open as long as the caller re-verifies the
    binding through :func:`bound_agent_workspace_target`. The child's copy is
    independent, so closing this one does not disturb a running agent.

    Other platforms keep their original pathname and do not inherit a descriptor.
    """
    workspace_path = os.fspath(workspace)
    if sys.platform != "darwin":
        return workspace_path, None

    workspace_fd = -1
    runtime_fds: list[int] = []
    runtime_paths: tuple[str, ...] = ()
    try:
        # Resolve the runtime paths before opening the workspace: a workspace
        # open() failure lands in the OSError handler below, which names the
        # colliding runtime path in its refusal -- resolving after the open
        # would print "<unknown>" for exactly the failure a user hits first.
        runtime_paths = _voice_runtime_sandbox_paths()

        workspace_fd = _open_directory_descriptor(workspace_path)
        workspace_identity = os.fstat(workspace_fd)
        workspace_id = (workspace_identity.st_dev, workspace_identity.st_ino)
        workspace_ancestors = set(_directory_ancestor_identities(workspace_fd))

        for runtime_path in runtime_paths:
            runtime_fd = _open_directory_descriptor(runtime_path)
            runtime_fds.append(runtime_fd)
            runtime_identity = os.fstat(runtime_fd)
            runtime_id = (runtime_identity.st_dev, runtime_identity.st_ino)
            runtime_ancestors = set(_directory_ancestor_identities(runtime_fd))
            if workspace_id in runtime_ancestors or runtime_id in workspace_ancestors:
                raise RuntimeError(
                    _voice_runtime_guard_message(
                        os.path.abspath(workspace_path),
                        os.path.abspath(runtime_path),
                        "inside" if runtime_id in workspace_ancestors else "contains",
                    )
                )

        return workspace_path, workspace_fd
    except OSError as exc:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        raise RuntimeError(
            _voice_runtime_guard_message(
                os.path.abspath(workspace_path),
                os.path.abspath(runtime_paths[0]) if runtime_paths else "<unknown>",
                "cannot-verify",
                failed_path=getattr(exc, "filename", None) or "<unknown path>",
                failure_reason=getattr(exc, "strerror", None) or str(exc),
            )
        ) from exc
    except BaseException:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        raise
    finally:
        for runtime_fd in runtime_fds:
            os.close(runtime_fd)


def _close_bound_agent_workspace(descriptor: int) -> None:
    """Close a workspace descriptor, swallowing an already-closed race."""
    try:
        os.close(descriptor)
    except OSError:
        pass


async def release_bound_agent_workspace(descriptor: int) -> None:
    """Close a bound workspace descriptor off-loop before honoring cancellation."""
    closing = asyncio.create_task(asyncio.to_thread(_close_bound_agent_workspace, descriptor))
    cancellation: asyncio.CancelledError | None = None
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError as exc:
            # A descriptor is a process-lifetime resource.  A second cancellation
            # must not detach the worker that owns its close and leak it until the
            # gateway exits, so settle the tiny close before propagating cancel.
            cancellation = exc
    closing.result()
    if cancellation is not None:
        raise cancellation


async def bind_voice_safe_agent_workspace_async(
    workspace: str | os.PathLike[str],
) -> tuple[str, int | None]:
    """Cancellation-safe off-loop wrapper for workspace identity binding.

    ``asyncio.to_thread`` cannot stop a running worker.  If its awaiter is
    cancelled after the worker opens the descriptor but before ownership is
    transferred, a plain await loses the returned fd.  Shield and settle the
    worker; on cancellation, close any descriptor it produced before re-raising.
    """
    binding = asyncio.create_task(asyncio.to_thread(bind_voice_safe_agent_workspace, workspace))
    cancellation: asyncio.CancelledError | None = None
    while not binding.done():
        try:
            await asyncio.shield(binding)
        except asyncio.CancelledError as exc:
            cancellation = exc

    if cancellation is None:
        return binding.result()

    try:
        _path, descriptor = binding.result()
    except BaseException:
        # The caller's cancellation remains authoritative, but retrieving the
        # worker exception prevents a false "Task exception was never retrieved".
        raise cancellation
    if descriptor is not None:
        try:
            await release_bound_agent_workspace(descriptor)
        except asyncio.CancelledError as exc:
            cancellation = exc
    raise cancellation


def _bound_agent_workspace_matches(descriptor: int, workspace: str | os.PathLike[str]) -> bool:
    """Whether *workspace* currently names an already-bound directory identity.

    The caller uses the bound identity after this comparison, never the supplied
    pathname, so a subsequent symlink retarget cannot change what is authorized.
    """
    candidate = _open_directory_descriptor(workspace)
    try:
        expected = os.fstat(descriptor)
        actual = os.fstat(candidate)
        return (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)
    finally:
        os.close(candidate)


def bound_agent_workspace_target(descriptor: int, workspace: str | os.PathLike[str]) -> str | None:
    """The bound directory's OWN pathname, or None when *workspace* is not it.

    The identity check and the name read are one call because a caller needs both
    in the same worker hop, and because returning the caller's own pathname would
    defeat the check: that string is exactly what a same-UID retarget controls,
    and a peer handed it re-resolves it after this returns.

    What comes back is the kernel's name for the descriptor that was verified
    (``/proc/self/fd`` on Linux, ``F_GETPATH`` on macOS), so it carries no symlink
    component left to swap and it cannot name a descendant the check never covered.

    It does NOT make a peer's own resolution descriptor-bound, and nothing can: a
    pathname handed to another process is re-resolved by that process, and macOS has
    no descriptor-addressable path namespace to hand instead (``/dev/fd/<n>`` is
    exactly what it cannot resolve). A same-UID rename of the canonical directory
    between this call and that resolution therefore stays open. Callers that own the
    child's cwd should pin it with ``create_subprocess_limited``'s ``chdir_fd``,
    which does not go through a name at all; this is for the ACP ``session/new`` cwd,
    where a string is the only thing the protocol carries.

    Raises OSError when this platform exposes no way to ask, so the caller fails
    closed instead of falling back to the mutable spelling.
    """
    if not _bound_agent_workspace_matches(descriptor, workspace):
        return None
    resolved = fd_real_path(descriptor)
    if resolved is None:
        raise OSError(
            errno.ENOSYS,
            "cannot read a bound workspace descriptor's own path on this platform",
        )
    return resolved


class BoundWorkspaceMismatch(Exception):
    """A requested session workspace is not the bound directory identity."""


async def resolve_bound_session_workspace(
    descriptor: int, workspace: str | os.PathLike[str]
) -> str:
    """Off-loop verify-then-substitute for an ACP session cwd on a bound runtime.

    Both ACP front ends enforce one rule -- prove the requested path still names the
    bound identity, then hand the peer the DESCRIPTOR's own name instead of the
    caller's spelling -- so the rule lives here once rather than in two places that
    can drift apart. Each caller keeps only the mapping to its own error type:
    :class:`BoundWorkspaceMismatch` when the path is not the bound identity, OSError
    when the binding cannot be verified at all.

    Off-loop because it opens a directory and reads a descriptor's name; on the loop
    that is filesystem IO in front of every session start.
    """
    resolved = await asyncio.to_thread(bound_agent_workspace_target, descriptor, workspace)
    if resolved is None:
        raise BoundWorkspaceMismatch(os.fspath(workspace))
    return resolved


def _is_policy_cache_dir(path: str) -> bool:
    """Whether *path* is a governance-cache directory, by leaf name.

    Matched on the leaf rather than against a resolved path so it holds for every
    spelling the dir lists carry — the ``$HOME``-relative default, the legacy
    ``~/.kirocrew`` entry that the deny lists must keep covering, and the relocated
    form from :func:`_relocated_policy_cache_dirs` — without a filesystem call on the
    spawn path.
    """
    return os.path.basename(path.rstrip("/" + os.sep)) == _POLICY_CACHE_LEAF


def _crew_hidden_sandbox_targets() -> set[str]:
    """Absolute paths of the crew-home leaves the sandbox masks, both spellings.

    The seatbelt profile needs to tell these apart from the other hidden entries: they
    take a write deny as well as a read deny, while ``.aws`` must not (a tool refreshing
    a cached token rewrites it legitimately). On Linux the distinction does not arise --
    a bind mount blocks both directions in one rule.
    """
    home = str(Path.home())
    targets = {os.path.join(home, rel) for rel in _CREW_HIDDEN_DIRS}
    targets.update(_relocated_crew_targets(_CREW_HIDDEN_LEAVES))
    return targets


def _is_voice_runtime_dir(path: str) -> bool:
    """Whether *path* is the gateway-only voice runtime subtree."""
    normalized = os.path.normpath(path)
    return normalized.endswith(os.sep + _VOICE_RUNTIME_LEAF) or normalized.endswith(
        "/" + _VOICE_RUNTIME_LEAF.replace(os.sep, "/")
    )


# CC mode: files to expose read-only inside otherwise-hidden dirs.
# After hiding the parent dir, these are recreated with original content.
_CC_EXPOSE_FILES: list[str] = [
    ".aws/config",
]

# CC mode: individual sensitive files that aren't inside the hidden dirs above.
# These require file-level (not directory-level) sandbox enforcement.
_CC_FILES: list[str] = [
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # KiroCrew's channel-credential file. The data home moved to ~/.kiro/crew,
    # so the live .env is now ~/.kiro/crew/.env; the legacy ~/.kirocrew/.env is
    # kept covered too (a not-yet-migrated box still holds real secret bytes).
    ".kiro/crew/.env",
    ".kirocrew/.env",
]


def _hidden_path_contains_visible_path(
    hidden_path: str,
    visible_paths: tuple[str, ...],
) -> bool:
    """Return whether hiding *hidden_path* would also hide a required path."""

    hidden = os.path.abspath(hidden_path)
    for item in visible_paths:
        visible = os.path.abspath(item)
        try:
            if os.path.commonpath((hidden, visible)) == hidden:
                return True
        except ValueError:
            continue
    return False


# Sensitive env var prefixes to scrub from the child environment.
# Scrubbed in ALL modes (standard + strict) — credential_process reads
# from ~/.aws/config, not env vars, so scrubbing is always safe.
_SENSITIVE_ENV_PREFIXES: list[str] = [
    "AWS_SECRET",
    "AWS_SESSION",
    "SSH_AUTH_SOCK",
    "GNUPGHOME",
    "GIT_ASKPASS",
]

# Python interpreter env that must NOT leak into a *foreign* Python subprocess
# launched under the sandbox (e.g. the MCP servers kiro-cli spawns, such as
# ord-mcp, which bundle their own interpreter + deps, or any Python the agent's
# shell runs).
#  - PYTHONPATH / PYTHONHOME: Kiro Crew's runtime may export PYTHONPATH
#    pointing at its own site-packages; a foreign server that inherits it
#    prepends Kiro Crew's site-packages to sys.path and imports Kiro Crew's
#    fastmcp/cryptography instead of its own -> ABI collision + init hang.
#  - PYTHONPYCACHEPREFIX: the packaged desktop app exports it at
#    ``<data home>/cache/pycache`` so the embedded interpreter keeps bytecode
#    out of the signed bundle. Inherited into the agent subtree, every foreign
#    interpreter (uv-managed pythons, ephemeral venvs the agent's bash spawns)
#    mirrors its whole stdlib + site-packages under the crew home instead of
#    writing ``__pycache__`` beside its own sources; each ephemeral root mints
#    a fresh path-keyed mirror, so the cache grows without bound (multi-GB per
#    day under heavy subagent use). ``pycache_gc.prune_pycache`` bounds what
#    the gateway's own tree still writes there.
#  - PYTHONDONTWRITEBYTECODE: the packaged macOS app exports it instead of the
#    prefix (its tree ships fully precompiled, see gateway-env.js), so the
#    bundled interpreter never writes into the sealed .app. It is a statement
#    about OUR bundle, not about the user's Python: inherited by a foreign
#    interpreter it silently disables bytecode caching for the user's own
#    projects -- pytest's assertion rewriter falls back to rewriting in memory
#    on every run, for one -- which is a slowdown nobody asked for and would
#    struggle to attribute to the desktop app.
# Stripped ONLY when the caller passes ``strip_python_env=True`` (the
# kiro-cli / agent spawn path). It is deliberately NOT part of
# ``_SENSITIVE_ENV_PREFIXES`` because KiroCrew's OWN sandboxed Python
# subprocesses (cron scripts, app backends, code-review workers) import
# ``kiro_crew`` via PYTHONPATH -- and on the packaged app run the BUNDLED
# interpreter, which must keep the bytecode settings so it never writes into
# the signed bundle -- so both would break if stripped.
_PYTHON_ENV_PREFIXES: list[str] = [
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONPYCACHEPREFIX",
    "PYTHONDONTWRITEBYTECODE",
]

# Gateway-owned credentials must never reach agent-influenced subprocesses.
# This list feeds the cc/strict launcher scrub, the always-on ``scrub_env``
# parent scrub, and the narrower ``scrub_agent_denied_env`` compatibility helper.
# ACP spawn paths use ``scrub_agent_subprocess_env`` so Windows Kiro delegation
# has the same parent-side scrub as the POSIX sandbox launchers. Loader coverage
# is pinned by regression test.
_AGENT_DENIED_ENV_KEYS: list[str] = [
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_USER_TOKEN",
    "WECOM_BOT_ID",
    "WECOM_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "WEBEX_BOT_TOKEN",
    "MICROSOFT_APP_ID",
    "MICROSOFT_APP_PASSWORD",
    "MICROSOFT_APP_TENANT_ID",
    "WEIXIN_TOKEN",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "JIRA_API_TOKEN",
    "JIRA_TOKEN_",
    "KIROCREW_OWNER_ID",
    # The central-governance fetch configuration — see
    # ``platform/policy_distribution.py``. The URL is listed as well as the header,
    # deliberately:
    #
    # * ``KIROCREW_POLICY_HEADERS`` is a live bearer credential for the fleet's own
    #   control plane, and with it an agent could read the ceiling document that the
    #   ``is_sensitive_path`` keystone exists to keep it from reading on disk;
    # * ``KIROCREW_POLICY_URL`` is credential-bearing in its own right whenever the
    #   fleet uses a pre-signed object URL, where the signature rides in the query
    #   string — and even unsigned it names the control plane, which the SEL, the
    #   policy viewer and ``RefreshOutcome.detail`` all deliberately withhold.
    #
    # Spelled as CONCRETE NAMES rather than a ``KIROCREW_POLICY_`` prefix, because this
    # list has consumers with two different matching rules: the spawn scrubs here use
    # ``startswith``, but ``cron_script._CRON_ENV_DENY`` tests exact membership and
    # ``mcp_cron`` builds ``\b``-anchored regexes from it, and a prefix entry silently
    # matches nothing in either. ``test_governance_distribution`` pins these against
    # ``POLICY_DISTRIBUTION_ENV_VARS``, which owns the set, so a variable added there
    # cannot quietly stay agent-readable.
    "KIROCREW_POLICY_URL",
    "KIROCREW_POLICY_HEADERS",
    "KIROCREW_POLICY_REFRESH_SECS",
    "KIROCREW_POLICY_TIMEOUT_SECS",
    "KIROCREW_POLICY_MAX_CACHE_AGE_SECS",
    "KIROCREW_POLICY_ON_UNAVAILABLE",
    "KIROCREW_POLICY_CACHE_ONLY",
]


# ── Platform context accessor ──


def _sandbox_policy():
    """Return the active context's SandboxPolicy adapter.

    The Default adapter delegates to ``_STRICT_DIRS`` / ``_CC_DIRS`` above, so a
    standalone process gets today's exact lists; the internal companion extends
    them.
    """
    return current_context().sandbox


# ── Availability probes ──


# unshare(2) flags for the userns probe.
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS = 0x00020000

# Errnos that indicate a TRANSIENT resource failure (fork/CDLL under momentary
# pressure) — the kernel supports user namespaces, we just couldn't verify it
# right now. These must never be treated as "this host has no sandbox backend"
# (one EAGAIN during a cron spawn burst would otherwise fail-close every
# subsequent spawn because the failed probe result was cached).
_TRANSIENT_PROBE_ERRNOS = frozenset(
    {errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.ENOSPC}
)

# Delay before the single in-probe retry on a transient failure.
_PROBE_TRANSIENT_RETRY_DELAY_SECS = 0.05

# Ceiling on how long a blocking ``warm_backend`` waits for the probe to land.
# The probe itself is a fork + unshare (sub-millisecond) and the warm thread
# makes at most two attempts separated by one _PROBE_TRANSIENT_RETRY_DELAY_SECS
# sleep, so this is orders of magnitude of slack rather than a tuned value — it
# exists so a wedged probe cannot stall boot indefinitely. Exceeding it is not
# an error: the cache stays cold and the self-healing transient path applies.
_WARM_JOIN_TIMEOUT_SECS = 2.0

# Steps of the launcher's namespace handshake, named in probe failure reasons so
# a caller can tell the host mechanisms apart instead of seeing a bare errno: a
# NEWNS denial is Ubuntu's AppArmor userns restriction, while NEWUSER with
# ENOSPC/EUSERS is a hardened user.max_user_namespaces=0. ENOSPC is ALSO what
# momentary fd/disk pressure looks like, so the cap verdict stays TRANSIENT and
# is never cached; the remedy travels with it so a host at a cap of 0 — which is
# reported transient forever — still gets told which sysctl to raise.
_PROBE_STEP_NEWUSER = "unshare(CLONE_NEWUSER)"
_PROBE_STEP_NEWNS = "unshare(CLONE_NEWNS)"
#: Wire step the probe child sends INSTEAD of "U" when its ``CLONE_NEWUSER`` EINVAL
#: is explained by the child having been multithreaded, carrying the thread count.
#: The parent classifies it exactly as it classifies a plain EINVAL -- the step
#: exists to carry the explanation, not to change the verdict. See
#: ``_probe_child_thread_count``.
_PROBE_STEP_MULTITHREADED = "M"

#: Trailing clause of the reason `_probe_parent_sequence` emits for the
#: `_PROBE_STEP_MULTITHREADED` collapse. A single shared spelling, because callers
#: that must RECOGNIZE the collapse (a fork child whose verdict is unobtainable is
#: an unknown reading, not a disagreement) match against the reason text -- a
#: hand-copied substring would drift the moment the wording changes.
_PROBE_MULTITHREADED_REASON = (
    "an os.register_at_fork hook started one, so the kernel's own verdict is "
    "unobtainable from this child"
)


def _probe_reason_is_multithreaded_collapse(reason: str) -> bool:
    """Whether a probe reason reports the multithreaded-fork-child collapse.

    True only for the fork path: the collapse text is emitted by
    `_probe_parent_sequence` when the probe child counted more than one thread,
    which happens when an ``os.register_at_fork`` hook armed earlier in the
    calling process starts a thread inside every fork child. The verdict such a
    child returns is the hook's artifact, not the kernel's answer.
    """
    return _PROBE_MULTITHREADED_REASON in (reason or "")


# A probe child that vanished mid-handshake is a harness failure, not a kernel
# verdict, so it must not be cached as "this host has no sandbox". Kept separate
# from _TRANSIENT_PROBE_ERRNOS so that set's cache semantics stay untouched.
# ESRCH/ENOENT surface when opening /proc/<pid>/... for a dead child; EPIPE
# surfaces when releasing a child that died after the maps were written.
_PROBE_CHILD_GONE_ERRNOS = frozenset({errno.ESRCH, errno.ENOENT, errno.EPIPE})

# Upper bound on the probe's pipe handshake. The real exchange is
# sub-millisecond; this only stops a pathological child from wedging the
# background warm thread forever.
_PROBE_HANDSHAKE_TIMEOUT_SECS = 5.0

# Detail of the most recent failed userns probe: (transient, reason, remedy).
# ``None`` means the last probe succeeded (or none has run yet). Consumed by
# detect_backend() for cache policy and by wrap_argv() for error reporting.
#
# One value, swapped atomically, so a reader always gets a reason and the remedy
# from the SAME probe. Holding the remedy in a second global would let a
# concurrent re-probe land between the two reads and pair one probe's failure
# with another's mechanism, which is the wrong fix presented as the right one.
_last_unshare_failure: tuple[bool, str, str] | None = None

# ── Remedy tokens for a Linux user-namespace denial ──
# The probe already knows WHICH step failed and with which errno, and those two
# facts identify the host mechanism (see the table in docs/guides/install.md).
# That knowledge used to die inside the reason string, leaving every presentation
# layer to show a bare ``errno 1 (EPERM)`` and no way forward — issue #1660.
# These tokens carry the mechanism out to callers machine-readably, so the
# dashboard, doctor and logs can each render their own remedy copy instead of
# pattern-matching English prose out of the detail.
#
# A token travels IN-BAND: every probe step returns it alongside its verdict, so
# it is never module state that a second probe could overwrite. ``""`` means the
# failure identifies no mechanism — a harness failure, a non-Linux host, or a
# deferred on-loop probe.
REMEDY_APPARMOR_USERNS = "apparmor_userns"  # Ubuntu >= 23.10 restricted profile
REMEDY_MAX_USER_NAMESPACES = "max_user_namespaces"  # user.max_user_namespaces=0
REMEDY_NO_USER_NS = "no_user_ns"  # kernel built without CONFIG_USER_NS
REMEDY_USERNS_DENIED = "userns_denied"  # userns creation refused outright


def _remedy_for_step(label: str, err: int) -> str:
    """Name the host mechanism behind one failed unshare step.

    ``label`` is one of the two ``_PROBE_STEP_*`` constants for a real kernel
    verdict; any other label is a harness failure (fork/pipe under pressure)
    which says nothing about the host and therefore has no remedy.

    A NEWNS denial is only reachable AFTER NEWUSER succeeded, which is the
    signature of Ubuntu's restricted-profile restriction rather than of userns
    being unavailable — the distinction that decides whether the fix is an
    AppArmor profile or a sysctl.
    """
    if label == _PROBE_STEP_NEWNS:
        return REMEDY_APPARMOR_USERNS if err == errno.EPERM else ""
    if label != _PROBE_STEP_NEWUSER:
        return ""
    if err in (errno.ENOSPC, errno.EUSERS):
        return REMEDY_MAX_USER_NAMESPACES
    if err in (errno.EINVAL, errno.ENOSYS):
        return REMEDY_NO_USER_NS
    if err == errno.EPERM:
        return REMEDY_USERNS_DENIED
    return ""


# Concrete, mechanism-specific first line for the ``no_backend`` guidance in a
# SandboxUnavailableError message. Kept as prose here (rather than only as a
# token) because logs, doctor and the Slack surface all read the message text —
# only the dashboard consumes the token and renders its own translated copy.
_LINUX_REMEDY_GUIDANCE = {
    REMEDY_APPARMOR_USERNS: (
        "This host looks like Ubuntu 23.10 or newer with "
        "kernel.apparmor_restrict_unprivileged_userns=1: the user namespace was "
        "created, then the mount namespace was denied because the restricted "
        "AppArmor profile carries no CAP_SYS_ADMIN. Run `kirocrew service "
        "install` to install the narrow kirocrew-userns AppArmor profile (it "
        "grants only `userns` and applies to the kirocrew service alone). "
        "systemd is what attaches that profile, so the service is the only path "
        "that applies it — a gateway started by hand stays unconfined, and "
        "`aa-exec -p` cannot fix that for an unprivileged user because entering "
        "a named profile needs privilege and aa-exec execs unconfined rather "
        "than failing. The desktop app reuses a gateway already listening on the "
        "port, so installing the service covers that install too. Do NOT set the "
        "sysctl to 0 — that removes a kernel-wide protection to satisfy one app. "
    ),
    REMEDY_MAX_USER_NAMESPACES: (
        "User namespace creation hit the per-user cap, which usually means "
        "user.max_user_namespaces=0 (a CIS-hardened default). Raise that sysctl. "
    ),
    REMEDY_NO_USER_NS: (
        "The kernel rejected the user namespace outright, which means it was "
        "built without CONFIG_USER_NS. There is no host-level fix short of a "
        "different kernel. "
    ),
    REMEDY_USERNS_DENIED: (
        "User namespace creation was refused. On Debian-family hosts check "
        "kernel.unprivileged_userns_clone (it must be 1); inside a container "
        "this is usually the container's own seccomp filter denying unshare, "
        "which is fixed with container run flags rather than host config. "
    ),
}


def _linux_remedy_guidance(remedy: str) -> str:
    """Mechanism-specific guidance prefix for a remedy token (``""`` if none)."""
    return _LINUX_REMEDY_GUIDANCE.get(remedy, "")


def unavailable_remedy() -> str:
    """Public: remedy token for the most recent sandbox probe failure.

    ``""`` when the last probe succeeded, when none has run, or when the failure
    identifies no host mechanism. Pair it with :func:`unavailable_kind` — a
    ``"transient"`` failure is momentary resource pressure and must never be
    presented as something the operator should reconfigure.
    """
    if _last_unshare_failure is None:
        return ""
    return _last_unshare_failure[2]


def unavailable_reason() -> str:
    """Public: technical reason for the most recent sandbox probe failure.

    Names the failing step verbatim (e.g. ``"unshare(CLONE_NEWNS) failed with
    errno 1 (EPERM)"``), so a diagnostic surface can show the kernel's answer
    rather than a paraphrase. ``""`` when the last probe succeeded or none has
    run. Sibling accessor to :func:`unavailable_remedy`, reading the same
    recorded failure so the two can never describe different probes.
    """
    if _last_unshare_failure is None:
        return ""
    return _last_unshare_failure[1]


def remedy_guidance(remedy: str) -> str:
    """Public: mechanism-specific guidance for a ``REMEDY_*`` token (``""`` if none).

    The stable cross-module entry point for :data:`_LINUX_REMEDY_GUIDANCE`, so
    diagnostic surfaces (doctor, dashboard) render the one shared remedy text
    for a mechanism instead of maintaining a drifting copy.
    """
    return _linux_remedy_guidance(remedy)


def _close_probe_fds(*fds: int) -> None:
    """Close probe pipe fds, tolerating an already-closed one. Never raises.

    ``os.close`` blocks, but every probe path runs off the event loop
    (``_probe_unshare`` defers to the background warm thread when a loop is
    running), so this does not breach the no-blocking-call-on-event-loop rule.
    """
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


_PROBE_CHILD_FD_SWEEP_CAP = 4096
"""Fallback bound for the probe child's inherited-fd close sweep.

Used only when ``SC_OPEN_MAX`` cannot be read or answers nonsense. When
sysconf answers, its value (the soft ``RLIMIT_NOFILE``) is trusted as the
bound: ``os.closerange`` delegates to ``close_range(2)`` on Linux >= 5.9, so
a wide span costs one syscall rather than a walk, and silently clamping the
bound would leave a high-numbered lock fd open with no diagnostic that the
sweep came up short.
"""


def _fd_sweep_ranges(keep: frozenset[int], limit: int | None = None) -> tuple[tuple[int, int], ...]:
    """Precompute the ``os.closerange`` spans covering ``[0, bound)`` minus *keep*.

    Runs in the PARENT, before ``os.fork()``. The probe child of a threaded
    process must not allocate or take locks — another thread may own the
    allocator lock at fork time and vanish, leaving it held forever in the
    child — so everything that sorts, boxes, or asks ``sysconf`` happens here,
    and the child is left executing bare ``closerange`` syscalls over the
    returned pairs (:func:`_close_fd_ranges`).

    The bound is ``SC_OPEN_MAX`` (the soft ``RLIMIT_NOFILE``);
    :data:`_PROBE_CHILD_FD_SWEEP_CAP` applies only when sysconf cannot answer.
    ``limit`` exists for tests. Never raises.
    """
    if limit is None:
        try:
            limit = int(os.sysconf("SC_OPEN_MAX"))
        except (AttributeError, OSError, ValueError):
            # AttributeError: os.sysconf does not exist off-POSIX (Windows);
            # the sweep only runs on Linux, but this helper must keep its
            # never-raises contract everywhere the tests exercise it.
            limit = _PROBE_CHILD_FD_SWEEP_CAP
    if limit <= 0:
        limit = _PROBE_CHILD_FD_SWEEP_CAP
    ranges: list[tuple[int, int]] = []
    low = 0
    for fd in sorted(k for k in keep if k >= 0):
        if fd >= limit:
            break
        if fd > low:
            ranges.append((low, fd))
        low = fd + 1
    if low < limit:
        ranges.append((low, limit))
    return tuple(ranges)


def _close_fd_ranges(ranges: tuple[tuple[int, int], ...]) -> None:
    """Close the precomputed fd spans: the probe child's half of the sweep.

    Runs between ``os.fork()`` and ``os._exit`` in a child that never execs,
    so ``O_CLOEXEC`` never fires and every inherited descriptor — the
    ``gateway.lock`` flock fd and the dashboard listen socket included — is
    still open. Without the sweep, a probe child orphaned by its parent's
    death (gateway OOM-killed between fork and reap) keeps the lock fd open
    and pins the data home until someone reclaims it.

    Only ``os.closerange`` is invoked here: the spans were computed pre-fork
    by :func:`_fd_sweep_ranges` precisely so this post-fork path does no
    allocation-bearing work beyond iterating a ready tuple. ``closerange``
    ignores bad fds, so this never raises.
    """
    for low, high in ranges:
        os.closerange(low, high)


def _probe_failure(label: str, err: int) -> tuple[bool, bool, str, str]:
    """Shape one failed probe step into ``(ok, transient, reason)``.

    EPERM stays PERMANENT: an AppArmor userns denial, or a kernel built without
    CONFIG_USER_NS, will not clear on a retry, and caching that verdict is what
    makes ``detect_backend()`` honest. Only the momentary-resource errnos are
    transient — widening that set caused incident 2026-07-18, where one EAGAIN
    was cached as "this host has no sandbox" for an hour.

    Returns the step's remedy token IN-BAND with the verdict. Carrying it in a
    module global instead would let a second, concurrent probe interleave between
    one probe staging its token and the caller reading it, recording a reason with
    the wrong mechanism.
    """
    name = errno.errorcode.get(err, "?")
    return (
        False,
        err in _TRANSIENT_PROBE_ERRNOS,
        f"{label} failed with errno {err} ({name})",
        _remedy_for_step(label, err),
    )


def _probe_harness_failure(label: str, err: int) -> tuple[bool, bool, str, str]:
    """Classify a probe-scaffolding failure, treating a vanished child as transient.

    A child that dies mid-handshake reaches the parent as ESRCH/ENOENT on a
    ``/proc`` map write, or EPIPE on the write that releases it. None of those is
    a kernel verdict about user namespaces, so caching them permanently would
    strand every later spawn until restart — the incident-2026-07-18 shape.
    """
    if err in _PROBE_CHILD_GONE_ERRNOS:
        name = errno.errorcode.get(err, "?")
        return (False, True, f"{label} failed with errno {err} ({name})", "")
    return _probe_failure(label, err)


def _probe_child_unshare(libc: ctypes.CDLL, flags: int) -> int:
    """Run one ``unshare(2)`` in the probe child; return 0 or the errno.

    A module-level seam so a test can simulate the Ubuntu >= 23.10 shape
    ("NEWUSER ok, NEWNS EPERM") without needing a restricted kernel.
    """
    ctypes.set_errno(0)
    if libc.unshare(flags) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def _probe_child_thread_count() -> int:
    """Live threads in the probe child, or 0 when it cannot be determined.

    ``unshare(CLONE_NEWUSER)`` implies ``CLONE_THREAD``, which the kernel refuses
    with **EINVAL** unless the caller's thread group holds exactly one task. A
    ``fork()`` child is single-threaded by construction, so this normally reads 1 --
    but ``os.register_at_fork`` handlers run INSIDE ``os.fork()``, before it
    returns, and a library can start a thread there. OpenTelemetry's metric SDK does
    exactly that: its ``PeriodicExportingMetricReader`` registers an
    ``after_in_child`` hook that restarts its exporter thread in every child.

    Used ONLY to explain an EINVAL, never to reclassify one. EINVAL is genuinely
    ambiguous here -- a kernel built without ``CONFIG_USER_NS`` returns it too, and a
    multithreaded child cannot tell the two apart, because it never gets far enough to
    ask. Calling it transient would be just as wrong as calling it permanent, and it
    would additionally withhold the ``no_backend`` opt-in (``sandbox_allow_unsandboxed_exec``)
    from a host that really has no user namespaces. So the classification stays exactly
    as it was and the REASON names the thread, which is the part a reader cannot infer:
    a bare "errno 22 (EINVAL)" sends them to check their kernel config, which is the
    wrong place. Making such a process probe successfully needs a single-threaded
    child, i.e. a different spawn mechanism, and that is its own change.

    ``st_nlink`` of ``/proc/self/task`` is ``2 + threads`` (each thread is a
    subdirectory), so this is one ``stat`` and no list: the probe child of a threaded
    process must not allocate, because another thread may have owned the allocator lock
    at fork time and no longer exists to release it. Linux-only, like the rest of the
    probe.
    """
    try:
        return max(0, os.stat("/proc/self/task").st_nlink - 2)
    except OSError:
        return 0


def _probe_write_identity_maps(pid: int, uid: int, gid: int) -> tuple[str, int] | None:
    """Write the probe child's identity maps, exactly as the launcher's parent does.

    Returns ``None`` on success, else ``(label, errno)`` for the first
    ``/proc/<pid>/`` file that could not be written. A child that died between
    the fork and this write surfaces here as ESRCH/ENOENT instead of raising.
    """
    for name, payload in (
        ("setgroups", "deny"),
        ("uid_map", f"{uid} {uid} 1\n"),
        ("gid_map", f"{gid} {gid} 1\n"),
    ):
        try:
            with open(f"/proc/{pid}/{name}", "w") as handle:
                handle.write(payload)
        except OSError as exc:
            return (f"/proc/<pid>/{name} write", exc.errno or 0)
    return None


def _probe_read_step(fd: int) -> tuple[str, int] | None:
    """Read one ``<step>:<errno>`` report from the probe child.

    ``None`` means the child closed the pipe without reporting, sent junk, or
    stayed silent past the handshake deadline — the deadline being what stops a
    pathological child from wedging the background warm thread forever.

    Uses ``poll`` rather than ``select``: ``select`` raises ``ValueError`` once a
    descriptor reaches FD_SETSIZE (1024), and a long-lived gateway can easily
    hand the probe a pipe fd past that. Raising there would kill the warm thread
    and leave ``wrap_argv`` rejecting every sandboxed spawn.
    """
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    deadline = time.monotonic() + _PROBE_HANDSHAKE_TIMEOUT_SECS
    buf = b""
    try:
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                if not poller.poll(max(1, int(remaining * 1000))):
                    return None
                chunk = os.read(fd, 32)
            except OSError:
                return None
            if not chunk:
                return None  # writer closed (POLLHUP) without a full report
            buf += chunk
    finally:
        try:
            poller.unregister(fd)
        except (KeyError, OSError):
            pass
    step, _, value = buf.split(b"\n", 1)[0].decode("ascii", "replace").partition(":")
    try:
        return (step, int(value))
    except ValueError:
        return None


def _probe_child_death(pid: int) -> str:
    """Describe how a silent probe child ended, for the failure reason.

    A child killed by a signal (the OOM killer, a stray SIGKILL) is a momentary
    environmental failure rather than a kernel verdict, so naming the signal
    preserves the diagnostic the combined-call probe used to read out of the
    child's exit status. Non-blocking, so a child wedged past the handshake
    deadline is described instead of waited on.
    """
    try:
        reaped, status = os.waitpid(pid, os.WNOHANG)
    except OSError:
        return "exited without reporting"
    if reaped != pid:
        return f"stayed silent for {_PROBE_HANDSHAKE_TIMEOUT_SECS:g}s"
    if os.WIFSIGNALED(status):
        return f"killed by signal {os.WTERMSIG(status)}"
    if os.WIFEXITED(status):
        return f"exited with status {os.WEXITSTATUS(status)}"
    return "exited without reporting"


def _probe_reap(pid: int) -> None:
    """Reap the probe child on every exit path so no zombie or stuck child leaks.

    Reaps a child that already exited without signalling it — the common case,
    and the one that must never send SIGKILL at a pid the kernel could have
    recycled. Only a child still running after the handshake ended (it cannot
    make progress: its pipes are closed) is killed, which bounds the reap
    without spinning. ``platform_compat.kill_pid`` deliberately propagates
    ``ProcessLookupError``, so an exit in that race is caught here.
    """
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return
    except OSError:
        return  # not our child, or already reaped
    try:
        platform_compat.kill_pid(pid, platform_compat.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


def _probe_child_sequence(
    libc: ctypes.CDLL,
    c2p_r: int,
    c2p_w: int,
    p2c_r: int,
    p2c_w: int,
    sweep_ranges: tuple[tuple[int, int], ...],
) -> None:
    """Probe child: run the launcher's two unshare steps, reporting each on the pipe.

    Never returns. It reports raw errnos and classifies nothing, so the entire
    verdict lives in the parent where a test can drive it without forking.
    ``sweep_ranges`` was computed pre-fork by :func:`_fd_sweep_ranges` so this
    path performs no allocation-bearing bookkeeping of its own.
    """
    try:
        _close_probe_fds(c2p_r, p2c_w)
        # Drop every other inherited descriptor before touching namespaces:
        # an orphaned probe child must not keep the gateway.lock fd (or the
        # dashboard listen socket) open and pin the home. Only the handshake
        # ends and the standard streams survive. (#3150)
        _close_fd_ranges(sweep_ranges)
        # Read BEFORE the unshare: it is the only moment the count is the one the
        # kernel judged. Reported only alongside an EINVAL, and only to explain it --
        # see _probe_child_thread_count for why it must not change the verdict.
        threads = _probe_child_thread_count()
        err = _probe_child_unshare(libc, _CLONE_NEWUSER)
        if err == errno.EINVAL and threads > 1:
            os.write(c2p_w, b"M:%d\n" % threads)
            os._exit(0)
        os.write(c2p_w, b"U:%d\n" % err)
        if err:
            os._exit(0)
        # NEWNS needs a mapped UID, so wait for the parent's maps first. This
        # ordering is the entire point of the probe.
        if not os.read(p2c_r, 1):
            os._exit(0)  # parent abandoned the handshake; it already has a verdict
        os.write(c2p_w, b"N:%d\n" % _probe_child_unshare(libc, _CLONE_NEWNS))
        os._exit(0)
    except BaseException:
        os._exit(1)


def _probe_parent_sequence(
    pid: int,
    c2p_r: int,
    p2c_w: int,
    uid: int,
    gid: int,
    death: Callable[[int], str] = _probe_child_death,
) -> tuple[bool, bool, str, str]:
    """Parent half of the probe: drive the handshake and decide the verdict.

    ``death`` describes a child that stopped reporting. It is injected because the
    spawned probe's child is owned by a ``Popen`` -- calling ``waitpid`` on it here
    would race that object's own bookkeeping -- while the forked probe's child is
    reaped by this module. The verdict logic is identical for both.
    """
    report = _probe_read_step(c2p_r)
    if report is None:
        return (False, True, f"probe child {death(pid)}; no {_PROBE_STEP_NEWUSER} result", "")
    step, err = report
    if step == _PROBE_STEP_MULTITHREADED:
        # Same classification and same remedy as a plain EINVAL -- deliberately, see
        # _probe_child_thread_count. Only the reason gains the thread count, because
        # that is the one part a reader cannot infer from the errno.
        ok, transient, reason, remedy = _probe_failure(_PROBE_STEP_NEWUSER, errno.EINVAL)
        return (
            ok,
            transient,
            f"{reason}; the probe child had {err} threads, which alone makes it "
            "return EINVAL (CLONE_NEWUSER implies CLONE_THREAD) -- "
            f"{_PROBE_MULTITHREADED_REASON}",
            remedy,
        )
    if step != "U":
        return (False, True, f"probe child sent unexpected step {step!r}", "")
    if err:
        return _probe_failure(_PROBE_STEP_NEWUSER, err)

    failed_map = _probe_write_identity_maps(pid, uid, gid)
    if failed_map is not None:
        label, map_errno = failed_map
        return _probe_harness_failure(label, map_errno)

    try:
        os.write(p2c_w, b"x")
    except OSError as exc:
        return _probe_harness_failure("probe handshake write", exc.errno or 0)

    report = _probe_read_step(c2p_r)
    if report is None:
        return (False, True, f"probe child {death(pid)}; no {_PROBE_STEP_NEWNS} result", "")
    step, err = report
    if step != "N":
        return (False, True, f"probe child sent unexpected step {step!r}", "")
    if err:
        return _probe_failure(_PROBE_STEP_NEWNS, err)
    return (True, False, "ok", "")


def _probe_unshare_once() -> tuple[bool, bool, str, str]:
    """One launcher-shaped namespace probe: ``(ok, transient, reason)``.

    Runs in a FRESH interpreter when one can be spawned, and falls back to
    :func:`_probe_unshare_via_fork` otherwise. The two produce the same verdict
    tuple through the same classifier; only the process the child half runs in
    differs. See :data:`_PROBE_SHIM_CODE` for why that difference is the whole
    point.
    """
    spawned = _probe_unshare_via_spawn()
    if spawned is not None:
        return spawned
    return _probe_unshare_via_fork()


#: Child half of the probe, run in a FRESH interpreter rather than a fork of the
#: caller. Same wire protocol as :func:`_probe_child_sequence`, so the reviewed
#: parent half drives either one unchanged.
#:
#: WHY A FRESH PROCESS. ``unshare(CLONE_NEWUSER)`` implies ``CLONE_THREAD`` and the
#: kernel refuses it with EINVAL unless the caller's thread group holds exactly one
#: task. A fork child inherits that condition: ``os.register_at_fork`` handlers run
#: INSIDE ``os.fork()`` before it returns, so a dependency that restarts a thread in
#: every child -- OpenTelemetry's ``PeriodicExportingMetricReader`` does exactly
#: this -- makes the child multithreaded before the probe can measure anything. The
#: verdict is then EINVAL, which is classified permanent and cached, and every later
#: sandboxed spawn on that process fails closed. A release gate lost 40 tests to one
#: such probe: all of them pass alone, none of them is a metrics test.
#:
#: A fresh interpreter starts single-threaded and runs no after-in-child fork hooks,
#: so its thread count at ``unshare()`` time is 1 regardless of the caller. This does
#: NOT soften the fail-closed rule: a genuinely single-threaded process that still
#: gets EINVAL means the host lacks ``CONFIG_USER_NS``, which stays permanent. It
#: removes the FALSE EINVAL, not the real one.
#:
#: It is also the more faithful probe. The real launcher is already a fresh
#: interpreter -- ``wrap_argv`` returns ``[sys.executable, launcher_path, ...]`` --
#: which then forks and unshares. So the fork-based probe was strictly MORE
#: pessimistic than the spawn it predicts, and this makes the two agree.
#:
#: Kept deliberately free of ``kiro_crew`` imports and run under ``-I -S``, like
#: ``_SPAWN_SHIM_CODE``: no site directory, no ``PYTHON*`` environment influence,
#: nothing to shadow. It writes ONLY wire steps on fd 1.
_PROBE_SHIM_CODE = r"""
import ctypes, errno, os

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000


def threads():
    # st_nlink of /proc/self/task is the thread count plus the two dir entries.
    try:
        return max(1, os.stat("/proc/self/task").st_nlink - 2)
    except OSError:
        return 1


def unshare(libc, flags):
    ctypes.set_errno(0)
    if libc.unshare(flags) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def main():
    try:
        # dlopen(NULL): resolve unshare() from the libc ALREADY loaded into this
        # interpreter. Never ctypes.util.find_library here -- on Linux it EXECUTES
        # helper processes (ldconfig, then a PATH-resolved gcc/cc/objdump on musl
        # hosts) to locate libc, and this probe runs before any confinement, so a
        # workspace-controlled `gcc` on PATH would be same-user code execution.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.unshare.argtypes = [ctypes.c_int]
        libc.unshare.restype = ctypes.c_int
    except BaseException:
        os._exit(1)
    n = threads()
    err = unshare(libc, CLONE_NEWUSER)
    if err == errno.EINVAL and n > 1:
        os.write(1, b"M:%d\n" % n)
        os._exit(0)
    os.write(1, b"U:%d\n" % err)
    if err:
        os._exit(0)
    if not os.read(0, 1):
        os._exit(0)
    os.write(1, b"N:%d\n" % unshare(libc, CLONE_NEWNS))
    os._exit(0)


main()
"""

#: Ceiling on the spawned probe. The child does two syscalls and one blocking read
#: whose writer is this process, so anything near this is a wedged interpreter, not
#: slow work. Exceeding it is reported TRANSIENT: a host that cannot start a Python
#: in 20 seconds is under momentary pressure, not permanently sandbox-less.
_PROBE_SPAWN_TIMEOUT_SECONDS = 20.0

_probe_spawn_unavailable_logged = False


def _probe_spawned_death(proc: "subprocess.Popen[bytes]") -> str:
    """Describe how the spawned probe child ended, for a transient reason string."""
    code = proc.poll()
    if code is None:
        return "did not report"
    if code < 0:
        return f"was killed by signal {-code}"
    return f"exited with status {code}"


def _probe_unshare_via_spawn() -> tuple[bool, bool, str, str] | None:
    """Probe in a fresh interpreter. ``None`` means "cannot spawn, use the fork path".

    Returning ``None`` rather than a verdict is deliberate: an interpreter this
    process cannot start says nothing about the host's namespaces, so it must not
    become a sandbox verdict.
    """
    global _probe_spawn_unavailable_logged
    if not sys.executable:
        if not _probe_spawn_unavailable_logged:
            _probe_spawn_unavailable_logged = True
            logger.warning(
                "namespace probe cannot spawn a fresh interpreter (sys.executable is "
                "empty); falling back to a fork-based probe, which reports EINVAL on a "
                "multithreaded caller even where the sandbox works"
            )
        return None

    uid, gid = os.getuid(), os.getgid()
    try:
        # close_fds is subprocess's default and does the job the fork path has to do
        # by hand: the child gets only its standard streams, so an orphaned probe
        # cannot hold the gateway lock fd or the dashboard listen socket open (#3150).
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _PROBE_SHIM_CODE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        return _probe_failure("probe spawn", exc.errno or 0)
    except Exception as exc:  # pragma: no cover - defensive
        return (False, True, f"probe spawn failed: {exc}", "")

    assert proc.stdin is not None and proc.stdout is not None
    try:
        return _probe_parent_sequence(
            proc.pid,
            proc.stdout.fileno(),
            proc.stdin.fileno(),
            uid,
            gid,
            death=lambda _pid: _probe_spawned_death(proc),
        )
    finally:
        # Closing stdin releases a child still waiting on the maps; the wait then
        # reaps it. Popen owns the pid, so _probe_reap must NOT run here.
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=_PROBE_SPAWN_TIMEOUT_SECONDS)
        except Exception:  # pragma: no cover - a wedged interpreter
            proc.kill()
            try:
                proc.wait(timeout=_PROBE_SPAWN_TIMEOUT_SECONDS)
            except Exception:
                pass


def _probe_unshare_via_fork() -> tuple[bool, bool, str, str]:
    """The fork-based probe: same verdict, but the child inherits fork hooks.

    Retained as the fallback for a process that cannot spawn an interpreter at all.
    Its child can be made multithreaded by an ``os.register_at_fork`` handler, which
    is why :func:`_probe_unshare_via_spawn` is preferred whenever it is available.

    Mirrors the sequence ``_build_launcher_script()`` actually performs — fork,
    child ``unshare(CLONE_NEWUSER)``, parent writes the identity UID/GID map,
    child ``unshare(CLONE_NEWNS)`` — because the two flags do NOT behave the
    same way when combined. A single ``unshare(CLONE_NEWUSER | CLONE_NEWNS)`` is
    satisfied atomically and therefore SUCCEEDS on hosts where the split
    sequence fails: with Ubuntu's ``kernel.apparmor_restrict_unprivileged_userns
    = 1`` (the default since 23.10), creating a user namespace moves the process
    into a restricted AppArmor profile carrying no CAP_SYS_ADMIN, so the
    *second* unshare returns EPERM. The previous combined probe reported those
    hosts as sandbox-capable and every real spawn then died with
    ``sandbox: unshare(NEWNS) failed: errno 1``.

    ``reason`` names the failing step so a caller can tell the mechanisms apart
    — a NEWNS denial is the AppArmor userns restriction, whereas NEWUSER with
    ENOSPC/EUSERS is ``user.max_user_namespaces=0`` — rather than reporting a
    bare errno that fits both.

    Linux-only and off-loop only: ``_probe_unshare()`` guards the platform and
    defers to the background warm thread when a loop is running, so the fork,
    pipe reads and ``waitpid`` here never block the event loop.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.unshare.argtypes = [ctypes.c_int]
        libc.unshare.restype = ctypes.c_int
    except OSError as exc:
        return (False, exc.errno in _TRANSIENT_PROBE_ERRNOS, f"libc load failed: {exc}", "")
    except Exception as exc:  # find_library returning junk, ABI issues, ...
        return (False, False, f"libc load failed: {exc}", "")

    uid, gid = os.getuid(), os.getgid()
    try:
        c2p_r, c2p_w = os.pipe()
    except OSError as exc:
        return _probe_failure("probe pipe", exc.errno or 0)
    try:
        p2c_r, p2c_w = os.pipe()
    except OSError as exc:
        _close_probe_fds(c2p_r, c2p_w)
        return _probe_failure("probe pipe", exc.errno or 0)

    # Compute the child's fd sweep BEFORE forking: sorting, sysconf, and tuple
    # building all allocate, and post-fork the allocator lock may be held by a
    # thread that no longer exists in the child. (#3150)
    sweep_ranges = _fd_sweep_ranges(frozenset({0, 1, 2, c2p_w, p2c_r}))

    try:
        pid = os.fork()
    except OSError as exc:
        _close_probe_fds(c2p_r, c2p_w, p2c_r, p2c_w)
        return _probe_failure("fork", exc.errno or 0)

    if pid == 0:
        _probe_child_sequence(libc, c2p_r, c2p_w, p2c_r, p2c_w, sweep_ranges)  # never returns
        os._exit(1)  # pragma: no cover - defensive

    _close_probe_fds(c2p_w, p2c_r)
    try:
        return _probe_parent_sequence(pid, c2p_r, p2c_w, uid, gid)
    finally:
        # Closing p2c_w also releases a child still waiting on the maps.
        _close_probe_fds(c2p_r, p2c_w)
        _probe_reap(pid)


# ── Background warm thread (never-block-on-loop policy) ──
# The event loop NEVER executes fork/waitpid/sleep for the probe. On-loop
# callers with a cold cache get an immediate transient "none" (fail-closed,
# self-heals in ms) and fire a background daemon thread that populates the
# cache off-loop. Boot sites call prewarm_backend() to fill the cache before
# any on-loop caller ever reaches detect_backend(), so the transient path is
# typically never hit in production.

_warm_thread: threading.Thread | None = None


def _record_probe_failure(transient: bool, reason: str, remedy: str = "") -> None:
    """Record a probe failure and its remedy token together.

    Sole writer of the pair, so a token can never outlive the failure it
    describes: a caller that records a failure without probing omits `remedy` and
    thereby clears any earlier one, instead of having to remember a separate line.
    That matters because the token is surfaced to the user even for a transient
    verdict, so a stale one would name the wrong host mechanism.
    """
    global _last_unshare_failure
    _last_unshare_failure = (transient, reason, remedy)


def _background_warm() -> None:
    """Run the probe off-loop and populate the cache. Thread target."""
    global _backend, _last_unshare_failure
    for attempt in (1, 2):
        ok, transient, reason, remedy = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            _backend = "namespace"
            logger.info("Background warm: sandbox backend = namespace")
            return
        _record_probe_failure(transient, reason, remedy)
        if not transient:
            logger.warning("Background warm: probe permanent failure: %s", reason)
            _backend = "none"
            return
        logger.warning("Background warm: probe transient (attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    # Both attempts transient — leave cache uncached (None) so next call re-tries
    logger.warning("Background warm: both attempts transient, cache stays cold")


def _kick_background_warm() -> None:
    """Start the background warm thread if not already running.

    Thread-start failure (RuntimeError under thread exhaustion) is swallowed:
    the cache stays cold and the pre-existing self-healing transient path
    applies on the next spawn.  This keeps gateway boot stable even when the
    host is resource-constrained.
    """
    global _warm_thread
    if _warm_thread is not None and _warm_thread.is_alive():
        return  # dedupe: warm already in progress
    _warm_thread = threading.Thread(target=_background_warm, name="sandbox-probe-warm", daemon=True)
    try:
        _warm_thread.start()
    except RuntimeError:
        _warm_thread = None
        logger.debug("sandbox warm thread start failed; cache stays cold")


def prewarm_backend() -> None:
    """Fire-and-forget boot hook: start background probe to fill the cache.

    Call early in gateway startup (slack/gateway.py, mcp_gateway/gatewayd.py)
    so the cache is warm before any on-loop spawn path reaches detect_backend().
    """
    if sys.platform != "linux":
        return  # probes are Linux-only
    _kick_background_warm()


def warm_backend(timeout: float = _WARM_JOIN_TIMEOUT_SECS) -> None:
    """Blocking boot hook: fill the probe cache BEFORE returning.

    ``prewarm_backend`` only *starts* the probe, so a caller that reaches
    ``detect_backend`` in the same tick still races the warm thread and gets the
    synthetic-transient answer — a cold-cache false negative that reads exactly
    like "this host has no sandbox backend". This variant waits for the probe to
    land, so the next ``detect_backend`` sees a warm cache and the transient path
    is not reachable from a warmed boot.

    The wait is bounded: ``_background_warm`` makes at most two attempts with a
    single short delay between them, and a join timeout caps the total. A timeout
    is not an error — the cache simply stays cold and the pre-existing
    self-healing transient path applies, exactly as with ``prewarm_backend``.

    **Never-block-on-loop invariant**: this blocks on a thread join, so it MUST
    NOT be called from a running event loop. On-loop callers use
    ``await asyncio.to_thread(warm_backend)``; synchronous callers (CLI paths)
    may call it directly.
    """
    if sys.platform != "linux":
        return  # probes are Linux-only
    _kick_background_warm()
    thread = _warm_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def _probe_unshare() -> bool:
    """Return True if user + mount namespaces work (Linux).

    Failures are logged with their errno and classified transient vs
    permanent in :data:`_last_unshare_failure`; a transient failure gets one
    immediate retry (off-loop only).

    **Never-block-on-loop invariant**: when called from a running asyncio
    event loop with a cold cache, this function does NOT probe — it fires
    ``_kick_background_warm()`` and returns False with a transient reason.
    The background thread populates the cache in ms; the next spawn re-checks
    and finds a warm cache. Boot prewarm ensures this path is rarely hit.

    Callers deciding cache policy (detect_backend) MUST consult the
    classification — a transient result is not evidence that the host lacks
    a sandbox backend.
    """
    global _last_unshare_failure
    if sys.platform != "linux":
        _record_probe_failure(False, "not Linux")
        return False

    # Fast path: the cache already proved user namespaces work -- no probe
    # needed. Keeps on-loop callers correct after prewarm instead of
    # deferring and returning False.
    if _backend == "namespace":
        return True

    # Detect running event loop — governs whether we probe directly or defer.
    on_loop = False
    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        pass

    if on_loop:
        # NEVER probe on the event loop. Kick background warm and fail transient.
        _kick_background_warm()
        # No probe ran, so the omitted remedy clears any older failure's token.
        _record_probe_failure(
            True,
            "probe deferred to background thread (cold cache on event loop); "
            "cache warms in ms — retry",
        )
        return False

    # Off-loop: direct probe with one retry on transient failure.
    for attempt in (1, 2):
        ok, transient, reason, remedy = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            return True
        _record_probe_failure(transient, reason, remedy)
        if not transient:
            logger.warning("userns probe failed (permanent): %s", reason)
            return False
        logger.warning("userns probe failed (transient, attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    return False


def userns_available() -> bool:
    """Public: True if unprivileged user + mount namespaces work on this host.

    Stable cross-module entry point for the namespace-support probe, shared by
    the OS-level sandbox here and the JailProvider extension point
    (``platform/interfaces.py``), so consumers do not depend on the private
    ``_probe_unshare`` name.
    """
    return _probe_unshare()


@functools.lru_cache(maxsize=1)
def is_wsl() -> bool:
    """Public: True if this Linux host is running under Windows Subsystem for Linux.

    Centralized host probe (parallel to :func:`userns_available`) so consumers
    never re-implement WSL detection. WSL2 *does* expose working user
    namespaces, so :func:`userns_available` returns True there — but WSL's
    networking is a NAT'd virtual interface, and rootless-namespace jails
    (slirp4netns) make agentic command networking unreachable. A jail backend
    (JailProvider) uses this to opt WSL out of jailing.

    Detection (cheap, in order): the ``WSL_DISTRO_NAME`` / ``WSL_INTEROP`` env
    vars WSL injects into every login shell, then the ``microsoft`` marker the
    WSL kernel stamps into ``/proc/version`` (covers WSL1 + WSL2, both Microsoft
    and -microsoft-standard builds). Result is cached — the host's WSL-ness does
    not change within a process. Always False off Linux.
    """
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


@functools.lru_cache(maxsize=1)
def is_docker_container() -> bool:
    """Public: True if this process is running inside a Docker/OCI container.

    Centralized host probe (parallel to :func:`is_wsl`) so consumers never
    re-implement container detection.  Used by :func:`wrap_argv` to produce an
    actionable error message when ``unshare(CLONE_NEWUSER)`` is blocked by the
    container runtime's seccomp/AppArmor policy instead of a kernel-level
    user-namespace restriction — the two cases warrant different remedies.

    Detection order (cheap, no I/O on fast paths):

    1. ``/.dockerenv`` — Docker daemon creates this in every container.
    2. ``/run/.containerenv`` — Podman's equivalent OCI marker.
    3. ``CONTAINER=oci`` env var — set by Podman rootless and some runtimes.
    4. ``/proc/1/cgroup`` — contains ``docker``, ``containerd``, or
       ``kubepods`` in container-managed cgroups; also fires in nested
       Docker-in-Docker setups.

    Result is cached — the container context does not change within a process.
    Always False off Linux.
    """
    if sys.platform != "linux":
        return False
    # Fast path: Docker always creates /.dockerenv; Podman creates /run/.containerenv.
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    # Podman rootless and some OCI runtimes export CONTAINER=oci.
    if os.environ.get("CONTAINER") == "oci":
        return True
    # Fallback: inspect the cgroup hierarchy for well-known runtime markers.
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
            content = fh.read().lower()
        return "docker" in content or "containerd" in content or "kubepods" in content
    except OSError:
        return False


def _probe_sandbox_exec() -> bool:
    """Return True if macOS ``sandbox-exec`` actually works.

    Uses a file-based profile with fixed system paths for both
    ``sandbox-exec`` and its ``/usr/bin/true`` target. The probe tests with an
    ``(allow default)`` profile to detect kernel-level rejection, not merely
    executable presence.
    """
    if sys.platform != "darwin":
        return False
    # Decide empirically — do NOT hard-code a macOS version cutoff. An earlier
    # `major >= 26 → return False` gate was wrong: sandbox-exec + the Seatbelt
    # kernel subsystem still work on macOS 26 (Tahoe) — verified that the real
    # generated profile compiles, runs kiro-cli, AND enforces (a strict profile
    # denies `cat ~/.aws/config`). The gate disabled a working sandbox and forced
    # the agent onto the fail-closed no-isolation path. The probe below already
    # detects a genuinely-broken sandbox-exec on any host/version, so trust it.
    # Note: sandbox-exec / sandbox_init() are marked "deprecated" in headers
    # since macOS 10.8, but the Seatbelt kernel subsystem they use is NOT
    # deprecated — it's the same enforcement layer that backs App Sandbox and
    # iOS. All major AI CLIs (Claude Code, Codex, Gemini) rely on it.
    # Rather than hard-coding version checks, we probe empirically below.
    sb = "/usr/bin/sandbox-exec"
    if not os.path.exists(sb):
        return False
    # Probe with a file-based (allow default) profile against a TRUSTED, fixed
    # system binary. We deliberately do NOT probe the (user-writable) kiro-cli
    # binary: the probe runs under (allow default) with KiroCrew's credentials,
    # so exec'ing a user-writable target here could run a planted payload
    # effectively unsandboxed. The probe only needs to confirm the kernel
    # accepts sandbox_apply, which /usr/bin/true validates safely.
    target = "/usr/bin/true"
    if not os.path.exists(target):
        return False
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="kirocrew_probe_")
    try:
        os.write(fd, b"(version 1)(allow default)")
        os.close(fd)
        r = subprocess.run(
            [sb, "-f", profile_path, target],
            capture_output=True,
            timeout=5,
        )
        if r.returncode != 0:
            detail = r.stderr.decode(errors="replace").strip()
            # A nested probe ALWAYS fails: Seatbelt cannot nest, so from inside a
            # sandbox `sandbox_apply` returns EPERM even under an (allow default)
            # profile. Reporting that at WARNING as a probe failure sent operators
            # hunting for a broken sandbox-exec on hosts where it works perfectly
            # unnested, so say what actually happened instead.
            if _macos_sandbox_state() is True:
                logger.info(
                    "sandbox-exec probe failed inside an existing Seatbelt "
                    "sandbox (exit %d: %s) — nesting is impossible; this host's "
                    "sandbox-exec is NOT broken",
                    r.returncode,
                    detail,
                )
            else:
                logger.warning(
                    "sandbox-exec probe failed (exit %d): %s",
                    r.returncode,
                    detail,
                )
        return r.returncode == 0
    except Exception as exc:
        logger.debug("sandbox-exec probe failed: %s", exc)
        return False
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass


# ── Backend: Linux namespace sandbox ──


def _resolve_agent_executable(executable: str) -> str:
    """Resolve *executable* through the active edition before sandboxing.

    The public adapter is identity. An edition companion may replace a managed
    launcher with the direct executable it ultimately invokes so KiroCrew can
    apply exactly one OS-level sandbox. A transient adapter failure degrades to
    the original executable, which preserves the secure behavior: the outer
    sandbox still applies and a launcher that cannot run nested fails closed.
    Platform composition failures always propagate through ``safe_context_call``.
    """
    from kiro_crew.platform import safe_context_call

    return safe_context_call(
        lambda: current_context().agent_executable.resolve_executable(executable),
        fallback=executable,
        log_message="Agent executable resolver failed; using the original executable",
    )


@functools.lru_cache(maxsize=None)
def _ssh_supports_accept_new() -> bool:
    """Return True if the installed ssh supports StrictHostKeyChecking=accept-new (OpenSSH >= 7.6)."""
    try:
        r = subprocess.run(["ssh", "-V"], capture_output=True, timeout=5)
        m = re.search(r"OpenSSH_(\d+)\.(\d+)", r.stderr.decode())
        if m:
            return (int(m.group(1)), int(m.group(2))) >= (7, 6)
    except Exception:
        pass
    return False


def _build_launcher_script(
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> str:
    """Build a Python launcher script for the Linux namespace sandbox.

    The launcher is executed as a subprocess.  It:

    1. Forks a child.
    2. Child calls ``unshare(CLONE_NEWUSER)`` and signals the parent.
    3. Parent writes identity UID/GID map (``uid uid 1``) to
       ``/proc/<child>/{setgroups,uid_map,gid_map}`` and signals back.
    4. Child calls ``unshare(CLONE_NEWNS)``, sets mount propagation private,
       bind-mounts empty dirs over credential paths, scrubs env vars,
       and ``exec``s the real command.

    The child retains the real UID/GID — no UID 0, no UID 65534.
    """
    home = str(Path.home())
    uid = os.getuid()
    gid = os.getgid()
    # Source the sensitive-dir lists from the active PlatformContext so the
    # The internal companion can extend them (+ .midway/.ada).  The Default adapter
    # returns ``list(_STRICT_DIRS)`` / ``list(_CC_DIRS)``, so standalone is
    # unchanged.  ``_STANDARD_DIRS`` is not an extension point (no interface
    # method) and stays on the module global.
    if sandbox_level == "standard":
        dirs = _STANDARD_DIRS
    elif sandbox_level == "cc":
        dirs = _sandbox_policy().cc_dirs()
    else:
        dirs = _sandbox_policy().strict_dirs()
    files = _CC_FILES if sandbox_level in ("cc", "strict") else []
    expose_files = _CC_EXPOSE_FILES if sandbox_level == "cc" else []
    env_prefixes = list(_SENSITIVE_ENV_PREFIXES)
    if sandbox_level in ("cc", "strict"):
        # Block agent subprocesses from reading credentials via os.environ
        # (the file-level bind-mount of ~/.kiro/crew/.env hides them on disk;
        # config/loader.py seeds them into os.environ for trusted children
        # only — sandboxed agents must not see them either way).
        env_prefixes = env_prefixes + list(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        # Foreign Python subprocess (kiro-cli's MCP servers) — do not let
        # KiroCrew's PYTHONPATH/PYTHONHOME leak in and shadow their own deps.
        env_prefixes = env_prefixes + list(_PYTHON_ENV_PREFIXES)
    hide_ssh = sandbox_level == "strict"
    hidden_dirs = [os.path.join(home, d) for d in dirs]
    hidden_dirs.extend(_relocated_policy_cache_dirs())
    hidden_dirs.extend(_relocated_crew_targets(_CREW_HIDDEN_LEAVES))
    hidden_dirs.extend(_voice_runtime_sandbox_paths())
    hidden_dirs.extend(os.path.abspath(path) for path in extra_hidden_dirs)
    unhidden = [
        path for path in hidden_dirs if _hidden_path_contains_visible_path(path, extra_visible_dirs)
    ]
    hidden_dirs = [path for path in hidden_dirs if path not in unhidden]
    # The governance cache is READ-ONLY whenever it is exposed at all, and that is a
    # property of the directory rather than of the caller's request: `extra_visible_dirs`
    # otherwise cancels a target's whole rule set, so the one caller that legitimately
    # needs to READ the ceiling (`apps/backend.py`, which boots in cache-only mode and
    # resolves the fleet ceiling from this file) would get WRITE with it. That is the
    # dangerous direction — the metadata records the source the next boot trusts, so a
    # same-UID process that can rewrite the pair picks the ceiling for every later boot,
    # and an app backend is arbitrary third-party code. Deciding it here means a future
    # caller cannot re-open the hole by passing this path.
    readonly_dirs = [path for path in unhidden if _is_policy_cache_dir(path)]
    # ``run`` must stay readable because it holds this launcher, but making both
    # its lexical and canonical spellings read-only prevents an agent from
    # renaming the hidden voice-runtime mount out from under the path-based rule.
    readonly_dirs.extend(_voice_runtime_parent_paths())
    # The crew data home's ceilings. Read-only rather than hidden because in-sandbox
    # code resolves them (a script cron's ``boot_platform()``, the config loader) and an
    # absent ceiling reads as the permissive standalone default — masking one would
    # REMOVE it. A caller's ``extra_visible_dirs`` cannot re-open the write side, for the
    # reason spelled out for the governance cache above.
    readonly_dirs.extend(
        os.path.join(home, target)
        for target in _CREW_READONLY_TARGETS
        if os.path.join(home, target) not in hidden_dirs
    )
    # A relocated data home escapes every ``$HOME``-relative rule above, which would
    # leave the ceiling writable on exactly the managed fleets that set it.
    readonly_dirs.extend(
        path for path in _relocated_crew_targets(_CREW_READONLY_LEAVES) if path not in hidden_dirs
    )
    # A caller-supplied hidden path may be a FILE, and the two launcher loops hide
    # each kind differently: a directory gets an empty dir bind-mounted over it, a file
    # gets an empty temp file. The dir loop is guarded by `if os.path.isdir(target)`, so
    # a file entry matched neither it nor the file loop and was SILENTLY SKIPPED — the
    # caller asked for it to be hidden, got no error, and it stayed readable.
    #
    # That is not hypothetical: `security.sensitive_home_dirs()` is not all directories
    # (`sel_hmac.key`, `token_signing.key`, `.kiro/crew/.env` are files), and Papyrus
    # passes that whole list as `extra_hidden_dirs` so a `.tex` cannot `\input` the
    # gateway's own secrets into a rendered PDF.
    #
    # Every path goes in BOTH lists, and the CHILD classifies it. The child already
    # re-checks with its own `isdir`/`isfile` per loop, so whichever branch matches does
    # the work and the other skips — no double-mount, no wrong-kind mount. Classifying
    # here instead would mean an `os.path.isfile()` per entry (52 of them) inside
    # `_build_launcher_script`, which runs on the event loop for every async spawn: on a
    # stalled NFS home those stats block the gateway and the liveness heartbeat. Letting
    # the child decide keeps the syscalls in the child, where they are already happening
    # and where blocking costs nothing but that one spawn.
    #
    # macOS is unaffected either way: for these entries the profile emits BOTH a
    # `(subpath …)` and a `(literal …)` deny, so a plain-file leaf is covered
    # without relying on how subpath treats a non-directory.
    dirs_json = json.dumps(list(dict.fromkeys(hidden_dirs)))
    readonly_json = json.dumps(list(dict.fromkeys(readonly_dirs)))
    files_json = json.dumps(
        list(dict.fromkeys([os.path.join(home, f) for f in files] + hidden_dirs))
    )
    expose_json = json.dumps([(os.path.join(home, f), f.split("/")[-1]) for f in expose_files])
    env_prefixes_json = json.dumps(env_prefixes)
    ssh_dir = json.dumps(os.path.join(home, ".ssh"))
    ssh_known_hosts = json.dumps(os.path.join(home, ".ssh", "known_hosts"))
    sandbox_level_json = json.dumps(sandbox_level)
    strict_host_key_opt = (
        " -o StrictHostKeyChecking=accept-new" if _ssh_supports_accept_new() else ""
    )

    return f'''#!/usr/bin/env python3
"""Namespace sandbox launcher — spawned by KiroCrew."""
import sys
# Harden against stdlib shadowing. This launcher runs as
# ``python <config_dir>/run/kirocrew_sandbox_*.py``, so CPython prepends the
# script's own directory (sys.path[0], typically <config_dir>/run/) to sys.path.
# A stray sibling module left in that directory by another process — e.g.
# struct.py, os.py — then shadows the real stdlib and crashes the imports below
# (seen in the wild: "ImportError: cannot import name 'calcsize' from
# '/tmp/struct.py'", which kills the agent subprocess on spawn). ``sys`` is a
# builtin and cannot be shadowed, so importing it first is safe; drop the
# launcher dir (and any cwd "" entry) before importing anything that resolves
# from the filesystem.
sys.path[:] = [p for p in sys.path if p not in ("", sys.path[0])]
import ctypes
import os
import stat
import tempfile

# Hoisted from Steps 5/6 (used only AFTER unshare()+mount isolation): a
# FIRST-TIME stdlib import reads module files off disk, and once the child has
# entered its user+mount namespaces that read can be denied by the host's LSM
# (seen in the wild: Ubuntu 24.04 with apparmor_restrict_unprivileged_userns=1
# denies the post-unshare read, so ``import platform`` at seccomp-install time
# died with ModuleNotFoundError and every sandboxed spawn failed -- #8151).
# Import EVERYTHING this launcher needs while it is still pre-isolation, so no
# post-isolation code ever touches the filesystem for stdlib. The static-scan
# test pins this: every import in this generated script must be module-level.
import platform as _plat
import struct as _struct

_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS   = 0x00020000
_MS_RDONLY     = 1
_MS_NOSUID     = 2
_MS_NODEV      = 4
_MS_NOEXEC     = 8
_MS_REMOUNT    = 32
_MS_BIND       = 4096
_MS_REC        = 16384
_MS_PRIVATE    = 1 << 18

# dlopen(NULL): resolve mount()/unshare()/prctl() from the libc ALREADY loaded
# into this interpreter. Never ctypes.util.find_library here -- on Linux it
# EXECUTES helper processes to locate libc (ldconfig first, then a PATH-resolved
# gcc/cc/objdump/ld once ldconfig yields no match, i.e. on musl hosts). This
# module scope runs BEFORE the fork and before either unshare() below, under an
# environment the SPAWNING CALLER supplies -- so on such a host a
# caller-controlled `gcc` on PATH would be same-user code execution ahead of the
# confinement this launcher exists to establish.
#
# Same rule, same reason, as the spawned userns probe in ``_PROBE_SHIM_CODE``,
# which already resolves libc this way; the launcher was the one pre-confinement
# script still violating it. Not a new code path either: ``find_library``
# returning None made this call ``CDLL(None)`` anyway, so dlopen(NULL) was
# already the implicit fallback here. ``ctypes.util`` is deliberately left
# unimported above so a future reintroduction fails loudly instead of silently
# reopening the PATH lookup.
_libc = ctypes.CDLL(None, use_errno=True)
_libc.mount.argtypes = [
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
    ctypes.c_ulong, ctypes.c_void_p,
]
_libc.mount.restype = ctypes.c_int
_libc.unshare.argtypes = [ctypes.c_int]
_libc.unshare.restype = ctypes.c_int
_libc.prctl = _libc.prctl if hasattr(_libc, "prctl") else None
if _libc.prctl:
    _libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    _libc.prctl.restype = ctypes.c_int

def _mount_or_die(source, target, flags, what):
    """``mount(2)`` or refuse to exec, naming *what* and the errno.

    Every mount in this launcher IS a security control -- each one hides a
    credential path, or (for ``/``) pins mount propagation so the hiding
    cannot escape. Discarding the return value makes those controls fail
    OPEN: the path stays visible and the agent runs anyway, believing it is
    hidden. Nothing downstream notices -- there is no post-mount emptiness
    check, the launcher has no logger, and the pre-exec hardlink scan only
    fires when a credential happens to carry an extra link.

    So these refuse, matching what the rest of this launcher already does
    when a control cannot be established: both ``unshare`` calls, the
    seccomp-BPF install, and the hardlink scan all ``sys.exit``. What marks
    those off from the decisions that DO degrade open is a rule, not a list:
    a failed hiding mount is the one thing this helper exists to prevent,
    nothing else here is one, and each of those others argues its case at
    its own site -- the ``EXPOSE_FILES`` pre-read is one of them, named as
    an example and not as a roster -- no count is kept here, since the count
    is what goes stale. Read it narrowly: none is a failed hiding mount, NOT
    the stronger claim that no credential can end up reachable. A degrade
    elsewhere is never license to degrade a mount.

    ``sandbox_level`` is the explicit opt-out for a host that cannot mount;
    a silent unhidden credential is not.
    """
    if _libc.mount(source, target, None, flags, None) != 0:
        _err = ctypes.get_errno()
        sys.exit(
            "sandbox: BLOCKED -- %s failed: errno %d (%s). The sandbox could not "
            "establish this control, so the agent would run with the path "
            "visible. Lower sandbox_level to run without it deliberately."
            % (what, _err, os.strerror(_err))
        )

def _locked_mount_flags(target):
    """Mount flags on *target* the kernel may have LOCKED, ready to re-assert.

    Inside an unprivileged user namespace the kernel treats a mount's
    nosuid / nodev / noexec bits as locked and rejects with EPERM any remount
    whose flag set would clear them. A bind created over *target* inherits
    those bits -- locks included -- from its source mount (/tmp carries
    nosuid,nodev by default on AL2023 / Fedora / RHEL), so the sealing
    remount must carry them again. Called AFTER the bind step, so ``f_flag``
    reflects the new bind's effective flags. Re-asserting a bit already in
    force can only keep restrictions, never widen access. atime is left
    alone: a remount that passes no atime flag preserves the existing mode,
    which already satisfies MNT_LOCK_ATIME.

    On ``statvfs`` failure fall back to 0 extra flags: the remount then
    behaves exactly as it did before this helper existed, and a locked-flag
    rejection still fails closed at the call site. Never degrades the seal.
    """
    try:
        f_flag = os.statvfs(target).f_flag
    except OSError:
        return 0
    flags = 0
    # getattr, never bare os.ST_*: the launcher only ever RUNS on Linux, where
    # all three exist, but the sandbox tests execute this helper's extracted
    # source on every POSIX host and macOS defines only ST_RDONLY / ST_NOSUID
    # -- a bare os.ST_NODEV there raises AttributeError, which the OSError
    # fallback above deliberately does not swallow.
    if f_flag & getattr(os, "ST_NOSUID", 0):
        flags |= _MS_NOSUID
    if f_flag & getattr(os, "ST_NODEV", 0):
        flags |= _MS_NODEV
    if f_flag & getattr(os, "ST_NOEXEC", 0):
        flags |= _MS_NOEXEC
    return flags

REAL_UID = {uid}
REAL_GID = {gid}
SENSITIVE_DIRS = {dirs_json}
READONLY_DIRS = {readonly_json}
SENSITIVE_FILES = {files_json}
EXPOSE_FILES = {expose_json}
ENV_PREFIXES = {env_prefixes_json}
SSH_DIR = {ssh_dir}
SSH_KNOWN_HOSTS = {ssh_known_hosts}
HIDE_SSH = {hide_ssh}
SANDBOX_LEVEL = {sandbox_level_json}

def main():
    argv = sys.argv[1:]
    if not argv:
        sys.exit("sandbox_launcher: no command given")

    # Export this launcher's HOST pid before any fork/namespace work. The
    # gateway records exactly this pid (its direct Popen child) when it
    # writes ``session_pid_<pid>.txt`` on session claim, so in-sandbox
    # identity resolvers can look the file up directly via this env var
    # instead of walking /proc — which breaks whenever the subtree's view
    # of pids diverges from the host's (PID-namespace sandboxing).
    os.environ["KIROCREW_HOST_PID"] = str(os.getpid())

    # Two pipes for parent↔child synchronization
    c2p_r, c2p_w = os.pipe()  # child signals "unshare done"
    p2c_r, p2c_w = os.pipe()  # parent signals "maps written"

    pid = os.fork()

    if pid > 0:
        # ── Parent: write identity UID/GID map ──
        os.close(c2p_w)
        os.close(p2c_r)
        os.read(c2p_r, 1)  # wait for child to unshare(NEWUSER)
        os.close(c2p_r)
        with open(f"/proc/{{pid}}/setgroups", "w") as f:
            f.write("deny")
        with open(f"/proc/{{pid}}/uid_map", "w") as f:
            f.write(f"{{REAL_UID}} {{REAL_UID}} 1\\n")
        with open(f"/proc/{{pid}}/gid_map", "w") as f:
            f.write(f"{{REAL_GID}} {{REAL_GID}} 1\\n")
        os.write(p2c_w, b"x")  # signal child to proceed
        os.close(p2c_w)
        _, status = os.waitpid(pid, 0)
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        sys.exit(code)
    else:
        # ── Child: unshare, wait for maps, mount, exec ──
        os.close(c2p_r)
        os.close(p2c_w)

        # Step 1: enter user namespace
        if _libc.unshare(_CLONE_NEWUSER) != 0:
            sys.exit(f"sandbox: unshare(NEWUSER) failed: errno {{ctypes.get_errno()}}")
        os.write(c2p_w, b"x")  # tell parent
        os.close(c2p_w)
        os.read(p2c_r, 1)  # wait for maps
        os.close(p2c_r)

        # Step 2: enter mount namespace (now we have a mapped UID)
        if _libc.unshare(_CLONE_NEWNS) != 0:
            sys.exit(f"sandbox: unshare(NEWNS) failed: errno {{ctypes.get_errno()}}")

        # Private mount propagation
        _mount_or_die(None, b"/", _MS_REC | _MS_PRIVATE,
                      "making mount propagation private on /")

        # Pick a tmpfs-backed source dir for bind-mount empty files/dirs. Same-fs
        # binds (e.g. /tmp on ext4 over ~/.kiro/crew/.env on ext4) can corrupt the
        # target's host directory entry via a kernel propagation race when the
        # private NS is torn down — leaving the host file pointing at the empty
        # source inode permanently. Cross-fs binds use distinct inode spaces and
        # cannot leak that way. Fallback chain: /run/user/$UID → /dev/shm.
        # Verify each candidate is on a different filesystem from HOME by
        # comparing st_dev — same-fs candidates provide no isolation benefit.
        _tmpfs_src = None
        try:
            _home_dev = os.stat(os.path.expanduser("~")).st_dev
        except OSError:
            _home_dev = None
        for _candidate in (f"/run/user/{{REAL_UID}}", "/dev/shm"):
            try:
                if _home_dev is not None and os.stat(_candidate).st_dev == _home_dev:
                    continue  # same fs as HOME — no isolation, race still possible
                _probe = tempfile.mkdtemp(dir=_candidate, prefix="kirocrew_sbprobe_")
                try:
                    os.rmdir(_probe)
                except FileNotFoundError:
                    pass  # an external cleaner won the race — the root still works
                _tmpfs_src = _candidate
                break
            except (OSError, ValueError):
                continue
        # _tmpfs_src=None falls through to system default tempdir (typically /tmp).
        # In that case we accept the kernel-race risk because no tmpfs is
        # available — better to function (with the original regression risk)
        # than to refuse to start.

        # Tag every bind-mount SOURCE with this process's pid. The kernel pins
        # a bind source for the mount's lifetime, so these entries cannot be
        # unlinked here and are orphaned when the sandboxed process exits; the
        # pid in the name is the liveness key the periodic janitor
        # (_cleanup_stale_sandbox_mount_sources) probes to reclaim them. exec
        # preserves the pid, so this pid IS the running agent's pid. The
        # tmpfs probe above deliberately uses the sibling "kirocrew_sbprobe_"
        # prefix, OUTSIDE the pid-parsed family, so the janitor never races
        # its mkdtemp/rmdir window.
        _src_prefix = "kirocrew_sb_%d_" % os.getpid()

        # Pre-read files that must survive dir hiding.
        #
        # An expose source that cannot be READ degrades to "not exposed" with a
        # stderr warning, the same way the Step 7 hardlink scan degrades open.
        # This read runs during sandbox SETUP, so letting the OSError propagate
        # aborts the child before the command runs at all -- and selective
        # exposure is an OPTIMIZATION (keep ~/.aws/config reachable so
        # credential_process still resolves inside an otherwise-hidden ~/.aws),
        # never a security control. Failing the whole spawn because an optional
        # convenience is unreadable trades a working sandbox for no sandbox.
        #
        # `isfile` already covers ABSENT; this covers UNREADABLE, and the two
        # are not the same test: `stat` can succeed on a path whose `open` is
        # then denied. Seen in the wild as a filesystem restriction inherited
        # from the parent process, denying read on a 0600 file the child's own
        # uid owned -- so DAC bits and uid both looked correct while every
        # cc-mode spawn on that host died here.
        #
        # Catching the error is the only guard that HOLDS. Do not "tighten" this
        # into a pre-flight `os.access(src_path, os.R_OK)`: measured on the
        # affected host, `os.stat()` succeeded and `os.access()` reported BOTH
        # X_OK and R_OK as True while the operation was denied anyway. The
        # weaker check looks equivalent from the source alone and would
        # silently restore the abort.
        #
        # The warning is not optional. Skipping silently would leave the child
        # with no ~/.aws/config and no explanation, turning a loud setup failure
        # into a later auth failure that points nowhere near this line.
        expose_data = {{}}
        for src_path, filename in EXPOSE_FILES:
            if os.path.isfile(src_path):
                try:
                    with open(src_path, "rb") as fh:
                        expose_data[src_path] = fh.read()
                except OSError as exc:
                    print(
                        "sandbox: WARNING — cannot read %s (%s); it will be "
                        "ABSENT inside the sandbox. Anything depending on it "
                        "(e.g. credential_process in ~/.aws/config) will fail."
                        % (src_path, exc),
                        file=sys.stderr,
                    )

        # Bind-mount empty dirs over credential paths (per-dir tmpdir to
        # prevent content leaking across mounts via shared backing dir).
        for d in SENSITIVE_DIRS:
            target = d.encode()
            if os.path.isdir(target):
                per_dir_empty = tempfile.mkdtemp(dir=_tmpfs_src, prefix=_src_prefix).encode()
                _mount_or_die(per_dir_empty, target, _MS_BIND,
                              "hiding credential directory %s" % d)

        # Exposed-but-read-only dirs (the governance cache): bind the real dir over
        # itself, then remount that bind MS_RDONLY. Both steps are load-bearing --
        # MS_RDONLY is ignored on the initial MS_BIND, so without the remount this
        # loop would grant exactly the write access it exists to withhold. Creating
        # the bind ourselves is necessary but NOT sufficient inside a user
        # namespace: the kernel locks the source mount's nosuid/nodev/noexec bits,
        # and rejects a remount that would drop them, so the seal re-asserts them
        # via _locked_mount_flags -- re-asserting bits already in force can only
        # keep restrictions, never widen access.
        for d in READONLY_DIRS:
            target = d.encode()
            # ``exists``, not ``isdir``: a governance ceiling is a plain file
            # (``security_policy.json``), and bind-over-self + MS_RDONLY seals a
            # regular file exactly as it seals a directory. Guarding on ``isdir``
            # would silently skip every ceiling FILE — the caller asks for it to be
            # sealed, gets no error, and it stays writable.
            if os.path.exists(target):
                _mount_or_die(target, target, _MS_BIND,
                              "exposing read-only path %s" % d)
                _mount_or_die(target, target,
                              _MS_REMOUNT | _MS_BIND | _MS_RDONLY
                              | _locked_mount_flags(target),
                              "sealing read-only path %s" % d)

        # Restore selectively exposed files into the now-empty mounts
        for src_path, filename in EXPOSE_FILES:
            if src_path in expose_data:
                parent = os.path.dirname(src_path)
                dest = os.path.join(parent, filename)
                with open(dest, "wb") as fh:
                    fh.write(expose_data[src_path])
                # NOTE: this runs inside the embedded Linux-only namespace
                # launcher script (a standalone /tmp file that imports only
                # stdlib — sys/ctypes/os/stat/tempfile/platform/struct — and
                # never kiro_crew), so it
                # must stay a raw os.chmod, NOT platform_compat.chmod_safe
                # (which is undefined in that process). The launcher never runs
                # on Windows, so there is no portability loss.
                os.chmod(dest, 0o444)

        # Bind-mount empty files over individual sensitive files. Source the
        # empty tempfile from a tmpfs (cross-fs) when available so the bind
        # cannot corrupt the target's host directory entry on namespace exit.
        for f in SENSITIVE_FILES:
            target = f.encode()
            if os.path.isfile(target):
                fd, empty_path = tempfile.mkstemp(dir=_tmpfs_src, prefix=_src_prefix)
                os.close(fd)
                _mount_or_die(empty_path.encode(), target, _MS_BIND,
                              "hiding sensitive file %s" % f)

        # .ssh: hide keys but expose known_hosts content (strict only)
        if HIDE_SSH and os.path.isdir(SSH_DIR):
            kh_data = b""
            if os.path.isfile(SSH_KNOWN_HOSTS):
                # Host trust data FAILS CLOSED. This is deliberately NOT the
                # degrade-open treatment the EXPOSE_FILES pre-read above gets,
                # and the two sites are NOT symmetric:
                #
                #   - an unreadable ~/.aws/config costs REACHABILITY, so
                #     skipping it trades a convenience for a working sandbox;
                #   - an unreadable known_hosts costs VERIFICATION. The launcher
                #     puts StrictHostKeyChecking=accept-new into
                #     GIT_SSH_COMMAND, gated ONLY on that variable being unset
                #     -- never on whether this read succeeded. So continuing
                #     with an empty kh_data points UserKnownHostsFile at an
                #     absent file while auto-accept is still on: every host then
                #     reads as NEW and an interceptor's key is accepted. With
                #     known_hosts present, accept-new REFUSES a CHANGED key.
                #
                # A degrade here would therefore convert "refuse a changed key"
                # into "accept anything". Aborting is the safe direction: no
                # sandbox at all beats one that has quietly stopped verifying
                # hosts. Report first so the abort is diagnosable, then re-raise
                # and let it kill setup.
                try:
                    with open(SSH_KNOWN_HOSTS, "rb") as fh:
                        kh_data = fh.read()
                except OSError as exc:
                    print(
                        "sandbox: FATAL — cannot read %s (%s). Refusing to "
                        "continue: proceeding without it would leave host-key "
                        "verification accepting any new key."
                        % (SSH_KNOWN_HOSTS, exc),
                        file=sys.stderr,
                    )
                    raise
            # Cross-fs source for the same kernel-race reason as SENSITIVE_DIRS
            # (line 371) and SENSITIVE_FILES (line 389).
            ssh_tmp = tempfile.mkdtemp(dir=_tmpfs_src, prefix=_src_prefix).encode()
            _mount_or_die(ssh_tmp, SSH_DIR.encode(), _MS_BIND,
                          "hiding ssh key directory %s" % SSH_DIR)
            if kh_data:
                with open(os.path.join(SSH_DIR, "known_hosts"), "wb") as fh:
                    fh.write(kh_data)

        # Scrub sensitive env vars
        for key in list(os.environ):
            for prefix in ENV_PREFIXES:
                if key.startswith(prefix):
                    del os.environ[key]
                    break

        # Mark the sandboxed tree so in-sandbox wrap_argv calls know OS
        # isolation is already active (nested unshare is seccomp-denied).
        # Set AFTER the scrub loop so a scrubbed prefix cannot delete it.
        os.environ["KIROCREW_SANDBOX_ACTIVE"] = "1"
        # Record WHICH tier this sandbox was built at, so an in-sandbox
        # wrap_argv passthrough can detect a requested-vs-active tier
        # downgrade. Same non-scrubbable placement as the marker above.
        os.environ["KIROCREW_SANDBOX_LEVEL"] = SANDBOX_LEVEL

        # Fix /etc/ssh/ssh_config.d/ ownership issue: root-owned files
        # appear as nobody:nobody inside the user namespace because UID 0
        # is unmapped. SSH refuses to load them. Bypass with -F /dev/null.
        if not os.environ.get("GIT_SSH_COMMAND"):
            os.environ["GIT_SSH_COMMAND"] = (
                "ssh -F /dev/null -o IdentityFile=~/.ssh/id_rsa"
                " -o IdentityFile=~/.ssh/id_ecdsa"
                " -o IdentityFile=~/.ssh/id_ed25519"
                " -o UserKnownHostsFile=~/.ssh/known_hosts"
                "{strict_host_key_opt}"
            )

        # Gradle would otherwise leave a daemon running after this sandboxed
        # command exits, holding our mount namespace open with the credential
        # paths still masked, plus the inherited seccomp filter and emptied
        # capability bounding set. Nothing here changes what Gradle keys its
        # daemon context on, so a later build OUTSIDE the sandbox matches and
        # adopts that daemon, silently running under restrictions and a
        # credential view it never asked for. Keyed on the EFFECTIVE LAST
        # -Dorg.gradle.daemon= directive rather than on mere presence, because
        # duplicate -D resolves last-wins: a trailing =true would otherwise
        # survive, while appending when ours is already last just duplicates.
        if [
            _t
            for _t in os.environ.get("GRADLE_OPTS", "").split()
            if _t.startswith("-Dorg.gradle.daemon=")
        ][-1:] != ["-Dorg.gradle.daemon=false"]:
            os.environ["GRADLE_OPTS"] = (
                os.environ.get("GRADLE_OPTS", "") + " -Dorg.gradle.daemon=false"
            ).strip()

        # ── Step 5: Drop capabilities + set NO_NEW_PRIVS ──
        # Inside the user namespace, the child has CAP_SYS_ADMIN (owner of the
        # NS) which lets it umount the credential bind-mounts. Drop ALL
        # capabilities from the bounding set and set NO_NEW_PRIVS before exec.
        # (_struct is imported in the preamble, pre-isolation -- see the hoist
        # note there; importing it HERE crashed under AppArmor userns
        # restriction, #8151.)

        _PR_SET_NO_NEW_PRIVS = 38
        _PR_CAPBSET_DROP = 24
        if not _libc.prctl:
            # prctl(2) is how BOTH remaining controls are applied: the
            # capability-bounding drop plus NO_NEW_PRIVS here, and the
            # seccomp-BPF install in Step 6. Without it the child keeps
            # CAP_SYS_ADMIN over this mount namespace and can umount the
            # credential masks, so refuse for the same reason the unknown-arch
            # branch below does.
            sys.exit(
                "sandbox: BLOCKED — libc exposes no prctl(2), so neither the "
                "capability-bounding drop nor the seccomp-BPF namespace-escape "
                "filter can be applied. The agent would keep CAP_SYS_ADMIN in "
                "this mount namespace and could unmount the credential masks, "
                "so this spawn is refused. To run anyway WITHOUT OS-level "
                "isolation, set agent.sandbox='off' or "
                "agent.sandbox_allow_unsandboxed_exec=true in "
                "~/.kiro/crew/config.json."
            )
        if _libc.prctl:
            # Linux CAP_LAST_CAP is currently 41 (kernel 6.x); iterate 0..63 for
            # forward-compatibility — dropping a non-existent cap just returns -1.
            for _cap in range(64):
                _libc.prctl(_PR_CAPBSET_DROP, _cap, 0, 0, 0)
            # NO_NEW_PRIVS: prevents regaining caps via exec of setuid/setcap bins.
            # Load-bearing beyond hardening: the mount-source sweep's directory
            # gate (_cleanup_stale_sandbox_mount_sources) reclaims on the claim
            # that every launcher descendant still stats as this uid (or the
            # overflow uid from a nested userns), so a change here that lets a
            # descendant change uid would turn that gate fail-open.
            _ret = _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            if _ret != 0:
                sys.exit("sandbox: BLOCKED — failed to set NO_NEW_PRIVS (prctl returned %d)" % _ret)

        # ── Step 6: Install seccomp-BPF filter ──
        # Deny mount/umount2/unshare/setns/pivot_root to prevent the sandboxed
        # process from undoing the credential bind-mounts (namespace escape).
        #
        # NOTE: link/linkat were previously denied here to block hardlinking a
        # credential inode out past its bind-mount (pentest finding #9). That
        # deny has been removed; the vector is covered without a blanket syscall
        # ban (which broke npm cacache / pnpm / ln for no gain). Masking is
        # per-level: strict bind-masks its dir/file list PLUS ~/.ssh; cc masks
        # the same MINUS ~/.ssh; standard masks only _STANDARD_DIRS (.gnupg,
        # .gpg, .config/gcloud, .azure, .docker, crew-auth-staging). For a file
        # that IS masked the credential inode has no reachable path, so no link
        # source exists. For a file left UNMASKED at a given level (~/.ssh under
        # cc; .aws/.ssh/_CC_FILES under standard) there is no privilege delta:
        # it is already directly readable, so the command gate
        # (security.is_sensitive_bash_command) is the control for BOTH reading
        # and hardlinking it — the gate now resolves an agent-issued ln/link/cp
        # source through is_sensitive_path(), refusing a link to a credential
        # source at the same fidelity as a read (closing the "flatten onto a
        # benign alias" bypass, GPT review PR #1339). npm's own fs.link() is a
        # syscall and never transits that gate. seccomp cannot path-scope link
        # (BPF cannot dereference the pathname pointer), so a syscall-layer form
        # could only be all-or-nothing. NOTE: the Step-7 pre-exec nlink scan is
        # NOT relied on here — it stats paths AFTER the masks, so it sees mask
        # inodes, not real credential inodes. For AppSec (pre-existing / out of
        # scope): that Step-7 gap; a hardlink alias is durable and symlink-
        # resolution-invisible; and `mv` is not yet gate-covered. AppSec
        # re-review required — this edits a pentest remediation.
        #
        # Additionally deny kill(-1, sig) — the signal BROADCAST that reaches
        # every same-uid process on the host (gateway, other sessions). This
        # is the accident-containment redo of the reverted PID-namespace
        # isolation (24c320f6): a static arg filter blocks the hand-slip /
        # runaway-script broadcast without changing the subtree's view of
        # pids, so session identity, claim-push, and systemd stay intact.
        # Only ``kill`` needs arg inspection: tkill/tgkill/pidfd_send_signal
        # are inherently targeted (no broadcast semantics). pid==0 and
        # negative process-group targets stay ALLOWED on purpose — the spawn
        # already setsid()s, so every reachable process group is inside the
        # sandbox session, and denying killpg breaks legitimate tooling
        # (timeout(1), shell job control, cleanup traps).
        if _libc.prctl:
            _PR_SET_SECCOMP = 22
            _SECCOMP_MODE_FILTER = 2
            _SECCOMP_RET_ALLOW = 0x7FFF0000
            _SECCOMP_RET_ERRNO = 0x00050000
            _EPERM = 1
            _BPF_LD = 0x00
            _BPF_W = 0x00
            _BPF_ABS = 0x20
            _BPF_JMP = 0x05
            _BPF_JEQ = 0x10
            _BPF_K = 0x00
            _BPF_RET = 0x06
            # Syscall numbers (x86_64): mount=165, umount2=166, unshare=272,
            # setns=308, pivot_root=155, kill=62
            # aarch64: mount=40, umount2=39, unshare=97, setns=268,
            # pivot_root=41, kill=129
            # (_plat is imported in the preamble, pre-isolation -- importing it
            # HERE was the reported #8151 crash: the post-unshare first-time
            # stdlib read was denied and the whole launcher died.)
            _machine = _plat.machine()
            if _machine == "x86_64":
                _DENY_SYSCALLS = (165, 166, 272, 308, 155)
                _KILL_NR = 62
            elif _machine == "aarch64":
                _DENY_SYSCALLS = (40, 39, 97, 268, 41)
                _KILL_NR = 129
            else:
                # No syscall table for this arch, so the filter that keeps the
                # child from undoing the credential masks cannot be built.
                # Refuse rather than skip: with unshare(2) still permitted the
                # child can enter a nested user namespace, hold CAP_SYS_ADMIN
                # over a copy of this mount tree, and umount every mask — the
                # exact escape Step 6 exists to deny. _inside_kirocrew_sandbox()
                # and docs/system-specs/modules/security.md both state that a
                # sandboxed tree is confined "by the outer namespace + seccomp",
                # so a silent skip makes that claim false while every caller
                # still reads the spawn as isolated. sandbox_level="off" (or
                # agent.sandbox_allow_unsandboxed_exec) is the explicit opt-out
                # for a host that cannot be confined; a silent one is not.
                sys.exit(
                    "sandbox: BLOCKED — no seccomp syscall table for machine "
                    "%r, so the namespace-escape filter (mount/umount2/unshare/"
                    "setns/pivot_root) cannot be installed. The agent would run "
                    "able to unshare a new namespace and unmount the credential "
                    "masks, so this spawn is refused. Supported: x86_64, "
                    "aarch64. To run anyway WITHOUT OS-level isolation, set "
                    "agent.sandbox='off' or "
                    "agent.sandbox_allow_unsandboxed_exec=true in "
                    "~/.kiro/crew/config.json." % _machine
                )

            if _DENY_SYSCALLS:
                # Architecture constants for seccomp arch validation
                _AUDIT_ARCH_X86_64 = 0xC000003E
                _AUDIT_ARCH_AARCH64 = 0xC00000B7
                _SECCOMP_RET_KILL = 0x00000000
                _expected_arch = _AUDIT_ARCH_X86_64 if _machine == "x86_64" else _AUDIT_ARCH_AARCH64

                # BPF program layout (indices relative to start):
                #   0: LD arch
                #   1: JEQ expected_arch ? skip 1 : fall through
                #   2: RET KILL                (unexpected arch)
                #   3: LD syscall nr
                #   4..4+n-1: JEQ deny_i -> DENY
                #   k   = 4+n: JEQ kill_nr ? fall into arg check : jump ALLOW
                #   k+1: LD args[0] low 32 bits    (seccomp_data offset 16)
                #   k+2: JEQ 0xFFFFFFFF ? jump DENY : fall through
                #   ALLOW = k+3: RET ALLOW
                #   DENY  = k+4: RET ERRNO|EPERM
                #
                # Only the LOW 32 bits of args[0] are inspected. pid_t is a
                # 32-bit int: the kernel truncates the register to 32 bits, so
                # low==0xFFFFFFFF is exactly "pid == -1" regardless of what the
                # upper half holds. The upper half MUST NOT be matched — the
                # x86-64 ABI leaves it undefined for int arguments, and glibc's
                # ``movl`` zero-extends, so kill(-1) typically arrives as
                # 0x00000000_FFFFFFFF (a high==0xFFFFFFFF check silently never
                # fires, which is a filter bypass, not a compat issue).
                _insns = []
                # Load arch: BPF_LD | BPF_W | BPF_ABS, offset=4 (seccomp_data.arch)
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 4))
                # If arch == expected, skip next insn (jt=1); else fall through to kill
                _insns.append(_struct.pack("<HBBI", _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, _expected_arch))
                # Kill on unexpected arch (blocks i386 int 0x80 bypass)
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_KILL))
                # Load syscall number: BPF_LD | BPF_W | BPF_ABS, offset=0
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 0))
                # For each denied syscall: JEQ -> DENY (at index k+4)
                _n_deny = len(_DENY_SYSCALLS)
                for _i, _nr in enumerate(_DENY_SYSCALLS):
                    _jt = (_n_deny - _i - 1) + 4  # jumps to the DENY RET at k+4
                    _insns.append(_struct.pack("<HBBI",
                        _BPF_JMP | _BPF_JEQ | _BPF_K, _jt, 0, _nr))
                # k: nr == kill ? fall into arg check : jump to ALLOW (k+3)
                _insns.append(_struct.pack("<HBBI",
                    _BPF_JMP | _BPF_JEQ | _BPF_K, 0, 2, _KILL_NR))
                # k+1: load args[0] low word (offset 16, little-endian layout)
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 16))
                # k+2: low == 0xFFFFFFFF (pid -1) ? DENY (skip 1) : fall to ALLOW
                _insns.append(_struct.pack("<HBBI",
                    _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, 0xFFFFFFFF))
                # ALLOW: return SECCOMP_RET_ALLOW
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ALLOW))
                # DENY: return SECCOMP_RET_ERRNO | EPERM
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM))

                _prog_bytes = b"".join(_insns)
                _n_insns = len(_insns)

                # struct sock_fprog {{ unsigned short len; struct sock_filter *filter; }}
                class _SockFprog(ctypes.Structure):
                    _fields_ = [("len", ctypes.c_ushort),
                                ("filter", ctypes.c_char_p)]

                _fprog = _SockFprog()
                _fprog.len = _n_insns
                _fprog.filter = _prog_bytes
                _ret = _libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER,
                                   ctypes.addressof(_fprog), 0, 0)
                if _ret != 0:
                    sys.exit("sandbox: BLOCKED — failed to install seccomp-BPF filter (prctl returned %d)" % _ret)

        # ── Step 7: Pre-exec hardlink scan ──
        # Scan the agent workspace + /tmp for hardlinks (nlink > 1) whose
        # inode matches a protected credential file. If found, refuse to exec.
        # Only credential inodes with st_nlink > 1 enter the match set: an
        # inode with a single link has no alias anywhere on the filesystem, so
        # when every credential has nlink == 1 the walk is skipped entirely
        # and the common healthy-host spawn pays nothing. When a walk does
        # run, each root gets its OWN scan budget so a large workspace cannot
        # starve the /tmp scan (the world-writable root this check exists
        # for). On budget exhaustion the scan deliberately degrades OPEN with
        # a stderr warning rather than failing closed: /tmp on a busy host can
        # exceed any fixed budget from ordinary telemetry/cache churn, and
        # exiting here would break every sandbox spawn on such hosts. The cost,
        # plainly: an alias past the budget -- or past the quieter depth limit
        # below -- is never stat'd, so a second path to a credential inode goes
        # unchecked even though every mount held.
        #
        # REGULAR FILES ONLY, and that guard is what keeps the walk rare. Linux
        # does not allow a hardlink to a directory, so nlink > 1 says nothing
        # about a directory — and every directory has nlink >= 2 for `.` and
        # `..`. `SENSITIVE_FILES` deliberately carries every hidden path of BOTH
        # kinds (the hiding loops classify per entry, see `_build_launcher_script`),
        # so without this check two ordinary directories — `~/.kiro/crew-auth-staging`
        # and `~/.gnupg` on the measuring host — seeded the match set on every
        # spawn. The 100k-entry walk of $CWD and /tmp then ran every time, costing
        # 1.5s per sandboxed spawn and emitting the truncation warning constantly,
        # while no credential had an alias at all.
        _protected_inodes = set()
        for _pd in SENSITIVE_DIRS:
            if os.path.isdir(_pd):
                for _root, _dirs_scan, _files_scan in os.walk(_pd):
                    for _fname in _files_scan:
                        try:
                            _st = os.stat(os.path.join(_root, _fname))
                            if stat.S_ISREG(_st.st_mode) and _st.st_nlink > 1:
                                _protected_inodes.add((_st.st_dev, _st.st_ino))
                        except OSError:
                            pass
                    break  # depth=1 for credential dirs
        for _pf in SENSITIVE_FILES:
            try:
                _st = os.stat(_pf)
                if stat.S_ISREG(_st.st_mode) and _st.st_nlink > 1:
                    _protected_inodes.add((_st.st_dev, _st.st_ino))
            except OSError:
                pass

        if _protected_inodes:
            _MAX_SCAN_PER_ROOT = 100000
            _dangerous_links = []
            _truncated_roots = []
            _cwd = os.getcwd()
            for _scan_root in (_cwd, "/tmp"):
                if not os.path.isdir(_scan_root):
                    continue
                _root_scanned = 0
                _root_truncated = False
                for _root2, _dirs2, _files2 in os.walk(_scan_root):
                    # Depth limit: max 5 levels
                    _depth = _root2[len(_scan_root):].count(os.sep)
                    if _depth > 5:
                        _dirs2.clear()
                        continue
                    for _fn2 in _files2:
                        if _root_scanned >= _MAX_SCAN_PER_ROOT:
                            _root_truncated = True
                            break
                        _root_scanned += 1
                        _fp2 = os.path.join(_root2, _fn2)
                        try:
                            _st2 = os.lstat(_fp2)
                            if _st2.st_nlink > 1:
                                if (_st2.st_dev, _st2.st_ino) in _protected_inodes:
                                    _dangerous_links.append(_fp2)
                        except OSError:
                            pass
                    if _root_truncated:
                        break
                if _root_truncated:
                    _truncated_roots.append((_scan_root, _root_scanned))
            for _t_root, _t_count in _truncated_roots:
                print(
                    "sandbox: WARNING — pre-exec hardlink scan truncated at "
                    "%d files in %s; scan incomplete (control degrades open)"
                    % (_t_count, _t_root),
                    file=sys.stderr,
                )
            if _dangerous_links:
                sys.exit(
                    f"sandbox: BLOCKED — found hardlink(s) to protected credential "
                    f"inodes: {{_dangerous_links[:5]}}. Remove them before running."
                )

        os.execvp(argv[0], argv)

if __name__ == "__main__":
    main()
'''


def _ensure_run_dir() -> str:
    """Create ``<config_dir>/run/`` with mode 0o700, falling back to tmpdir on failure."""
    run_dir = str(config_dir() / "run")
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        # exist_ok does not re-apply mode on existing dirs — enforce explicitly.
        # 0o700 (owner-only rwx) is deliberately restrictive: this dir holds
        # per-session sandbox launcher scripts and sockets that must NOT be
        # world-readable. Semgrep's 0o644 suggestion is wrong for a directory
        # (needs the execute/traverse bit) and would loosen, not tighten, access.
        os.chmod(run_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
    except OSError:
        logger.warning("Cannot create %s; falling back to system tmpdir", run_dir)
        run_dir = tempfile.gettempdir()
    return run_dir


# Interpreter flags for the namespace launcher. These matter for CONFINEMENT
# ORDERING, not tidiness: the launcher IS a Python process, and everything the
# interpreter does at startup happens BEFORE the script reaches ``unshare``. With
# site processing enabled, ``site`` executes code from env-derived paths at startup
# -- user-site ``.pth`` files (whose location comes from ``PYTHONUSERBASE``, else
# ``HOME``) and ``sitecustomize`` (from ``PYTHONPATH``). For a config-declared
# server ``env`` block that is externally authorable text, so it is arbitrary code
# running unconfined. No argv[0] pin helps: the interpreter is the pinned binary.
#   -I (isolated) ignores PYTHON* startup vars and drops the script dir from
#      sys.path; implies -E and -s.
#   -S skips ``site`` altogether, which is what closes the class rather than
#      individual keys -- no .pth and no sitecustomize run at all.
# Safe because the generated launcher imports stdlib only (sys, os, stat, struct,
# tempfile, platform, ctypes) and never needs site-packages. The namespace probe
# and spawn shims already start their interpreters with these exact flags; this
# launcher was the one Python entrypoint that did not.
_LAUNCHER_INTERPRETER_FLAGS: tuple[str, ...] = ("-I", "-S")


def _launcher_script_of(launcher_argv: list[str]) -> str:
    """The generated launcher script inside a ``namespace_argv`` result.

    Derived from the flag count rather than hardcoded, so adding a flag cannot
    silently return a flag token where a path is expected — which would both leak
    the tempfile and hand the caller ``"-I"`` to ``unlink``.
    """
    return launcher_argv[1 + len(_LAUNCHER_INTERPRETER_FLAGS)]


def namespace_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> list[str]:
    """Wrap *argv* via the Python namespace launcher.

    The launcher forks, the parent writes identity UID/GID maps, and the
    child bind-mounts empty dirs over credential paths before exec.
    The child retains the real UID/GID.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])

    # Give the seal something to mount ON, or refuse the spawn. ``READONLY_DIRS`` is
    # guarded on
    # ``os.path.exists`` in the launcher (a ceiling may be a plain file, so the guard
    # cannot be ``isdir``), and an absent ceiling therefore gets no bind + remount pair
    # at all — leaving the data home writable at that name for the whole sandbox.
    # Materialising the sealable subset first is what makes the seal non-vacuous on a
    # default install. Runs before the script is built so the paths exist by the time
    # the child mounts, and raises ``SandboxCeilingUnsealable`` rather than launching
    # with a keystone the seal could not cover.
    _materialize_sealable_ceilings()

    script = _build_launcher_script(
        sandbox_level,
        strip_python_env=strip_python_env,
        extra_hidden_dirs=extra_hidden_dirs,
        extra_visible_dirs=extra_visible_dirs,
    )
    run_dir = _ensure_run_dir()
    fd, path = tempfile.mkstemp(
        suffix=".py", prefix=f"kirocrew_sandbox_{os.getpid()}_", dir=run_dir
    )
    os.write(fd, script.encode())
    os.close(fd)
    platform_compat.chmod_safe(path, 0o700)

    return [sys.executable, *_LAUNCHER_INTERPRETER_FLAGS, path, *resolved_argv]


# ── Backend: macOS sandbox-exec ──

_SEATBELT_PROFILE = """\
(version 1)
(allow default)
{deny_rules}
"""


def _build_seatbelt_profile(
    sandbox_level: str = "strict",
    *,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> str:
    """Build a Seatbelt .sb profile denying reads of sensitive dirs."""
    home = str(Path.home())
    # Source the sensitive-dir lists from the active PlatformContext (Default
    # adapter == today's module globals; internal companion adds .midway/.ada).
    if sandbox_level == "standard":
        dirs = _STANDARD_DIRS
    elif sandbox_level == "cc":
        # On macOS, don't hide .aws — credential_process and SSO token
        # caches live under .aws/ and Seatbelt can't do partial exposure
        # as cleanly as Linux bind mounts. Deny patterns still block LLM
        # tool reads of credential files. The .aws-exclusion is applied to the
        # context-sourced list so a companion's extra cc dirs are still hidden.
        dirs = [d for d in _sandbox_policy().cc_dirs() if d != ".aws"]
    else:
        dirs = _sandbox_policy().strict_dirs()
    files = _CC_FILES if sandbox_level in ("cc", "strict") else []
    expose_files = _CC_EXPOSE_FILES if sandbox_level == "cc" else []
    expose_abs = {os.path.join(home, f) for f in expose_files}
    crew_hidden = _crew_hidden_sandbox_targets()
    rules: list[str] = []
    for target in (
        [os.path.join(home, d) for d in dirs]
        + _relocated_policy_cache_dirs()
        + _relocated_crew_targets(_CREW_HIDDEN_LEAVES)
        + list(_voice_runtime_sandbox_paths())
    ):
        if _hidden_path_contains_visible_path(
            target, extra_visible_dirs
        ) and not _is_voice_runtime_dir(target):
            # An exposed governance cache stays READ-only: keep the write and hardlink
            # denies and drop only the read deny. `extra_visible_dirs` otherwise cancels
            # the target's whole rule set, which would hand the one caller that needs to
            # read the ceiling (`apps/backend.py` in cache-only mode) the ability to
            # rewrite it — and the metadata records the source the next boot trusts, so
            # that is the dangerous direction. Mirrors READONLY_DIRS on Linux.
            if _is_policy_cache_dir(target):
                sealed = target.replace('"', '\\"')
                rules.append(f'(deny file-write* (subpath "{sealed}"))')
                rules.append(f'(deny file-link (subpath "{sealed}"))')
            continue
        escaped = target.replace('"', '\\"')
        # Check if any exposed files live under this dir
        exposed_in_dir = [f for f in expose_abs if f.startswith(target + "/")]
        if exposed_in_dir:
            exceptions = " ".join(
                f'(require-not (literal "{f.replace(chr(34), chr(92) + chr(34))}"))'
                for f in exposed_in_dir
            )
            rules.append(f'(deny file-read* (require-all (subpath "{escaped}") {exceptions}))')
        else:
            rules.append(f'(deny file-read* (subpath "{escaped}"))')
        if _is_policy_cache_dir(target) or _is_voice_runtime_dir(target) or target in crew_hidden:
            # Linux bind-mounts these roots away, which blocks both directions.
            # macOS needs an explicit write deny as well as the read rule above:
            # governance metadata is a trust root, a writable voice-runtime image would
            # race the gateway's authenticated decoder spawn, and a crew-home secret that
            # is read-denied but writable can still be OVERWRITTEN -- forging
            # ``token_signing.key`` needs no read at all. Scoped to those three sets on
            # purpose: widening it to every hidden entry would also cover .aws, which a
            # tool rewrites legitimately when it refreshes a cached token.
            rules.append(f'(deny file-write* (subpath "{escaped}"))')
            if target in crew_hidden:
                # A leaf may be a plain file, which no subpath rule addresses.
                rules.append(f'(deny file-write* (literal "{escaped}"))')
        # Deny creating a HARDLINK whose target is under this dir.
        # Seatbelt's file-read* deny is path-based, so a hardlink at a
        # non-denied path (e.g. /tmp) reads the same inode past the deny rule.
        # ``file-link`` fires on the link TARGET, so this stops the sandboxed
        # agent from minting such a hardlink in the first place.  Blanket (no
        # exposed-file exception): the agent never needs to hardlink a
        # credential-dir file, and blocking it is harmless.
        rules.append(f'(deny file-link (subpath "{escaped}"))')

    # The voice image lives below ``run``. Keep that parent readable (the
    # sandbox launcher itself is stored there), but deny every write through
    # both lexical and canonical spellings. Literal ancestor rules prevent a
    # same-UID agent from renaming a parent around the path-based subtree deny.
    for target in _voice_runtime_parent_paths():
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
    for target in _voice_runtime_ancestor_guards():
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
    # The crew data home's ceilings: readable (in-sandbox code resolves them) but never
    # writable, so a sandboxed process cannot hand itself a ceiling. Mirrors
    # READONLY_DIRS on Linux. Both spellings, because a ceiling may be a file
    # (``literal``) or a directory (``subpath``), and ``file-link`` stops the agent
    # minting a writable alias to the same inode.
    for target in [
        os.path.join(home, rel) for rel in _CREW_READONLY_TARGETS
    ] + _relocated_crew_targets(_CREW_READONLY_LEAVES):
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
    for f in files:
        target = os.path.join(home, f)
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-read* (literal "{escaped}"))')
        # Also deny hardlinking the protected file (see above).
        rules.append(f'(deny file-link (literal "{escaped}"))')
    for target in dict.fromkeys(os.path.abspath(path) for path in extra_hidden_dirs):
        if _hidden_path_contains_visible_path(target, extra_visible_dirs):
            continue
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-read* (subpath "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
        # BOTH shapes, because most of this list is plain FILES, not directories:
        # sandbox_credential_targets() yields .codex/auth.json,
        # .claude/.credentials.json, .netrc, .git-credentials, .npmrc, .pypirc,
        # .docker/config.json, .kube/config, sel_hmac.key, token_signing.key.
        # Whether a subpath rule alone covers a plain file is asserted in three
        # comments in this tree and CONTRADICTED by the crew_hidden branch above
        # ("A leaf may be a plain file, which no subpath rule addresses"), and
        # nothing tests it -- no test in this repo executes sandbox-exec, so the
        # claim has never been checked against the kernel. This mask is the ONLY
        # compensating control for a harness whose passive reads never reach the
        # gate, so it must not rest on an unverified reading of Seatbelt: the
        # literal is redundant if subpath does cover files, and load-bearing if it
        # does not.
        rules.append(f'(deny file-read* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-link (literal "{escaped}"))')

    # .ssh: deny all access except reading known_hosts (strict only)
    if sandbox_level == "strict":
        ssh_dir = os.path.join(home, ".ssh")
        ssh_escaped = ssh_dir.replace('"', '\\"')
        ssh_kh = os.path.join(ssh_dir, "known_hosts")
        ssh_kh_escaped = ssh_kh.replace('"', '\\"')
        rules.append(
            f'(deny file-read* (require-all (subpath "{ssh_escaped}")'
            f' (require-not (literal "{ssh_kh_escaped}"))))'
        )
        rules.append(f'(deny file-write* (subpath "{ssh_escaped}"))')
        # Block hardlinking any .ssh file (private keys) out of the
        # denied subtree.  Blanket over the whole subpath — no known_hosts
        # exception, since a hardlink to known_hosts has no legitimate use.
        rules.append(f'(deny file-link (subpath "{ssh_escaped}"))')

    return _SEATBELT_PROFILE.format(deny_rules="\n".join(rules))


# kiro-cli >= 2.13 ships its own internal agent sandbox, toggled by the
# "sandbox" key in this settings file. On macOS its in-process seatbelt init
# cannot nest inside KiroCrew's sandbox-exec wrap — the kernel returns EPERM
# even under an (allow default) outer profile. Exactly one sandbox layer can be
# active per spawn, so on macOS the layers are mutually exclusive:
# kiro's internal sandbox ON  -> KiroCrew's seatbelt OFF for kiro-cli spawns
# kiro's internal sandbox OFF -> KiroCrew's seatbelt ON (unchanged default)
# (``~/.kiro/settings`` is the kiro-cli backend's own directory, distinct from
# KiroCrew's data home ``~/.kiro/crew``; the filename is the literal kiro-cli ships.)
_KIRO_INTERNAL_SETTINGS_PATH = "~/.kiro/settings/amazon-internal.json"
_KIRO_INTERNAL_SANDBOX_KEY = "sandbox"

# One loud warning per process for the delegation decision (per-spawn logs
# would spam warm-pool refills); every delegated spawn is still SEL-audited.
_kiro_delegation_warned = False


def _pinned_env_bin() -> str:
    """Absolute path to ``env``, resolved WITHOUT consulting PATH.

    ``env`` is the process that applies the credential scrub (``env -u KEY ...``)
    on the delegation paths, where no Seatbelt/namespace layer wraps the child.
    A bare ``"env"`` token is resolved by the OS through the PATH in the
    environment we hand ``Popen`` -- and on the script-cron MCP path that PATH can
    come from a config-declared server ``env`` block. Redirecting ``env`` does not
    merely run an attacker binary: it means the scrub NEVER RUNS, so the child
    receives the very credentials (Slack tokens, owner id) the ``-u`` flags exist
    to strip, and can exfiltrate them. Pinning is therefore load-bearing on these
    paths even though they intentionally apply no OS confinement of our own.

    ``trusted_system_bin`` ignores ``os.environ`` PATH entirely (fixed system dirs
    only); the ``/usr/bin/env`` fallback matches the idiom already used by
    ``sandbox_exec_argv`` and ``cgroup_scope_argv`` so an unusual host layout
    still yields an absolute path rather than a redirectable bare name.

    Deliberately does NOT pin the inner command: that is the operator's agent or
    server binary (``kiro-cli``, ``npx``, ...), which legitimately must be found on
    PATH, and it carries the same config-file trust level as the ``env`` block
    itself -- an author who can set PATH can already set ``command`` directly, so
    pinning it would buy nothing while breaking normal installs.
    """
    return platform_compat.trusted_system_bin("env") or "/usr/bin/env"


def kiro_internal_sandbox_enabled() -> bool:
    """True when kiro-cli's own internal agent sandbox is enabled.

    Reads the ``"sandbox"`` key from ``~/.kiro/settings/amazon-internal.json``
    (kiro-cli >= 2.13). Absent file, missing key, or parse failure all return
    False, which keeps KiroCrew's own sandbox engaged — failure resolves
    toward our audited isolation layer, never toward no isolation.

    Deliberately uncached: it is one small-file read per spawn, and caching
    would make a settings flip require a gateway restart (mirrors the
    uncached ``_resolve_kiro_bin`` rationale).
    """
    # Deferred import: sandbox.py is a low-level leaf that deliberately avoids a
    # top-level dependency on hooks (hooks imports sandbox at call time). The
    # read is routed through hooks.safe_read_file (security-controls): the file
    # is user-writable, so the read gets is_sensitive_path() on the RESOLVED
    # target (a symlink into ~/.aws etc. is refused through the link) plus
    # O_NOFOLLOW against a TOCTOU swap of the final component.
    from kiro_crew.hooks import safe_read_file

    try:
        data = json.loads(safe_read_file(_KIRO_INTERNAL_SETTINGS_PATH))
        if not isinstance(data, dict):
            # Valid-but-non-object JSON ([], "str", null, 123) must also
            # resolve toward KiroCrew's own sandbox, not raise.
            return False
        return bool(data.get(_KIRO_INTERNAL_SANDBOX_KEY, False))
    except (OSError, ValueError, RuntimeError):
        # OSError covers missing file / EACCES / PermissionError (sensitive
        # or symlinked target refused by hooks); ValueError covers JSON
        # decode; RuntimeError covers home-directory resolution failure.
        # Every failure resolves toward KiroCrew's own sandbox.
        return False


def _spawns_kiro_cli(argv: list[str]) -> bool:
    """True when *argv* launches kiro-cli (by basename, the same convention
    as ``_resolve_kiro_bin``).

    Only the kiro-cli spawn may delegate isolation to kiro's internal
    sandbox — every other agent-influenced spawn (e.g. an MCP probe or a
    cron script) has no internal sandbox of its own and MUST keep KiroCrew's
    wrap regardless of the kiro settings file.
    """
    return bool(argv) and Path(argv[0]).name == "kiro-cli"


def _delegate_to_kiro_internal_sandbox(
    argv: list[str],
    sandbox_level: str,
    *,
    strip_python_env: bool = False,
) -> tuple[list[str], str | None] | None:
    """Delegate an explicitly trusted kiro-cli spawn to its internal sandbox.

    This is NOT the forbidden silent unsandboxed fallback: the child still
    runs under kiro-cli's own sandbox. On macOS the delegation is config-driven
    mutual exclusion with Kiro Crew's seatbelt; on Windows it is restricted to a
    positive first-party Kiro backend classification because Kiro Crew has no
    native OS wrapper there. The decision is deterministic (never a reaction to
    a wrap failure), logged loudly once per process, and every delegated spawn
    is SEL-audited on an audit-or-deny basis. If the audit event cannot be
    written, ``None`` tells the caller to continue through the normal sandbox
    policy, which fail-closes on Windows.

    The POSIX env scrub is applied inline. Windows has no ``env -u`` launcher,
    so every production caller must pass :func:`scrub_agent_subprocess_env`'s
    result as the child environment; regression tests pin those call sites.

    Deliberately does NOT resolve the real kiro binary: the launcher shim is
    part of kiro's own sandbox mechanism on this path, so bypassing it here
    would defeat the delegated layer.
    """

    global _kiro_delegation_warned
    try:
        # circular import (pre-emptive, layering): sandbox.py is a low-level
        # leaf imported at module level by many modules including subprocess
        # entry points. A top-level dep on sel would invert the low-level ->
        # high-level layering; deferring to this rarely-taken path keeps
        # sandbox leaf-pure.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="sandbox",
            agent="system",
            source="sandbox.wrap_argv",
            tool_name=_command_log_label(argv),
            tool_kind="subprocess",
            outcome="delegated",
            resources=(
                "Windows Kiro backend delegation: kiro internal sandbox owns "
                "this spawn; Kiro Crew has no native Windows sandbox backend"
                if sys.platform == "win32"
                else "macOS sandbox mutual exclusion: kiro internal sandbox on -> "
                "KiroCrew seatbelt off for this kiro-cli spawn"
            ),
            # audit-or-deny: written synchronously; a filesystem failure
            # re-raises so an unaudited delegation can never proceed.
            critical=True,
        )
    except Exception:
        # Fail closed (security-controls): a security delegation that cannot
        # be audited does not happen. The caller continues through Kiro Crew's
        # normal policy: macOS gets its seatbelt; Windows, which has no native
        # backend, raises SandboxUnavailableError rather than run unaudited.
        logger.warning(
            "SEL audit failed for sandbox delegation — refusing unaudited "
            "delegation; falling back to Kiro Crew's sandbox policy",
            exc_info=True,
        )
        return None
    # SEL audit succeeded — delegation is actually proceeding. Only now
    # consume the warn-once flag (a SEL-failed attempt above fell back to
    # seatbelt and must not burn the warning for the first real delegation).
    if not _kiro_delegation_warned:
        _kiro_delegation_warned = True
        logger.warning(
            "SECURITY: delegating this %s kiro-cli spawn to kiro-cli's internal "
            "sandbox and skipping Kiro Crew's OS wrapper. Env scrubbing still "
            "applies.",
            "Windows" if sys.platform == "win32" else "macOS",
        )
    if sys.platform == "win32":
        return list(argv), None
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env)
    if unset_args:
        return [_pinned_env_bin(), *unset_args, *argv], None
    return list(argv), None


def sandbox_exec_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> tuple[list[str], str | None]:
    """Wrap *argv* with ``sandbox-exec -f <profile>``.

    Also scrubs sensitive env vars via ``env -u`` since Seatbelt only
    handles file-level deny rules, not environment variables.

    Returns (new_argv, tmp_profile_path).  Caller should delete the
    profile file after the child exits.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])

    profile = _build_seatbelt_profile(
        sandbox_level,
        extra_hidden_dirs=extra_hidden_dirs,
        extra_visible_dirs=extra_visible_dirs,
    )
    run_dir = _ensure_run_dir()
    fd, path = tempfile.mkstemp(
        suffix=".sb", prefix=f"kirocrew_sandbox_{os.getpid()}_", dir=run_dir
    )
    os.write(fd, profile.encode())
    os.close(fd)
    # Build env -u flags for sensitive vars present in current env. cc/strict
    # additionally scrub agent-denied credential keys (Slack tokens, owner id)
    # since loader.py seeds them into os.environ for trusted children only.
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env)
    # Mark the sandboxed tree, exactly as the Linux namespace launcher does after
    # its own env scrub (see the export beside ``KIROCREW_HOST_PID``). Without
    # this, an in-sandbox ``wrap_argv`` call cannot tell that KiroCrew's own
    # sandbox already confines it, tries to nest, and gets EPERM — which then
    # fail-closes every app-backend and MCP spawn on the host. Set as an ``env``
    # assignment so it lands AFTER the ``-u`` flags and cannot be dropped by them.
    marker = f"{_IN_SANDBOX_MARKER}=1"
    # Record the tier beside the marker, in the same after-the-``-u``-flags
    # position so the scrub cannot drop it — an in-sandbox wrap_argv
    # passthrough compares it against the requested tier to detect downgrades.
    level_assign = f"{_IN_SANDBOX_LEVEL_VAR}={sandbox_level}"
    # SECURITY: BOTH wrappers this function prepends are pinned here, at the layer
    # that prepends them, so no spawn site has to remember to re-pin (the caller's
    # ``env`` may carry a config-declared PATH, and CPython resolves a slash-less
    # argv[0] through THAT PATH via os.get_exec_path):
    #   * the outer ``env`` (argv[0]), which runs first of all;
    #   * the inner ``sandbox-exec``, which ``env`` itself resolves through the
    #     PATH in the environment it is handed -- BEFORE the Seatbelt profile is
    #     applied, so a hostile PATH there is a pre-confinement escape.
    # ``trusted_system_bin`` ignores PATH entirely rather than reading os.environ:
    # a gateway's PATH can legitimately lead with agent-writable directories
    # (a worktree venv's bin, ~/.local/bin), so resolving through it would leave
    # the hole half-open. Both fall back to their canonical macOS locations,
    # matching the _probe_sandbox_exec probe, so a host with an unusual layout
    # still gets an absolute path rather than a redirectable bare name.
    outer_env = _pinned_env_bin()
    sandbox_exec = platform_compat.trusted_system_bin("sandbox-exec") or "/usr/bin/sandbox-exec"
    return (
        [outer_env, *unset_args, marker, level_assign, sandbox_exec, "-f", path, *resolved_argv],
        path,
    )


def _sandbox_env_scrub_keys(sandbox_level: str, strip_python_env: bool) -> list[str]:
    """Names of the live environment keys to scrub for a given sandbox level.

    The single source of the per-level scrub set, shared by
    :func:`_sandbox_env_unset_args` (which renders it as ``env -u`` flags) and
    the first-party no-backend carve-out in :func:`wrap_argv` (which hands the
    keys to :func:`_unset_env_argv` for a trusted-absolute-path ``env`` prefix),
    so the two paths can never scrub different sets.
    """
    prefixes = list(_SENSITIVE_ENV_PREFIXES)
    if sandbox_level in ("cc", "strict"):
        prefixes.extend(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        prefixes.extend(_PYTHON_ENV_PREFIXES)
    return [key for key in os.environ if any(key.startswith(p) for p in prefixes)]


def _sandbox_env_unset_args(sandbox_level: str, strip_python_env: bool) -> list[str]:
    """``env -u`` flags scrubbing sensitive vars for a sandboxed/delegated spawn.

    Shared by ``sandbox_exec_argv`` (seatbelt wrap) and
    ``_delegate_to_kiro_internal_sandbox`` (macOS mutual-exclusion path) so the
    env-scrub guarantee is identical whether or not KiroCrew's own seatbelt is
    the active isolation layer.
    """
    unset_args: list[str] = []
    for key in _sandbox_env_scrub_keys(sandbox_level, strip_python_env):
        unset_args.extend(["-u", key])
    return unset_args


def _parse_pid_segment(pid_str: str) -> int | None:
    """Parse a pid segment from a sweep-owned filename, or None to skip.

    Both sweeps (launcher scripts and mount sources) only ever WRITE an ASCII
    positive decimal pid, so anything else is a foreign or planted name and
    must fail toward "skip": non-ASCII decimals (``int()`` would accept
    them), pid ``0`` (``os.kill(0, 0)`` probes the caller's own process group
    and always reads alive), zero-padded segments, and — belt-and-braces,
    NAME_MAX keeps real names far shorter than the int/str conversion limit —
    a ``ValueError`` from ``int()`` itself.
    """
    if not pid_str.isascii() or not pid_str.isdecimal() or pid_str.startswith("0"):
        return None
    try:
        return int(pid_str)
    except ValueError:
        return None


def cleanup_stale_sandbox_profiles(*, legacy_dir: str | None = None) -> int:
    """Remove orphan sandbox files from <config_dir>/run/ and legacy /tmp.

    A file is removed when EITHER:
      - The tagged PID is dead (os.kill probe fails), OR
      - The file mtime is older than _LAUNCHER_MAX_AGE_SECONDS (the launcher
        is consumed exactly once at child exec, so old files are garbage
        regardless of PID liveness — this handles the spawner-PID design
        where the gateway PID is always alive for current-generation files).

    Also sweeps legacy /tmp/kirocrew_sandbox_*.py files that predate the
    migration to <config_dir>/run/ — these have no PID segment, so only the
    age threshold applies — plus the orphaned bind-mount sources the namespace
    launcher stages on tmpfs (see _cleanup_stale_sandbox_mount_sources).

    Called from the periodic cleanup sweep in session.py, offloaded to the
    maintenance executor (blocking I/O).  Safe to call from sync contexts too.

    Returns:
        Number of stale files removed.
    """
    now = time.time()
    if legacy_dir is None:
        legacy_dir = _LEGACY_LAUNCHER_DIR
    run_dir = str(config_dir() / "run")
    removed = 0

    # ── Sweep <config_dir>/run/ (PID + age) ──
    if os.path.isdir(run_dir):
        for entry in os.listdir(run_dir):
            if not entry.startswith("kirocrew_sandbox_"):
                continue
            if entry.endswith(".sb"):
                suffix = ".sb"
            elif entry.endswith(".py"):
                suffix = ".py"
            else:
                continue
            filepath = os.path.join(run_dir, entry)
            # Age check first — handles the spawner-PID design flaw
            try:
                mtime = os.stat(filepath).st_mtime
            except OSError:
                continue
            if (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                try:
                    os.remove(filepath)
                    removed += 1
                except OSError:
                    pass
                continue
            # Fresh file — fall back to PID liveness check
            middle = entry[len("kirocrew_sandbox_") : -len(suffix)]
            pid = _parse_pid_segment(middle.split("_", 1)[0])
            if pid is None:
                continue
            # Liveness probe via the shim — NEVER raw os.kill(pid, 0), which
            # TERMINATES the target process on Windows (see platform_compat).
            try:
                alive = platform_compat.pid_exists(pid)
            except OverflowError:
                alive = False  # absurd pid digits from a corrupt filename — stale
            if not alive:
                try:
                    os.remove(filepath)
                    removed += 1
                except OSError:
                    pass

    # ── Sweep legacy /tmp/kirocrew_sandbox_*.py (age only, no PID segment) ──
    if os.path.isdir(legacy_dir):
        try:
            with os.scandir(legacy_dir) as it:
                for dentry in it:
                    if not dentry.name.startswith("kirocrew_sandbox_"):
                        continue
                    if not dentry.name.endswith(".py"):
                        continue
                    try:
                        mtime = dentry.stat().st_mtime
                    except OSError:
                        continue
                    if (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                        try:
                            os.remove(dentry.path)
                            removed += 1
                        except OSError:
                            pass
        except OSError:
            pass

    removed += _cleanup_stale_sandbox_mount_sources()
    removed += _cleanup_legacy_mount_source_residue()
    removed += _cleanup_retired_acp_snapshot_dir()
    return removed


def _mount_source_candidate_roots() -> list[str]:
    """The tmpfs roots the namespace launcher stages bind-mount sources on.

    Mirrors the launcher's own ``_tmpfs_src`` candidate chain —
    ``/run/user/$UID``, ``/dev/shm``, then the system default tempdir (the
    ``_tmpfs_src=None`` fallback). The tempdir entry is the gateway's own
    ``tempfile.gettempdir()``, which matches the launcher's fallback only while
    both resolve the same ``TMPDIR``; a launcher spawned with a divergent,
    persistent ``TMPDIR`` stages its fallback entries somewhere this sweep
    never visits, and nothing else reclaims them — a real limitation, reached
    only when NO tmpfs candidate exists at all. ``os.getuid`` is
    POSIX-only; the launcher itself is Linux-only, so a platform without it
    simply has no per-user runtime root to sweep.
    """
    roots: list[str] = []
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        roots.append(f"/run/user/{getuid()}")
    roots.append("/dev/shm")
    roots.append(tempfile.gettempdir())
    return roots


def _mount_pinned_source_names(
    proc_root: str = "/proc",
    *,
    matcher: Callable[[str], bool] | None = None,
    coverage: _PinScanCoverage | None = None,
) -> tuple[set[str], bool]:
    """Entry names of sandbox mount sources referenced by a live mount, plus
    whether the scan positively covered every namespace a PROCESS could be
    binding one from.

    A bind mount records its source as the ``root`` field (field 4) of a
    ``/proc/<pid>/mountinfo`` line, and the staged sources are direct children
    of a tmpfs root, so the basename of that field is the staged entry's name
    (mkdtemp names carry no spaces, so mountinfo's octal escaping never
    applies to them). Only prefix-shaped basenames are collected — a foreign
    mount whose root merely resembles a path cannot pin anything. Keying is by
    basename, so two identically-named entries on different roots would share
    a pin; mkdtemp's random suffix makes that vanishingly rare, and the error
    lands on the retention side.

    The launcher's mounts live in the sandboxed child's PRIVATE namespace —
    invisible in the gateway's own mountinfo — and that namespace can outlive
    the launcher pid through any surviving descendant (a build daemon, for
    example), so every readable pid's mountinfo is consulted. Coverage is
    what makes the answer usable, and it is established positively:

    - The listing must show pid 1. ``hidepid``/``subset=pid`` procfs hides
      other users' processes entirely — including a root holder that entered
      a sandbox namespace — and pid 1 always exists, so its absence proves
      the listing is filtered and the scan reports incomplete. It still reads
      every pid the filtered listing DOES show (this uid's own processes are
      never hidden from it, and every sandbox descendant keeps this uid), so
      the pins a visible holder contributes reach the caller regardless: the
      directory gate honours ``pinned`` before any other evidence.
    - A holder can fork a successor and exit between the pid listing and its
      own mountinfo read (the read then raises FileNotFoundError). The
      successor was forked BEFORE the exit, so it is visible to the very next
      listing: the scan re-lists after any pass that observed a vanish, and
      finishes on a pass that saw none. A pid appearing WITHOUT a vanish
      needs no rescan — it is either in a namespace whose surviving holders
      this scan already read, or in a brand-new namespace, which can only
      bind brand-new (fresh, live-pid) entries that are never reclaim
      candidates in the same sweep. A scan still churning after
      ``_PIN_SCAN_MAX_PASSES`` cannot prove coverage and reports incomplete.
      (A successor recycled onto an already-seen pid NUMBER inside this
      window escapes the re-listing; that needs the full pid space to wrap
      within the scan's milliseconds, and the residual error is
      retention-side only on the next sweep.)
    - A read failure is forgiven in two cases and counts as a coverage gap
      otherwise. ``EINVAL`` says that TASK has no ``nsproxy``, which is not yet
      a statement about the NAMESPACE: a thread-group leader can exit through
      ``pthread_exit`` while sibling threads keep running, and threads share the
      nsproxy, so a live namespace can sit behind a zombie leader while
      ``proc_root`` — which lists only leaders — shows nothing else to scan. So
      the group is consulted through ``/proc/<pid>/task/<tid>/mountinfo``, with
      exactly two outcomes per sibling and no third: one that READS contributes
      pins and nothing else, and one that does not read, for ANY reason,
      makes coverage unprovable. Departed siblings are counted rather than
      excused, because one exiting between the listing and its read may have
      spawned a successor THREAD first, and a thread is invisible to the outer
      re-listing (which enumerates thread-group leaders only), so no re-listing
      can recover it. Tasks in one group can also hold DIFFERENT mount
      namespaces, since a thread may ``unshare(CLONE_NEWNS)``, so a readable
      sibling never speaks for an unreadable one. When every sibling reads, the
      leader's own departure still makes this a vanish, because a departing task
      may have handed its namespace to a PROCESS forked after this pass's
      listing and only a re-listing can see that. A LIVE leader that could be a
      holder (this uid's, the overflow uid's, root's) has its siblings read the
      same way, since a thread can ``unshare(CLONE_FS)`` + ``setns`` into a
      namespace its leader is not in; a sibling that departed mid-read has its
      whole group re-read on the next pass, one that would not read for any
      other reason makes coverage unprovable. Any OTHER errno on the leader
      is forgiven only when the pid provably belongs to a DIFFERENT real user
      (its ``/proc/<pid>`` stats to a uid that is not ours, not root, and not
      the host's overflow uid) — such a process cannot be binding a source this
      uid staged, because the sandboxed child keeps this uid and NO_NEW_PRIVS.
      Root can enter any namespace, and a holder in a foreign user namespace
      stats as the overflow uid, so those count as coverage gaps.

    A namespace held only by an nsfs fd or a bind-mounted ``ns/mnt`` — zero
    member processes — has no mountinfo to scan and is out of scope; the
    launcher never creates one. ``complete=False`` means absence-of-pin was
    NOT established host-wide. When the caller passes ``coverage``, the scan
    also answers the narrower question the directory gate needs
    (:class:`_PinScanCoverage`): whether every task that could be a launcher
    descendant — this uid's and the overflow uid's — was read, with none
    departing between the final listing and its read. Root's tasks count as
    possible holders too (root can ``nsenter`` any namespace), so a hidden or
    unreadable root task — a filtered procfs included — lowers coverage along
    with ``complete``; another user's unreadable or departing task lowers only
    ``complete``.
    """
    pinned: set[str] = set()
    complete = True
    seen: set[str] = set()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if getuid is not None else None
    overflow_uid = _overflow_uid()
    # Default: the keyed ``kirocrew_sb_<pid>_`` shape. ``matcher`` lets the
    # legacy-residue sweep reuse this traversal — and, critically, its coverage
    # accounting (zombie leaders via task/, the vanish re-listing, the
    # foreign-uid forgiveness) — for a different name shape, instead of a
    # second scan that gets those cases subtly wrong.
    match = matcher or (lambda name: name.startswith(_MOUNT_SOURCE_PREFIX))
    # Fast pre-filter per line; only valid for the default shape, since a
    # custom matcher may accept names without the prefix.
    line_hint = _MOUNT_SOURCE_PREFIX if matcher is None else None

    def _collect(mountinfo_path: str) -> None:
        """Add every matching bind SOURCE named in one mountinfo to ``pinned``.

        Propagates ``OSError`` exactly as ``open`` would, so each caller decides
        what an unreadable task means for coverage. A source removed while
        still bound reads ``.../name//deleted``; the suffix is stripped so the
        real name is what pins.
        """
        with open(mountinfo_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line_hint is not None and line_hint not in line:
                    continue
                fields = line.split()
                if len(fields) > 3:
                    source = fields[3]
                    if source.endswith("//deleted"):
                        source = source[: -len("//deleted")]
                    source = os.path.basename(source)
                    if match(source):
                        pinned.add(source)

    # Coverage accounting for ``coverage``. A task of this uid (or the overflow
    # uid) that could not be read is never re-read (it is in ``seen``), so it
    # clears coverage for good. One that departed between a pass's listing and
    # its read may have handed its namespace to a child forked after that
    # listing: a re-listing shows the child, so a departure on a pass that IS
    # followed by another pass is accounted for, and only the final pass's
    # departures reach the caller.
    unread_sticky = False
    departed_this_pass = False
    uids: dict[str, int | None] = {}

    def _could_be_descendant(name: str) -> bool:
        # A descendant stats as this uid, or as the overflow uid from inside a
        # nested user namespace; root can ``nsenter`` ANY namespace, so a root
        # task counts too. Without a uid to compare against (no ``os.getuid``;
        # an unreadable overflowuid sysctl) every task might be one, so
        # coverage fails closed rather than open.
        uid = uids.get(name)
        if own_uid is None or overflow_uid is None or uid is None:
            return True
        return uid == own_uid or uid == overflow_uid or uid == 0

    def _report() -> None:
        if coverage is not None and (unread_sticky or departed_this_pass):
            coverage.covered = False

    for _ in range(_PIN_SCAN_MAX_PASSES):
        try:
            listed = [n for n in os.listdir(proc_root) if n.isdecimal()]
        except OSError:
            if coverage is not None:
                coverage.covered = False
            return pinned, False
        if "1" not in listed and "1" not in seen:
            # Filtered procfs (hidepid/subset): coverage is unprovable, but the
            # pids that ARE listed — this uid's own, which is where every
            # sandbox descendant lives — still read fine and still pin. Keep
            # walking so their pins reach the caller: a visible holder retains
            # its source. Returning here instead would hand the gate an empty
            # pinned set. Coverage falls with the flag: root's tasks are among
            # the hidden ones, and root can hold any namespace.
            complete = False
            unread_sticky = True
        new_pids = [n for n in listed if n not in seen]
        vanished = False
        departed_this_pass = False
        # Learn every new task's uid FIRST, in one tight loop, so a task that
        # departs during the (much longer) mountinfo walk below can still be
        # told apart from another user's; one gone before even this read is
        # treated as possibly ours.
        uids = {name: _task_uid(proc_root, name) for name in new_pids}
        for name in new_pids:
            seen.add(name)
            try:
                _collect(os.path.join(proc_root, name, "mountinfo"))
            except (FileNotFoundError, ProcessLookupError):
                # May have handed its namespace to a child forked before the
                # exit — visible to the next listing, so take another pass.
                vanished = True
                if _could_be_descendant(name):
                    departed_this_pass = True
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    # EINVAL says THIS TASK's nsproxy is gone, so this task is a
                    # member of no mount namespace. That is NOT yet a statement
                    # about the namespace: a thread-group leader can exit through
                    # ``pthread_exit`` while sibling THREADS keep running, and
                    # threads share the nsproxy, so the namespace — and its binds
                    # on our sources — can still be alive behind a zombie leader.
                    # ``proc_root`` lists only thread-group LEADERS, so the
                    # re-listing below can never see those threads. Measured on a
                    # real zombie leader: ``/proc/<tgid>/mountinfo`` is EINVAL
                    # while ``/proc/<tgid>/task/<live-tid>/mountinfo`` reads fine.
                    # So ask the group before concluding anything; one readable
                    # thread yields the WHOLE namespace's mount table, because
                    # every thread in the group shares it.
                    task_dir = os.path.join(proc_root, name, "task")
                    try:
                        tids = os.listdir(task_dir)
                    except FileNotFoundError:
                        tids = []  # the group is gone entirely
                    except OSError:
                        complete = False  # cannot ask — coverage unprovable
                        if _could_be_descendant(name):
                            unread_sticky = True
                        continue
                    unaccounted = 0
                    for tid in tids:
                        if tid == name:
                            continue  # the leader: it already answered EINVAL
                        try:
                            _collect(os.path.join(task_dir, tid, "mountinfo"))
                        except OSError:
                            unaccounted += 1
                    # INVARIANT, and there is deliberately no third case: a sibling
                    # that READS contributes pins and nothing else, and a sibling
                    # that does not read, for ANY reason, makes coverage unprovable.
                    #
                    # Departed siblings are counted too, rather than excused. One
                    # that exits between the ``tids`` snapshot and its read may have
                    # spawned a successor THREAD first, and a successor thread is
                    # invisible to the outer re-listing, which enumerates
                    # thread-group LEADERS only — so no re-listing can recover it
                    # and only fail-closed is honest. The cost is transient, never a
                    # stall: a sibling caught mid-exit is a race, so the next sweep
                    # sees a settled group, whereas the zombie LEADER this branch
                    # exists for is a steady state and stays reclaimable.
                    #
                    # Tasks in one group can also hold DIFFERENT mount namespaces (a
                    # thread may ``unshare(CLONE_NEWNS)``, and the launcher's own
                    # user namespace grants its descendants the CAP_SYS_ADMIN that
                    # needs), so a readable sibling never speaks for an unreadable
                    # one.
                    if unaccounted:
                        complete = False
                        if _could_be_descendant(name):
                            unread_sticky = True
                        continue
                    # Otherwise this is a VANISH, unconditionally. Reaching this
                    # branch at all means the LEADER departed, and a departing task
                    # may have handed its namespace to a process forked after this
                    # pass's listing, which only a re-listing can see — so a
                    # sibling that could be read must not be able to cancel it:
                    # the pins it yields are kept, its success is not evidence.
                    #
                    # This terminates: the pid enters ``seen`` above and is never
                    # re-read, so a stable zombie costs exactly one extra pass,
                    # while genuine churn exhausts ``_PIN_SCAN_MAX_PASSES`` and
                    # returns complete=False, which retains rather than removes.
                    vanished = True
                    if _could_be_descendant(name):
                        departed_this_pass = True
                    continue
                try:
                    st_uid: int | None = os.stat(os.path.join(proc_root, name)).st_uid
                except OSError:
                    st_uid = None
                if (
                    own_uid is None
                    or st_uid is None
                    or overflow_uid is None
                    or st_uid == own_uid
                    or st_uid == 0
                    or st_uid == overflow_uid
                ):
                    complete = False
                    # Root's or another user's unreadable task is a host-wide
                    # gap only; one that could be a launcher descendant also
                    # clears the narrower coverage the gate relies on.
                    if _could_be_descendant(name):
                        unread_sticky = True
            else:
                # The leader read. Its THREADS may still hold OTHER mount
                # namespaces -- a thread can ``unshare(CLONE_FS)`` and then
                # ``setns`` into a sandbox's namespace while its leader stays
                # outside, and root has the capability to do so anywhere -- and
                # ``proc_root`` lists leaders only, so those namespaces are
                # reachable through ``task/`` alone. Asked only for a possible
                # holder (another user's threads cannot enter a namespace this
                # uid staged), which keeps the cost to this uid's and root's
                # groups: measured 3.5k sibling reads in 0.13s on a dev host.
                if not _could_be_descendant(name):
                    continue
                task_dir = os.path.join(proc_root, name, "task")
                try:
                    tids = os.listdir(task_dir)
                except FileNotFoundError:
                    # The group went away right after the leader read: a
                    # departure, with the same successor concern as any other.
                    vanished = True
                    departed_this_pass = True
                    continue
                except OSError:
                    complete = False
                    unread_sticky = True
                    continue
                departed = unreadable = False
                for tid in tids:
                    if tid == name:
                        continue
                    try:
                        _collect(os.path.join(task_dir, tid, "mountinfo"))
                    except (FileNotFoundError, ProcessLookupError):
                        departed = True
                    except OSError as exc:
                        if exc.errno == errno.EINVAL:
                            continue  # a thread mid-exit: it holds no namespace
                        unreadable = True
                if unreadable:
                    complete = False
                    unread_sticky = True
                elif departed:
                    # A thread that exited between the ``tids`` snapshot and its
                    # read may have left a successor THREAD holding its
                    # namespace, which the outer re-listing (leaders only)
                    # cannot show -- so the GROUP is re-read on the next pass
                    # rather than this being either excused or made sticky.
                    seen.discard(name)
                    vanished = True
                    departed_this_pass = True
        if not vanished:
            # Every departure so far was followed by a re-listing, so what
            # remains unaccounted for is the still-present tasks that could not
            # be read (sticky) plus nothing from this pass's departures.
            _report()
            return pinned, complete
    # Still churning after the pass budget — coverage unproven, and the final
    # pass's departures had no re-listing to catch a successor.
    _report()
    return pinned, False


#: Wall-clock budget for ONE reclaim pass, keyed and legacy alike. A pile that
#: cannot be cleared inside it is left for the next pass: the sweep runs on the
#: maintenance executor, whose threads other housekeeping shares, and reclaim is
#: resumable by construction (each entry is decided independently, and nothing
#: depends on a pass finishing). Measured for scale: 939k directories took ~11s
#: on tmpfs, so this clears a normal backlog in one pass and spreads a
#: pathological one over a few, instead of occupying a worker for a minute.
_SWEEP_TIME_BUDGET_SECONDS = 10.0

#: How often the budget is consulted. A clock read per entry would be a
#: measurable share of the work at these counts; per batch is close enough for a
#: budget whose only job is to bound the pass.
_SWEEP_BUDGET_CHECK_EVERY = 4096


def _cleanup_stale_sandbox_mount_sources(*, roots: Sequence[str] | None = None) -> int:
    """Reclaim orphaned bind-mount sources staged by the namespace launcher.

    The launcher stages one empty dir per SENSITIVE_DIRS entry, one empty file
    per SENSITIVE_FILES entry, and (strict only) an SSH shadow dir holding a
    known-hosts copy, all named ``kirocrew_sb_<pid>_*`` on a tmpfs root. The
    kernel pins each source while its bind-mount lives, so the launcher cannot
    remove them and they orphan when the sandboxed process exits. Left alone
    they exhaust the runtime tmpfs (``/run/user/$UID``), at which point the
    systemd user manager cannot allocate transient scope units and every
    ``systemd-run --scope``-wrapped spawn fails.

    An entry becomes a reclaim candidate when its embedded pid is dead, or
    past ``_MOUNT_SOURCE_MAX_AGE_SECONDS`` (the backstop for a pid recycled
    onto an unrelated live process — routine on a ``pid_max=32768`` host).
    What a candidate's removal may touch is then decided by kind, because a
    bind source is NOT protected by the kernel against the sweeper: removing
    it succeeds from this namespace (mountinfo then shows ``//deleted``), a
    removed source DIRECTORY leaves the live mount's root inode ``S_DEAD`` so
    every create under the masked path fails from then on, and a non-empty
    dir's contents are visible inside any namespace still binding it:

    - Plain files: ``os.remove``. A file source's inode is held by the mount
      like an open descriptor, so the masked view is unaffected.
    - Dirs, empty or not: removed only when no readable mount namespace
      references the entry (:func:`_mount_pinned_source_names`) AND absence
      of a pin is positively established — by the scan proving it covered
      every namespace on the host, OR by it having read every task that
      could hold one (:class:`_PinScanCoverage`: this uid's, the overflow
      uid's and root's tasks, none unreadable, none departed between the
      final listing and its read). The second is what this fix adds. The
      host-wide flag also drops for another user's unreadable or departing
      task, which cannot be holding a source this uid staged (the sandboxed
      child keeps this uid with NO_NEW_PRIVS; only root can enter a foreign
      namespace), and requiring it alone is what stranded the directory
      class permanently on the hosts this sweep exists for (observed:
      929,540 dirs retained against 511 files reclaimed, the runtime tmpfs
      back at 100% of its inodes and every spawn failing again). A recycled
      pid reads live yet has no mount, so its entry is still reclaimed after
      the age backstop rather than stranded, while a genuine long-lived
      sandbox is pinned by its own process. A namespace no PROCESS holds has
      no mountinfo to report a pin — an fd- or bind-pinned namespace with
      zero members is out of scope, and the launcher never creates one — so
      the pin set cannot go stale in the deleting direction. The
      fresh-and-alive skip above stays load-bearing for the launcher's own
      staging window (after ``mkdtemp``, before ``mount``), when its entries
      are legitimately live and not yet pinned.

    Deliberately conservative about names: only the recognized
    ``kirocrew_sb_<pid>_`` shape with an ASCII positive pid is touched.
    Foreign ``tmp*`` names carry no liveness key and are left alone, as are
    ``kirocrew_sandbox_*`` launcher scripts, the ``kirocrew_sbprobe_*`` tmpfs
    probe, and prefix matches whose segment is not an ASCII positive decimal
    (an oversized all-digit segment IS the recognized shape — it probes
    OverflowError, reads stale, and is reclaimed). Some of these roots are
    world-writable, so a planted entry — including a deep tree built to make
    ``rmtree`` recurse — must fail toward "skip", never toward an exception
    that kills the sweep.

    Returns:
        Number of entries removed.
    """
    now = time.time()
    started = time.monotonic()
    examined = 0
    budget_spent = False
    if roots is None:
        roots = _mount_source_candidate_roots()
    # (pinned set, scan-was-complete) — built lazily, once, on the first
    # directory candidate (empty ones included: rmdir is gated too).
    pin_scan: tuple[set[str], bool] | None = None
    # Whether that scan read every task that could be a launcher descendant
    # (``_PinScanCoverage``) -- the narrower claim the gate accepts when the
    # host-wide flag is down for reasons that cannot involve a sandbox.
    coverage = _PinScanCoverage()
    removed = 0
    dirs_held_back = 0
    for root in roots:
        if budget_spent:
            break
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if not entry.startswith(_MOUNT_SOURCE_PREFIX):
                continue
            examined += 1
            if (
                examined % _SWEEP_BUDGET_CHECK_EVERY == 0
                and (time.monotonic() - started) > _SWEEP_TIME_BUDGET_SECONDS
            ):
                # Out of budget: stop cleanly and let the next pass continue.
                # Reclaim is resumable per entry, so a partial pass is progress,
                # never an inconsistent state.
                budget_spent = True
                break
            pid_str, sep, _rest = entry[len(_MOUNT_SOURCE_PREFIX) :].partition("_")
            pid = _parse_pid_segment(pid_str) if sep else None
            if pid is None:
                continue  # foreign / probe / planted names — no liveness key
            path = os.path.join(root, entry)
            try:
                mtime = os.lstat(path).st_mtime
            except OSError:
                continue
            over_age = (now - mtime) > _MOUNT_SOURCE_MAX_AGE_SECONDS
            # Liveness probe via the shim — NEVER raw os.kill(pid, 0), which
            # TERMINATES the target on Windows (platform_compat).
            try:
                alive = platform_compat.pid_exists(pid)
            except OverflowError:
                alive = False  # absurd pid digits from a corrupt name — stale
            if alive and not over_age:
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                # ANY dir removal — even rmdir of an empty one — S_DEADs a
                # live mount's root inode, so absence of a pin must be
                # POSITIVELY established: either the host-wide scan proved its
                # coverage, or it read every task that could be a launcher
                # descendant (``coverage.covered``). The second is what keeps
                # another user's unreadable or departing task from retaining
                # the class forever — it cannot be holding a source this uid
                # staged.
                if pin_scan is None:
                    pin_scan = _mount_pinned_source_names(coverage=coverage)
                pinned, scan_complete = pin_scan
                if entry in pinned or not (scan_complete or coverage.covered):
                    dirs_held_back += 1
                    continue
                try:
                    os.rmdir(path)
                    removed += 1
                    continue
                except OSError:
                    pass
                try:
                    shutil.rmtree(path, ignore_errors=True)
                except Exception:
                    continue  # e.g. a planted tree deep enough to exhaust recursion
                # ignore_errors swallows a partial failure — count only a
                # confirmed removal.
                if not os.path.lexists(path):
                    removed += 1
            else:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
    if dirs_held_back:
        # An always-closed pin scan is otherwise indistinguishable from a
        # working one while the dominant (directory) leak class re-accumulates
        # — surface it so an operator can tell retention from reclamation.
        scan_complete = pin_scan is not None and pin_scan[1]
        covered = scan_complete or coverage.covered
        # WARNING for the incomplete case, INFO for the benign pinned one.
        # Holding entries back because a live namespace binds them is normal
        # operation; holding them back on an unprovable scan is a FAULT — now
        # survivable, because a candidate whose staging group is gone is
        # reclaimed anyway, so what remains here is confined to entries whose
        # tree still looks alive. Coverage of every task that could be a
        # launcher descendant counts as proven for this purpose.
        (logger.info if covered else logger.warning)(
            "sandbox mount-source sweep: %d dir candidate(s) held back (%s)",
            dirs_held_back,
            (
                "pinned by a live mount namespace"
                if covered
                else "pin scan incomplete and descendant coverage unproven"
            ),
        )
    if budget_spent:
        logger.info(
            "sandbox mount-source sweep: paused at the %.0fs budget after %d entries "
            "(%d reclaimed this pass); the next pass resumes",
            _SWEEP_TIME_BUDGET_SECONDS,
            examined,
            removed,
        )
    return removed


#: Pre-#6268 builds staged their bind-mount sources with ``tempfile``'s DEFAULT
#: names, so those entries carry no pid and the pid-keyed sweep above cannot
#: reason about them at all. Every install that upgraded through that change
#: therefore carries a permanent pile — 1,836,596 entries measured on one host,
#: enough on its own to exhaust the runtime tmpfs's inodes and stop every
#: ``systemd-run --scope``-wrapped spawn. This is ``tempfile``'s exact shape:
#: the ``tmp`` prefix plus 8 characters of its own alphabet.
_LEGACY_MOUNT_SOURCE_RE = re.compile(r"^tmp[a-z0-9_]{8}$")

#: How many legacy-shaped candidates a root must hold before the legacy pass
#: touches ANY of them. An unkeyed name carries no provenance, so the pile IS
#: the provenance: no program's ordinary scratch use leaves dozens of empty,
#: 0o700, day-old ``tmp*`` directories in the session runtime dir, whereas the
#: leak this pass exists for left 1.8 million on one host. Below the threshold
#: every candidate is retained and the pass retires — there is no pile to heal.
_LEGACY_PILE_THRESHOLD = 64

#: Written once a legacy pass has completed, so the scan is not repeated for the
#: life of the install: no current build creates these names, so a completed
#: pass is final.
_LEGACY_RESIDUE_MARKER = ".legacy-mount-source-residue-swept"


def _launcher_tmpfs_roots() -> list[str]:
    """The root the legacy pass may walk: the session runtime dir alone.

    Deliberately narrower than :func:`_mount_source_candidate_roots`. The legacy
    sweep matches an unkeyed ``tmp*`` name, so it may only walk a root where
    that shape is far more likely ours than a stranger's. ``/run/user/$UID`` is
    the launcher's first pick, is scoped to this uid's login session (nothing
    keeps durable state there), and is where the observed pile lived.
    ``/dev/shm`` is NOT walked even though the launcher falls back to it: it is
    a host-wide tmpfs where any same-uid program's ``tempfile`` scratch
    legitimately lives, and an empty day-old scratch dir there can still be a
    live program's — a host whose launcher fell back to ``/dev/shm`` keeps the
    manual note in the issue instead. The shared system tempdir is excluded
    for the same reason, more so.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return []
    return [f"/run/user/{getuid()}"]


def _bound_source_basenames(
    proc_root: str = "/proc", *, coverage: _PinScanCoverage | None = None
) -> tuple[set[str], bool]:
    """Basenames of legacy-shaped bind sources named by any live mount, plus
    whether coverage was positively established.

    The same traversal as :func:`_mount_pinned_source_names` — zombie leaders
    consulted through ``task/``, the vanish re-listing, foreign-uid forgiveness
    — with the name predicate swapped for tempfile's shape, because the legacy
    residue carries no recognizable prefix. A first cut re-implemented the walk
    and got two things wrong that this delegation cannot: it filtered on the
    root field's DIRNAME, which is the source's path *within its own
    filesystem* (``/tmpab12cd34``, never ``/run/user/$UID/tmpab12cd34``) so the
    fence silently matched nothing; and it reported every ``EINVAL`` as a
    coverage gap, which on a real host with a couple of dozen zombie leaders
    made the pass permanently inert.

    Keyed by basename only, which is why no root is taken: a same-named entry
    on another filesystem shares the pin, which errs toward retention.
    """
    return _mount_pinned_source_names(
        proc_root,
        matcher=lambda name: bool(_LEGACY_MOUNT_SOURCE_RE.match(name)),
        coverage=coverage,
    )


def _cleanup_legacy_mount_source_residue() -> int:
    """One-shot reclaim of the pre-#6268, pid-less bind-mount source residue.

    An install that upgraded past #6268 gained a sweep that can never touch what
    the OLD build left behind, so the pile it inherited keeps the runtime tmpfs
    at its inode ceiling and every agent spawn keeps failing — an upgrade that
    ships the reclaim fix but not this leaves such a host exactly as broken as
    before. Runs from the same entry point as the keyed sweep, so the gateway's
    first cleanup pass after an update heals the host without an operator ever
    learning what ``Failed to start transient scope unit: No space left on
    device`` meant.

    An unkeyed name cannot be PROVEN to be ours, so every fence here is about
    keeping a stranger's entry rather than reclaiming ours:

    - only on the session runtime tmpfs the launcher picks first
      (:func:`_launcher_tmpfs_roots`), never ``/dev/shm`` or the shared system
      tempdir, where another same-uid program's ``tempfile`` scratch lives;
    - only ``tempfile``'s exact shape (:data:`_LEGACY_MOUNT_SOURCE_RE`);
    - only DIRECTORIES owned by THIS uid with the exact mode ``mkdtemp``
      creates (0o700) — a hand-made or umask-shaped entry is not one of
      these, and the old build's ``mkstemp`` file sources are left alone: an
      unlinked file another program still holds open loses what it writes
      next, which no fence here can rule out, while an empty dir holds
      nothing to lose;
    - only past ``_MOUNT_SOURCE_MAX_AGE_SECONDS``, which every real member of
      this class is by construction (no build has created the shape since
      #6268) while a live program's scratch dir usually is not;
    - only when no live mount names the entry as its source, and only when
      that absence was POSITIVELY established (:func:`_bound_source_basenames`
      complete, or every possible holder read — the same two claims as the
      keyed dir gate, since ``/run/user/$UID`` is reachable by this uid and
      root alone);
    - only once a root shows the PILE this pass exists for
      (:data:`_LEGACY_PILE_THRESHOLD` candidates passing every fence above):
      a stray scratch dir or two never trips it and is retained outright,
      while the leak class arrives by the hundred thousand;
    - dirs go through ``os.rmdir``, which REFUSES a non-empty directory: that
      is the emptiness fence, so a populated stranger's dir survives without a
      listing, and so does the legacy SSH shadow dir (it holds a known-hosts
      copy) — left for the human note in the issue rather than removed here;
    - the one-shot marker is stamped only by a pass that reached the end AND
      found nothing retained for age alone, so a host upgrading within a day
      of its last old-build spawn does not retire the pass on that cohort.

    Returns:
        Number of entries removed.
    """
    marker = config_dir() / _LEGACY_RESIDUE_MARKER
    try:
        if marker.exists():
            return 0
    except OSError:
        return 0
    roots = _launcher_tmpfs_roots()
    coverage = _PinScanCoverage()
    bound, complete = _bound_source_basenames(coverage=coverage)
    if not (complete or coverage.covered):
        # Absence-of-bind not established — retry on the next sweep rather than
        # remove on the strength of an unproven scan, and do NOT stamp the
        # marker, or one bad scan would retire the pass forever. The same two
        # claims the keyed dir gate accepts: ``/run/user/$UID`` is 0o700 and
        # this uid's, so its entries are reachable by this uid and root alone,
        # which is exactly what ``covered`` vouches for. Say so at WARNING,
        # like the keyed sweep's held-back report: on a host whose /proc never
        # settles this pass stays inert every tick, and a silent return would
        # be the same invisible-at-default-log-level failure the keyed sweep's
        # diagnostic exists to end.
        logger.warning(
            "sandbox mount-source sweep: legacy pass retained everything — bind "
            "coverage of /proc could not be established (pin scan incomplete, "
            "holder coverage unproven); the pass retries on the next tick"
        )
        return 0
    now = time.time()
    started = time.monotonic()
    examined = 0
    budget_spent = False
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if getuid is not None else None
    removed = 0

    young_retained = False

    def _reclaim(path: str) -> bool:
        try:
            os.rmdir(path)  # refuses a non-empty dir by design
        except OSError:
            return False
        return True

    for root in roots:
        if budget_spent:
            break
        try:
            entries = os.scandir(root)
        except OSError:
            continue
        # Candidates are buffered until the root proves it holds the pile;
        # below the threshold nothing in the buffer is touched. Once it is
        # reached the buffer is drained and reclaim streams from then on.
        pending: list[str] = []
        engaged = False
        with entries:
            for entry in entries:
                if not _LEGACY_MOUNT_SOURCE_RE.match(entry.name) or entry.name in bound:
                    continue
                examined += 1
                if (
                    examined % _SWEEP_BUDGET_CHECK_EVERY == 0
                    and (time.monotonic() - started) > _SWEEP_TIME_BUDGET_SECONDS
                ):
                    budget_spent = True
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if own_uid is not None and info.st_uid != own_uid:
                    continue
                # Directories only. The old build staged a mkstemp FILE per
                # masked file too, but an unlinked file another program still
                # holds open loses whatever it writes next, and nothing here
                # can tell such a file from ours; an empty directory holds no
                # data to lose and a stranger's mkdtemp dir gets ENOENT on its
                # next create, an error rather than silent loss.
                if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                    continue
                if (now - info.st_mtime) <= _MOUNT_SOURCE_MAX_AGE_SECONDS:
                    young_retained = True  # ages into candidacy on a later pass
                    continue
                if engaged:
                    removed += _reclaim(entry.path)
                    continue
                pending.append(entry.path)
                if len(pending) >= _LEGACY_PILE_THRESHOLD:
                    engaged = True
                    for queued in pending:
                        removed += _reclaim(queued)
                    pending = []
        if pending and not engaged:
            logger.info(
                "sandbox mount-source sweep: %d legacy-shaped entries under %s are below "
                "the pile threshold (%d); retained as not provably ours",
                len(pending),
                root,
                _LEGACY_PILE_THRESHOLD,
            )
    if budget_spent:
        logger.info(
            "sandbox mount-source sweep: legacy pass paused at the %.0fs budget after "
            "%d entries (%d reclaimed); the next pass resumes",
            _SWEEP_TIME_BUDGET_SECONDS,
            examined,
            removed,
        )
    elif young_retained:
        logger.info(
            "sandbox mount-source sweep: legacy pass complete but not retired — "
            "legacy-shaped entries under the age fence remain; the next pass "
            "that finds none stamps the marker"
        )
    else:
        # Stamp only a pass that reached the end of the residue AND left no
        # cohort behind for age, or a budget-truncated walk's remainder — or a
        # host upgrading within a day of its last old-build spawn — would be
        # retired unswept.
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            # Create-only and never through a symlink: a planted link at this
            # path must fail the stamp (the pass just repeats) rather than
            # have the gateway write wherever it points.
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, f"{int(now)} removed={removed}\n".encode())
            finally:
                os.close(fd)
        except OSError:
            # An unstampable marker only costs a repeat pass, which is idempotent.
            logger.debug("legacy mount-source sweep: could not stamp %s", marker, exc_info=True)
    if removed:
        logger.info(
            "sandbox mount-source sweep: reclaimed %d pre-prefix legacy entries "
            "(one-shot; these carry no pid and no earlier build could reclaim them)",
            removed,
        )
    return removed


def _cleanup_retired_acp_snapshot_dir() -> int:
    """Reclaim `<config_dir>/run/kiro-cli-snapshots` from before the in-place launch.

    KiroCrew used to copy the whole kiro-cli binary here per ACP spawn generation
    and exec the copy. The CLI is now launched in place, so nothing writes this
    tree — but an install upgrading past that change keeps every orphaned copy
    forever (observed: 196 MB in two ~100 MB copies). Nothing else reclaims them:
    the sweep above only matches `kirocrew_sandbox_*` FILES in this same dir, and
    the tree sits on `security._SENSITIVE_HOME_DIRS`, so the agent cannot delete
    it either — on request or otherwise. Best-effort and idempotent; returns 1
    when a tree was removed so the periodic sweep logs it.
    """

    retired = config_dir() / "run" / "kiro-cli-snapshots"
    if not retired.is_dir():
        return 0
    shutil.rmtree(retired, ignore_errors=True)
    # ignore_errors swallows partial failures, so report only a real removal.
    return 0 if retired.exists() else 1


# ── Public API ──

_backend: str | None = None  # "namespace", "sandbox-exec", "none"


def _allow_no_isolation() -> bool:
    """Whether the operator has explicitly opted into running the agent
    subprocess without OS-level credential isolation.

    Read lazily from config to avoid an import cycle with the config loader
    (sandbox.py is a low-level dependency of much of the codebase).
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_no_isolation", False))
    except Exception:
        return False


def _allow_unsandboxed_exec() -> bool:
    """Whether the operator has explicitly opted into allowing execution
    without ANY sandbox backend (fail-open behavior).

    When False (default), wrap_argv will RAISE instead of returning unmodified
    argv when no sandbox backend is available. This is the fail-closed behavior
    required by a penetration-test finding.

    Read lazily from config to avoid an import cycle with the config loader.
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_unsandboxed_exec", False))
    except Exception:
        return False


# Fallback tier for configured_sandbox_mode() when the config cannot be read.
# "auto" (= standard), matching wrap_argv's own default: an unreadable config
# must not be a way to obtain a LOOSER sandbox than the operator configured.
_SANDBOX_MODE_FALLBACK = "auto"


def configured_sandbox_mode() -> str:
    """The operator's ``agent.sandbox`` tier, for one-shot kiro-cli spawns.

    ``wrap_argv``'s ``mode`` parameter defaults to ``"auto"``, which coincides
    with the shipped ``agent.sandbox`` default but ignores what the operator
    actually configured. Where ``agent.sandbox`` is an explicit ``"off"`` —
    isolation deferred to kiro-cli's own internal sandbox, which cannot nest
    inside Kiro Crew's (macOS Seatbelt returns EPERM) — a spawn that takes the
    parameter default asks for a STRICTER tier than the operator configured. On
    a backend-less host an unclassified spawn then fail-closes while a delegated
    Kiro chat path can run; the reviewed Windows Kiro sites carry explicit
    classification, but keeping the configured tier remains the cross-platform rule.

    Passing the configured value is what keeps a one-shot read from being
    stricter than the long-lived session it accompanies; it can never make it
    looser, because both resolve the same key.

    The interactive ACP spawns already thread the configured mode through their
    ``sandbox_mode`` constructor argument. The one-shot ``kiro-cli`` reads
    (``--list-models``, ``whoami``, the ``/usage`` scrape) have no such plumbing,
    so they call this instead of relying on the parameter default. Use it for a
    spawn of the SAME binary under the SAME posture as chat; it is deliberately
    not for spawns that pin their own tier on purpose (the prerequisite probes'
    ``strict``, the credential-free registry clones).

    Read lazily, like the two opt-in predicates above, to avoid an import cycle
    with the config loader. Falls back to :data:`_SANDBOX_MODE_FALLBACK` so an
    unreadable config cannot silently loosen isolation. Governance still clamps
    the result UP inside ``wrap_argv`` (:func:`_clamp_sandbox_mode`), so an
    enterprise ``sandbox.min_level`` floor overrides this value as it does any
    other caller-supplied mode.
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return str(getattr(KiroCrewConfig.load().agent, "sandbox", _SANDBOX_MODE_FALLBACK))
    except Exception:
        logger.warning(
            "Could not read agent.sandbox; using %r for this spawn", _SANDBOX_MODE_FALLBACK
        )
        return _SANDBOX_MODE_FALLBACK


# The single environment marker that proves this process is already INSIDE a
# KiroCrew namespace sandbox. Deny-by-default: the gate keys ONLY on the
# explicit, single-purpose ``KIROCREW_SANDBOX_ACTIVE``, which is exported at
# exactly one site — the namespace launcher main() (see the export beside
# ``KIROCREW_HOST_PID``). We deliberately do NOT key on ``KIROCREW_HOST_PID``:
# it is dual-purpose session-identity plumbing, and gating a security-relevant
# passthrough on a variable set for other reasons is a latent bypass. Since the
# launcher sets ``KIROCREW_SANDBOX_ACTIVE`` at the same site, no fallback marker
# is needed. No unsandboxed code path sets this marker.
#
# Two sites set it, each immediately after applying that platform's credential-env
# scrub: the Linux namespace launcher's ``main()`` (after its ``ENV_PREFIXES``
# loop) and the macOS ``env`` prefix built by :func:`sandbox_exec_argv` (after its
# ``env -u`` flags, derived from the SAME prefix lists — see
# :func:`_sandbox_env_unset_args`). A marked process therefore always has an
# environment KiroCrew already sanitised, which is what makes the passthrough
# below safe for callers that use ``wrap_argv`` directly rather than
# ``sandboxed_spawn_argv``.
_IN_SANDBOX_MARKER = "KIROCREW_SANDBOX_ACTIVE"

# Companion to ``_IN_SANDBOX_MARKER``: records WHICH tier the outer sandbox was
# built at (``standard``/``cc``/``strict``), exported at the same two launcher
# sites and with the same non-droppable placement (after each platform's env
# scrub / ``-u`` flags). The marker alone proves "a Kiro Crew sandbox is active"
# but not its tier; without this record the nested passthrough is tier-blind —
# an in-sandbox caller requesting ``strict`` under a ``standard`` outer sandbox
# silently runs at ``standard``. The passthrough compares this against the
# requested tier so a downgrade is audited and warned about rather than
# invisible. Absent for trees launched by an older build — readers treat that
# as ``unknown``.
_IN_SANDBOX_LEVEL_VAR = "KIROCREW_SANDBOX_LEVEL"

# Confinement ordering for downgrade detection: a request is a downgrade only
# when its ordinal exceeds the active tier's. ``unknown`` (absent/unrecognized
# level var) is deliberately NOT in this table: it carries no ordinal claim, so
# no downgrade can be *proven* against it.
_TIER_ORDINALS: dict[str, int] = {"standard": 1, "cc": 2, "strict": 3}


def _mode_to_level(mode: str) -> str:
    """Map a ``wrap_argv`` mode to the sandbox tier it resolves to.

    ``"auto"``/``"standard"`` (and anything unrecognized) resolve to
    ``standard``; ``"cc"`` and ``"strict"`` map to themselves. Shared by the
    nested-passthrough tier comparison and the backend-wrap level resolution so
    the two sites can never diverge.
    """
    if mode == "strict":
        return "strict"
    if mode == "cc":
        return "cc"
    return "standard"


def _bundled_cli_invocation() -> str | None:
    """Absolute, shell-quoted path to the CLI this process was started from.

    The AppImage persona is defined by the install guide as needing "no Python,
    pip, npm, or Node" (docs/guides/install.md), so there is usually no
    ``kirocrew`` on their PATH at all — the CLI is bundled INSIDE the AppImage.
    Printing the bare command would hand exactly the affected user a
    ``command not found`` and leave them with only the opt-out, which is the
    opposite of the point.

    ``shutil.which("kirocrew")`` is deliberately NOT trusted as evidence here:
    this string is generated inside the gateway process, which inherits the
    AppImage's own PATH, but it is pasted into the user's shell, which does not.
    A hit would prove the bundle can find its own CLI, not that the user can.

    Returns None when the path cannot be established, so the caller can fall back
    to the bare name rather than print something invented.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return None
    try:
        resolved = os.path.realpath(argv0)
    except OSError:
        return None
    name = os.path.basename(resolved).lower()
    if not name.startswith("kirocrew") or not os.path.isfile(resolved):
        return None
    return shlex.quote(resolved)


def _apparmor_userns_restricted() -> bool:
    """True when this kernel is the Ubuntu AppArmor userns-restriction case.

    Read straight from /proc rather than importing
    :mod:`kiro_crew.service.apparmor`: ``sandbox`` is a low-level dependency of
    config loading, and pulling the service package in here would create an
    import cycle. One file read, no subprocess.
    """
    try:
        with open(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns", encoding="utf-8"
        ) as handle:
            return handle.read().strip() == "1"
    except OSError:
        return False


def _no_backend_guidance() -> str:
    """Remedy text for a genuine no-backend host, specific to WHY it has none.

    The generic "install a sandbox backend, or opt out" advice is actively
    unhelpful on the single most common affected host: stock Ubuntu 23.10+, where
    a backend exists and is one AppArmor profile away from working. Worse, the
    only concrete thing that text suggests is the opt-out, which turns off the
    isolation the message exists to protect.

    The remedy differs by HOW Kiro Crew was launched, so it is named per shape:

    * AppImage / desktop app — nothing applies a profile to a directly launched
      binary, so attach one to it (``kirocrew sandbox install-profile``).
    * anything else on such a host — the profile must be applied by systemd
      (``kirocrew service install``), because the only executable in a foreground
      launch is a shared interpreter and attaching there would grant unprivileged
      userns to every Python process on the machine.

    Deliberately does NOT tell the user to set the sysctl to 0: that trades a
    kernel-wide protection for one app's need, and the per-application profile
    exists so they do not have to.

    The ``sandbox_allow_unsandboxed_exec`` opt-out is still named in every case,
    because it is the documented escape hatch and withholding it would leave a
    stuck user with no way out. What changes is the ORDER: on a host where the
    sandbox is one profile away from working, the profile is the remedy and the
    opt-out is the last resort, where the previous text offered the opt-out as
    the only concrete suggestion.
    """
    optout = (
        "As a last resort, agent.sandbox_allow_unsandboxed_exec=true in "
        "~/.kiro/crew/config.json allows unsandboxed execution, but that removes "
        "the isolation this check exists to protect. "
    )
    if sys.platform.startswith("linux") and _apparmor_userns_restricted():
        base = (
            "This host restricts unprivileged user namespaces via AppArmor "
            "(kernel.apparmor_restrict_unprivileged_userns=1, the default on "
            "Ubuntu 23.10+ and derivatives). A sandbox backend DOES exist here — "
            "it needs a per-application AppArmor profile granting 'userns', "
            "exactly as stock Ubuntu already ships for chrome, brave, 1password "
            "and Discord. "
        )
        appimage = os.environ.get("APPIMAGE", "").strip()
        if appimage:
            # Name the CLI by ABSOLUTE PATH, not as `kirocrew`. An AppImage user
            # has no kirocrew on PATH (see _bundled_cli_invocation), so the bare
            # command would fail for exactly the person reading this. The bundled
            # path is valid while the app is running, which is when they will run
            # it, and it is the same binary the desktop app already spawns.
            cli = _bundled_cli_invocation() or "kirocrew"
            where = (
                " (that path is inside the running app, so run it while Kiro Crew " "is open)"
                if cli != "kirocrew"
                else ""
            )
            # shlex.quote, not bare interpolation: this string is printed for the
            # user to paste into a shell, and a filename is attacker-influenced in
            # the cases that matter (a downloaded or unpacked AppImage). An
            # AppImage named `Kiro-Crew-$(...).AppImage` would otherwise have its
            # substitution executed by the paste, turning a diagnostic into a
            # command-injection vector. Mirrors the quoting the desktop side
            # already does in website/electron/sandbox-profile.js.
            return (
                base
                + (
                    "This is an AppImage launch, which no profile is attached to yet. "
                    "Run this in a terminal (it needs sudo, so it cannot be done from "
                    f"the app): {cli} sandbox install-profile --path "
                    f"{shlex.quote(appimage)}{where} — then restart the app. Do NOT "
                    "set the sysctl to 0: that disables a kernel-wide protection for "
                    "every application on the machine. "
                )
                + optout
            )
        return (
            base
            + (
                "Run `kirocrew service install` to install the profile and have "
                "systemd apply it to the gateway unit. Do NOT set the sysctl to 0: "
                "that disables a kernel-wide protection for every application on the "
                "machine. "
            )
            + optout
        )
    return (
        "If this host genuinely lacks a sandbox backend, set "
        "agent.sandbox_allow_unsandboxed_exec=true in "
        "~/.kiro/crew/config.json to explicitly allow unsandboxed "
        "execution, or install a supported sandbox backend "
        "(Linux user namespaces, or macOS sandbox-exec). "
    )


def _classify_unavailable(transient: bool) -> str:
    """Name why no backend is available, given an already-read transient flag.

    One implementation of the rule shared by ``wrap_argv``'s
    ``SandboxUnavailableError.kind`` and the public :func:`unavailable_kind`, so
    the two can never drift into disagreeing about the same host.
    """
    if transient:
        return "transient"
    return "foreign_sandbox" if _inside_macos_sandbox() else "no_backend"


def unavailable_kind() -> str:
    """Classify a backend-less host for callers that offer a PERSISTENT opt-in.

    Returns ``""`` when a backend IS available, otherwise the same value
    ``SandboxUnavailableError.kind`` would carry.

    A caller that writes ``sandbox_allow_unsandboxed_exec`` to disk must act
    ONLY on ``"no_backend"``. ``detect_backend()`` alone is not enough: it also
    reports ``"none"`` for a momentary fork/resource failure, which self-heals on
    the next spawn and must never buy a permanent bypass — and for a foreign
    outer sandbox, where the host's own sandbox is fine and the remedy is to hand
    isolation back to Kiro Crew rather than disable it.
    """
    if detect_backend() != "none":
        return ""
    transient, _reason, _remedy = _last_unshare_failure or (False, "none", "")
    return _classify_unavailable(transient)


def _inside_kirocrew_sandbox() -> bool:
    """True when this process already runs inside a KiroCrew OS sandbox.

    Nested sandboxing is impossible on both backends: the Linux launcher's
    seccomp filter denies ``unshare``/``setns`` precisely so the sandboxed tree
    cannot manipulate namespaces, and macOS Seatbelt refuses ``sandbox_apply``
    with EPERM from inside an existing sandbox — even under an ``(allow default)``
    outer profile. An in-sandbox wrap_argv call must therefore pass through rather
    than fail closed — the outer sandbox still confines every descendant, so this
    is NOT the fail-open path. Failing closed here bricked every in-sandbox MCP
    spawn with unshare EPERM (the probe error was raised on every ctx.call_tool
    and silently swallowed by the caller), and on macOS it bricked every
    app-backend spawn (Dev Fleet's ``git worktree list``, Files' ``git
    status``/search) plus ~40 MCP probes at gateway boot.

    Detection is deny-by-default: gated solely on the explicit, launcher-only
    ``KIROCREW_SANDBOX_ACTIVE`` marker (see ``_IN_SANDBOX_MARKER``).
    """
    return bool(os.environ.get(_IN_SANDBOX_MARKER))


@functools.lru_cache(maxsize=1)
def _macos_sandbox_state() -> bool | None:
    """Kernel verdict on whether THIS macOS process is Seatbelt-confined.

    ``True``
        Confined — ``sandbox_check(pid, NULL, SANDBOX_FILTER_NONE)`` returned 1.
    ``False``
        Definitely not confined — the kernel answered 0.
    ``None``
        Unanswerable — non-darwin, or the symbol could not be loaded or called.

    Three states rather than a bool because the two negatives carry opposite
    security meanings. A definite ``False`` alongside a present
    ``KIROCREW_SANDBOX_ACTIVE`` marker proves the marker was forged or inherited
    into an unsandboxed process, and must NOT grant a passthrough. An
    unanswerable probe says nothing at all, and must not retroactively invalidate
    a marker the Linux path honours unconditionally.

    Cached for the process lifetime — a process cannot leave its sandbox.
    """
    if sys.platform != "darwin":
        return None
    try:
        libpath = ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib"
        lib = ctypes.CDLL(libpath, use_errno=True)
        check = lib.sandbox_check
        # sandbox_check(pid_t, const char *operation, enum sandbox_filter_type, ...)
        # A NULL operation with SANDBOX_FILTER_NONE (0) asks the generic
        # "is this pid sandboxed at all?" question. The path-scoped form that
        # could identify WHICH paths the profile denies is variadic and returns
        # -1 through ctypes on arm64, so it is not usable here.
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        rc = check(os.getpid(), None, 0)
        if rc < 0:
            return None
        return rc == 1
    except Exception as exc:  # missing symbol, ABI change, restricted dyld, ...
        logger.debug("sandbox_check unavailable (%s); sandbox state unknown", exc)
        return None


def _inside_macos_sandbox() -> bool:
    """True when the kernel confirms a Seatbelt sandbox confines this process.

    Used to tell a *nesting* EPERM apart from a genuinely missing backend, so the
    fail-closed error names the real cause. Without it,
    :func:`_probe_sandbox_exec`'s EPERM is reported as "this host has no sandbox
    backend" — on a host whose ``sandbox-exec`` works perfectly when not nested.

    Unlike :data:`_IN_SANDBOX_MARKER` this is OS-authoritative: it cannot be
    spoofed through an agent-influenced environment, and it sees sandboxes
    KiroCrew did NOT create — notably kiro-cli >= 2.13's own internal seatbelt
    (see ``_KIRO_INTERNAL_SETTINGS_PATH``), or an operator-wrapped gateway. It is
    therefore NOT sufficient on its own to grant a passthrough: it proves *some*
    sandbox is active, not that KiroCrew built it or scrubbed the environment.
    The marker supplies that half; see :func:`wrap_argv`.
    """
    return _macos_sandbox_state() is True


def agent_confinement_evidence() -> str | None:
    """Evidence that THIS process runs under agent-shell confinement, or ``None``.

    The runtime half of the operator-attestation check used by authorization
    gates whose input must come from a human at a host terminal and never from
    an agent-spawned process (the app dev-mode out-of-install confirmation,
    #6907). Returns a short human-readable reason when there is ANY evidence of
    confinement, ``None`` when there is none.

    Deny-direction only, and deliberately so: each signal is unforgeable *in
    the direction of refusal*. A present ``KIROCREW_SANDBOX_ACTIVE`` marker in
    a genuinely unsandboxed process only over-refuses (and proves the marker
    was forged or leaked — see :func:`_macos_sandbox_state`); a kernel verdict
    of "Seatbelt-confined" for a non-KiroCrew sandbox (kiro-cli's internal
    seatbelt, an operator-wrapped process) still means "not a bare operator
    terminal", which is the question being asked. The converse is NOT
    guaranteed: a ``None`` does not prove a human — an agent can strip the
    marker with ``env -u``, and Linux offers no cheap kernel verdict — which
    is why callers pair this with a structural probe of a sealed artifact
    (``_CREW_READONLY_LEAVES``) that the OS sandbox denies regardless of the
    environment. Never use this function to *grant* anything.
    """
    if os.environ.get(_IN_SANDBOX_MARKER):
        return f"the {_IN_SANDBOX_MARKER} marker is set (agent-sandboxed process)"
    if _macos_sandbox_state() is True:
        return "the kernel reports this process is Seatbelt-confined"
    return None


def _warn_no_isolation(mode: str) -> None:
    """Loudly surface that the agent subprocess is running WITHOUT OS-level
    isolation, so the fallback is never silent.

    When no sandbox backend is available the credential paths (``~/.aws``,
    ``~/.ssh``, ...) are visible to the (untrusted) agent subprocess and only
    the bypassable app-level ``security.py`` checks remain. This is a real
    degradation of the security posture, so it is logged as a WARNING unless
    the operator has explicitly acknowledged it via
    ``agent.sandbox_allow_no_isolation``. Emitted once per process.
    """
    if getattr(wrap_argv, "_warned", False):
        return
    wrap_argv._warned = True  # type: ignore[attr-defined]
    if _allow_no_isolation():
        logger.info(
            "OS-level sandbox unavailable (mode=%s); running WITHOUT credential "
            "isolation. Operator opted in via agent.sandbox_allow_no_isolation; "
            "app-level checks are the only remaining boundary.",
            mode,
        )
        return
    logger.warning(
        "SECURITY: no OS-level sandbox backend is available on this host "
        "(mode=%s), so the agent subprocess runs WITHOUT credential isolation — "
        "~/.aws, ~/.ssh and other secrets are readable by it and only the "
        "bypassable app-level security.py checks remain. Install a supported "
        "sandbox (Linux user namespaces, or macOS < 26 sandbox-exec), or set "
        "agent.sandbox_allow_no_isolation=true in ~/.kiro/crew/config.json to "
        "acknowledge the risk and silence this warning.",
        mode,
    )


def _command_log_label(argv: list[str]) -> str:
    """Return a fixed, non-sensitive executable class for diagnostics.

    ``wrap_argv`` is a generic boundary: later argv elements routinely contain
    user-controlled paths, URLs, and occasionally transport capabilities. Static
    analysis also correctly treats a list element as able to reach any other
    element. Never send a value taken from that container to a log or SEL event,
    even when the runtime expression selects ``argv[0]``. The fixed labels retain
    enough operational signal without exposing executable paths or arguments.
    """

    if not argv:
        return "unknown"
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    if name == "git":
        return "git"
    if name in {"python", "python3", "pythonw", "pythonw3"}:
        return "python"
    if name in {"node", "npm", "npx"}:
        return "node"
    if name in {"kiro", "kiro-cli", "kirocrew"}:
        return "kiro"
    if name in {"bash", "sh", "zsh", "cmd", "powershell", "pwsh"}:
        return "shell"
    if name in {"env", "bwrap", "sandbox-exec", "systemd-run"}:
        return "sandbox-helper"
    return "other"


def _warn_mode_off_unconfined(argv: list[str], is_kiro_spawn: bool) -> None:
    """Emit a once-per-process SECURITY warning when mode='off' results in
    no OS-level isolation and no verified delegation.

    This covers the gap where the documented mutual-exclusion invariant (above
    ``_KIRO_INTERNAL_SETTINGS_PATH``) is violated by an explicit mode='off'
    config without the kiro-cli delegation being active.
    """
    # Honour the same acknowledgment as _warn_no_isolation (SEC-009 opt-in).
    if _allow_no_isolation():
        if not getattr(_warn_mode_off_unconfined, "_info_logged", False):
            _warn_mode_off_unconfined._info_logged = True  # type: ignore[attr-defined]
            logger.info(
                "agent.sandbox='off' with no active delegation; operator opted "
                "in via sandbox_allow_no_isolation. Command: %s",
                _command_log_label(argv),
            )
        return

    # Per-branch latch so a non-kiro spawn doesn't suppress the kiro-spawn warning.
    _warned_set: set = getattr(_warn_mode_off_unconfined, "_warned_set", set())

    if is_kiro_spawn and sys.platform == "darwin":
        if "darwin_kiro" in _warned_set:
            return
        _warned_set.add("darwin_kiro")
        logger.warning(
            "SECURITY: agent.sandbox='off' but kiro-cli's internal sandbox is "
            "NOT enabled (~/.kiro/settings/amazon-internal.json). Both isolation "
            "layers are inactive — ~/.aws, ~/.ssh and other secrets are readable "
            "by the agent subprocess and only the bypassable app-level "
            "security.py checks remain. Set agent.sandbox='auto' or enable "
            "kiro-cli's internal sandbox to restore OS-level confinement. "
            "Command: %s",
            _command_log_label(argv),
        )
    elif sys.platform.startswith("linux"):
        if "linux" in _warned_set:
            return
        _warned_set.add("linux")
        logger.warning(
            "SECURITY: agent.sandbox='off' on Linux — there is no kiro-cli "
            "delegation mechanism on this platform, so the agent subprocess "
            "runs with NO OS-level confinement. ~/.aws, ~/.ssh and other "
            "secrets are readable by it and only the bypassable app-level "
            "security.py checks remain. Set agent.sandbox='auto' to engage "
            "namespace isolation. Command: %s",
            _command_log_label(argv),
        )
    elif sys.platform == "win32":
        if "win32" in _warned_set:
            return
        _warned_set.add("win32")
        logger.warning(
            "SECURITY: agent.sandbox='off' on Windows — no OS-level sandbox "
            "backend exists on this platform. The agent subprocess runs with "
            "full filesystem access. Command: %s",
            _command_log_label(argv),
        )
    else:
        if "other" in _warned_set:
            return
        _warned_set.add("other")
        logger.warning(
            "SECURITY: agent.sandbox='off' for a non-kiro-cli subprocess — "
            "running without OS-level confinement. Set agent.sandbox='auto' "
            "to engage seatbelt isolation. Command: %s",
            _command_log_label(argv),
        )

    _warn_mode_off_unconfined._warned_set = _warned_set  # type: ignore[attr-defined]


def _warn_first_party_unconfined_once(argv: list[str]) -> None:
    """One-shot loud SECURITY warning for the first-party no-backend carve-out.

    Per-process sentinel, mirroring :func:`_warn_mode_off_unconfined`'s latch
    style: the trigger is the HOST having no backend, so without the latch every
    managed MCP probe would repeat the same paragraph on every discovery cycle.
    """
    if getattr(_warn_first_party_unconfined_once, "_warned", False):
        return
    _warn_first_party_unconfined_once._warned = True  # type: ignore[attr-defined]
    logger.warning(
        "SECURITY: no OS-level sandbox backend on this host — spawning a "
        "first-party fixed-argv Kiro Crew helper UNCONFINED (its full command "
        "line is derived inside this package with no agent, repo, or "
        "user-config input; the credential environment is scrubbed). "
        "Hostile-input spawn paths are unaffected: they keep failing closed "
        "and still require agent.sandbox_allow_unsandboxed_exec=true. "
        "Command: %s",
        _command_log_label(argv),
    )


def _first_party_no_backend_passthrough(
    argv: list[str], sandbox_level: str, strip_python_env: bool
) -> tuple[list[str], str | None]:
    """Allowed path of the first-party carve-out in :func:`wrap_argv`.

    Reached only when the caller passed ``first_party_fixed_argv=True``, the
    backend unavailability class is ``no_backend``, and no governance
    ``sandbox.min_level`` floor is active (all checked by the caller). Applies
    the same env scrub as the other unconfined-but-deliberate paths, warns
    loudly once per process, and SEL-audits with a DISTINCT third outcome:
    ``unconfined`` — deliberately neither ``denied`` (nothing was refused) nor
    the nested-passthrough ``allowed`` (nothing confines this spawn).

    SEL failure here is log-and-proceed, matching the ``mode="off"`` delegation
    precedent: the spawn is first-party with a package-derived argv, and the
    alternative is bricking built-in tooling on audit hiccups.

    Deliberately NOT ``critical=True``: unlike the fail-closed ``denied`` and
    nested-passthrough ``allowed`` audits — rare, one-per-condition events —
    this fires for every managed-server probe on every discovery cycle of a
    backend-less host, and the critical path drains + flushes SYNCHRONOUSLY on
    the caller's thread, which here is the gateway event loop (async
    ``probe_server``). A best-effort async write keeps the loop responsive; the
    tamper-evident record still lands via the background writer.
    """
    _warn_first_party_unconfined_once(argv)
    try:
        from kiro_crew.sel import sel  # circular import: sandbox is low-level

        sel().log_tool_invocation(
            session_key="sandbox",
            agent="system",
            source="sandbox.wrap_argv",
            tool_name=_command_log_label(argv),
            tool_kind="subprocess",
            outcome="unconfined",
            resources="first-party fixed argv, no sandbox backend (issue #1563 carve-out)",
        )
    except Exception:
        logger.warning(
            "SEL audit failed for first-party unconfined spawn — proceeding "
            "unaudited: the argv is package-derived and denying the spawn "
            "would brick built-in tooling whenever SEL hiccups (matches the "
            "mode=off delegation posture). Command: %s",
            _command_log_label(argv),
            exc_info=True,
        )
    # Same env scrub as the seatbelt / delegation paths, via the trusted
    # absolute-path ``env`` binary (never a PATH-resolved shim). Where no such
    # binary exists — Windows, the main no-backend host — the argv-level scrub
    # cannot run; that is acceptable ONLY because every ratchet-allowlisted
    # caller routes through ``sandboxed_spawn_argv``, whose ``scrub_env`` drops
    # a superset of these keys from the child environment it returns.
    scrub_keys = _sandbox_env_scrub_keys(sandbox_level, strip_python_env)
    if scrub_keys:
        env_argv = _unset_env_argv(tuple(scrub_keys))
        if env_argv is not None:
            return [*env_argv, *argv], None
        logger.warning(
            "first-party unconfined spawn: no trusted `env` binary for the "
            "argv-level scrub; relying on the chokepoint's scrub_env for the "
            "child environment"
        )
    return list(argv), None


def detect_backend(config_mode: str = "auto") -> str:
    """Detect the best available sandbox backend.

    Cache policy (a single transient fork failure must not poison the cache and
    fail-close every spawn until restart):

    - A positive result (``"namespace"``/``"sandbox-exec"``) is cached for the
      process lifetime — kernel capability does not change while running.
    - ``"none"`` is cached ONLY when the userns probe failure looks permanent
      (kernel refuses user namespaces: EPERM/EINVAL/ENOSYS). A transient
      resource failure (fork EAGAIN, EMFILE, ...) is never cached — the next
      spawn re-probes and self-heals.
    - ``config_mode="off"`` short-circuits to ``"none"`` without probing and
      without touching the cache. All other modes share one cache entry:
      backend capability is mode-independent, so mode alternation no longer
      forces pointless re-probes.
    """
    global _backend
    if config_mode == "off":
        return "none"
    if _backend is not None:
        return _backend
    if userns_available():
        _backend = "namespace"
    elif _probe_sandbox_exec():
        _backend = "sandbox-exec"
    else:
        transient, reason, _remedy = _last_unshare_failure or (False, "none", "")
        if transient:
            logger.warning(
                "Sandbox backend probe failed transiently (%s); result NOT cached — "
                "the next spawn re-probes",
                reason,
            )
            return "none"
        _backend = "none"
    logger.info("Sandbox backend: %s (config_mode=%s)", _backend, config_mode)
    return _backend


class SandboxUnavailableError(RuntimeError):
    """``wrap_argv`` fail-closed because this host could not build a sandbox.

    A typed error so a caller can tell "the sandbox refused this spawn" apart
    from any other spawn failure **structurally**, instead of inferring it from
    host capability or pattern-matching English prose. That distinction matters:
    verification is not sandboxed on every platform (``_run_process`` skips the
    wrap on Windows) and the ``sandbox_allow_unsandboxed_exec`` opt-in bypasses
    it entirely, so "this host has no backend" does NOT imply "the sandbox is
    why this particular spawn failed". Reporting it that way would recreate the
    misdiagnosis class of #613 on a different platform.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` handlers keep
    working unchanged.

    ``kind`` is machine-readable so a presentation layer can select its own
    translated remedy copy: ``"transient"`` (momentary resource pressure — not
    cached, retrying works, and callers must NOT advise disabling the sandbox),
    ``"foreign_sandbox"`` (an outer Seatbelt sandbox KiroCrew did not create
    already confines this process and Seatbelt cannot nest — this host's sandbox
    is fine), or ``"no_backend"`` (the host genuinely offers no mechanism).

    ``detail`` is the technical probe reason, which names the failing step (e.g.
    ``"unshare(CLONE_NEWNS) failed with errno 1 (EPERM)"``).

    ``remedy`` is a machine-readable ``REMEDY_*`` token naming the host mechanism
    behind a Linux userns denial (``""`` when unknown), so a presentation layer
    can render the concrete fix for that mechanism rather than a bare errno.
    """

    def __init__(self, message: str, kind: str, detail: str, remedy: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail
        self.remedy = remedy


def reset_backend() -> None:
    """Reset cached backend (for testing or config change)."""
    global _backend, _last_unshare_failure
    _backend = None
    _last_unshare_failure = None


# wrap_argv's ``mode`` vocabulary is a superset of the governance ``sandbox``
# ordinal scale: ``auto`` is an alias that resolves to ``standard`` below.  Only
# this alias mapping lives here; the strictness ORDER is owned solely by
# governance._ORDINAL_SCALES["sandbox"] (the single source of truth) — we never
# re-encode the order, so a new tier added there is honoured here without edit.
_SANDBOX_MODE_ALIASES = {"auto": "standard"}


def credential_mask_applies(mode: str) -> bool:
    """Whether :func:`wrap_argv` would actually APPLY ``extra_hidden_dirs`` for *mode*.

    Exactly two outcomes hand back an UNWRAPPED child, dropping the mask: the ``off``
    tier, and a host with no backend where unsandboxed exec is opted in and no
    governance floor mandates a sandbox (the ``return argv, None`` after
    ``_warn_no_isolation``). Every other path either wraps the argv -- ``namespace``
    and ``sandbox-exec`` both thread ``extra_hidden_dirs`` through -- or REFUSES the
    spawn outright with :class:`SandboxUnavailableError`, and a refusal needs no guard
    because nothing starts.

    Note this predicate is STRICTER than that inventory on one path: with no
    backend it never consults the unsandboxed-exec opt-in, so the opted-in host
    answers False rather than tracking that mutable value. See the branch below.

    This lives here, beside those branches, so a caller whose security argument
    depends on the mask cannot drift from them: a future branch that skips the mask
    is a change to this function, not a silent hole in some other module's copy of
    the reasoning.
    """
    floor = _governance_sandbox_floor()
    effective = _clamp_sandbox_mode_to_floor(mode, floor)
    if effective == "off":
        return False
    # Already inside a Kiro Crew sandbox: a nested re-wrap is impossible by design,
    # wrap_argv passes the argv through (at most an env scrub) and never reaches a
    # backend that could apply the mask. The OUTER sandbox confines the child, but it
    # was built for the tier's own hidden dirs -- which deliberately leave ~/.aws,
    # ~/.ssh and ~/.kube readable for kiro-cli's sake -- so it is NOT a substitute for
    # an adapter-specific credential mask.
    if _inside_kirocrew_sandbox() and _macos_sandbox_state() is not False:
        return False
    if detect_backend(config_mode=effective) != "none":
        return True
    # backend == "none": FAIL CLOSED, reading NO policy value to decide it.
    #
    # Nothing on a host without a backend can carry ``extra_hidden_dirs``, so the
    # only question was whether the spawn would be refused instead -- and every
    # answer to THAT is mutable config read here at preflight and acted on at the
    # spawn. The opt-in was the first such value (an operator opting in between the
    # two let a session that had been told "the mask applies" hand back an unwrapped
    # child); the governance floor is the second, because a ceiling LOOSENED in the
    # same window drops the very refusal that made True safe to report. Both windows
    # close only by refusing to derive this from policy at all: no backend, no mask,
    # so no enforced adapter starts here regardless of how policy moves.
    #
    # The cost is that a no-backend host cannot run an enforced adapter even under a
    # governance floor that forbids unsandboxed execution. That host could not run
    # one anyway -- ``wrap_argv`` cannot satisfy the floor without a backend and
    # raises -- so this changes which layer reports it, not whether it works. Only an
    # ENFORCED adapter reaches here at all: ``enforce_sandbox_floor`` returns early
    # for every harness this core does not enforce, so no first-class path changes.
    return False


def effective_sandbox_mode(mode: str) -> str:
    """The tier :func:`wrap_argv` would ACTUALLY apply for *mode* on this host.

    Applies the governed ``sandbox.min_level`` clamp, so a caller whose security
    argument depends on the sandbox being on can ask whether it will be instead of
    trusting the raw config value -- which a governance floor may silently raise.
    Read-only: same clamp, no spawn, no side effects.
    """
    return _clamp_sandbox_mode_to_floor(mode, _governance_sandbox_floor())


def _governance_sandbox_floor() -> str | None:
    """Read the governed ``sandbox.min_level`` floor, or ``None`` when ungoverned.

    ``wrap_argv`` performs this read ONCE per call and reuses the value for
    both the mode clamp and the first-party carve-out condition, so the two can
    never disagree about whether the same host is governed and the (potentially
    profile-walking) resolve is never duplicated.

    Error posture (every caller inherits it): a ``PlatformCompositionError`` (a
    non-standalone host that could not compose) propagates — the sandbox floor
    must never silently downgrade from DENY to ALLOW on the very host that is
    supposed to be governed.  Any OTHER (transient) error reads as "floor
    absent" (a missing tighten is backstopped by the always-on controls).
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_floor_ordinal

        return governance_floor_ordinal("sandbox.min_level")
    except PlatformCompositionError:
        raise
    except Exception:
        return None


def _clamp_sandbox_mode(mode: str) -> str:
    """Read the governance floor and clamp *mode* up to it.

    Convenience wrapper preserving the read-then-clamp contract for callers and
    tests; :func:`wrap_argv` reads the floor itself (once) and calls
    :func:`_clamp_sandbox_mode_to_floor` directly.
    """
    return _clamp_sandbox_mode_to_floor(mode, _governance_sandbox_floor())


def _floor_mandates_sandbox(floor: str | None) -> bool:
    """True when an already-read ``sandbox.min_level`` *floor* requires isolation.

    ``None`` means ungoverned.  A governed floor at the LOOSEST tier is a policy
    that explicitly requires nothing, so testing the raw string for truthiness
    would read "no isolation required" as "isolation mandatory" and refuse a
    spawn the operator legitimately opted into — while telling them a floor of
    ``off`` forbids unsandboxed execution.

    The loosest tier is derived from the enforcer-owned ordinal registry rather
    than hardcoded, matching :func:`_clamp_sandbox_mode_to_floor`: a renamed or
    re-ordered scale must not silently invert this test.
    """
    if not floor:
        return False
    from kiro_crew.platform.governance import _ORDINAL_SCALES

    return floor != _ORDINAL_SCALES["sandbox"][0]


def _clamp_sandbox_mode_to_floor(mode: str, floor: str | None) -> str:
    """Clamp *mode* UP to an already-read ``sandbox.min_level`` *floor*, if any.

    Derives strictness ranking from the enforcer-owned ordinal registry
    (``OrdinalControl`` over ``_ORDINAL_SCALES['sandbox']``) — NOT a private
    duplicate table — so the floor cannot silently no-op if a tier is added to
    the scale.  Returns *mode* unchanged when there is no governance opinion or
    the floor is already satisfied.

    Fail-closed posture lives in the READ (:func:`_governance_sandbox_floor`):
    a ``PlatformCompositionError`` propagates, any other (transient) error
    reads as "floor absent".  Here, an unknown floor/mode value raises rather
    than ranking it as 0 (which would fail open).
    """
    from kiro_crew.platform.governance import _ORDINAL_SCALES, OrdinalControl

    if not floor:
        return mode
    scale = _ORDINAL_SCALES["sandbox"]
    # The floor already validated through OrdinalControl inside
    # governance_floor_ordinal, so it is in-scale; an unrecognised caller mode is
    # treated as the loosest tier so the floor still clamps it UP (fail-closed —
    # never let an unknown mode skip the tighten).
    cur_value = _SANDBOX_MODE_ALIASES.get(mode, mode)
    floor_rank = OrdinalControl("sandbox", floor).rank()
    cur_rank = scale.index(cur_value) if cur_value in scale else -1
    if floor_rank <= cur_rank:
        return mode
    # The floor's scale value IS a valid wrap_argv mode (off/standard/cc/strict).
    return floor


def wrap_argv(
    argv: list[str],
    mode: str = "auto",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    is_kiro_cli: bool | None = None,
    first_party_fixed_argv: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap a command argv with OS-level sandbox if available.

    Args:
        argv: Original command + args.
        mode: ``"auto"``/``"standard"`` (expose .aws/.ssh/.kube),
              ``"cc"`` (hide .aws but expose .aws/config for Bedrock auth),
              ``"strict"`` (hide everything), ``"off"`` (no sandbox).
        extra_hidden_dirs: Additional absolute directory trees to deny.
        extra_visible_dirs: Trusted paths that must remain visible when an
            otherwise-hidden parent contains them.
        is_kiro_cli: Explicit executable classification for descriptor-backed
            Kiro snapshots whose launch path no longer has a ``kiro-cli``
            basename. ``None`` retains basename detection for other callers.
            Windows internal-sandbox delegation requires this to be exactly
            ``True``; basename inference can never grant that exception.
        first_party_fixed_argv: True ONLY for spawns whose full argv is derived
            inside this package with zero agent/repo/user-config influence;
            every passing site must be allowlisted in
            ``test/test_spawn_audit.py::FIRST_PARTY_SPAWNS``. On a host with
            genuinely no sandbox backend (``no_backend`` — never a transient
            probe failure or a foreign outer sandbox) and no governance
            ``sandbox.min_level`` floor, such a spawn proceeds unconfined
            (env-scrubbed, loudly warned, SEL ``outcome="unconfined"``) instead
            of fail-closing. Inert whenever a backend exists or
            ``sandbox_allow_unsandboxed_exec`` is set.

    Returns:
        (wrapped_argv, cleanup_path_or_None).
        *cleanup_path* is a temp file to delete after the child exits
        (macOS seatbelt profile or Linux launcher script).
        ``None`` when no cleanup is needed.

    Raises:
        RuntimeError: When no sandbox backend is available, mode is not "off",
            ``agent.sandbox_allow_unsandboxed_exec`` is False (default), and
            neither the first-party carve-out nor the explicitly classified
            Windows Kiro internal-sandbox delegation applies.
            This is the fail-closed behavior — the agent subprocess is NOT
            allowed to run without OS-level isolation unless explicitly opted in.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "wrap_argv() performs blocking sandbox preparation and cannot run on "
            "an event loop; await wrap_argv_async() instead"
        )

    # Governance ordinal floor: a policy/profile may require a MINIMUM sandbox
    # tier (off < standard < cc < strict).  Clamp the requested mode up to that
    # floor before resolving the level — so an enterprise "min_level: cc" makes
    # even a mode="off" call run confined.  Cheap no-op when ungoverned.
    #
    # ONE read per wrap_argv call, reused by the first-party carve-out below:
    # the (potentially profile-walking) resolve runs once, and the clamp and
    # the carve-out condition can never disagree about the same host.
    governance_floor = _governance_sandbox_floor()
    mode = _clamp_sandbox_mode_to_floor(mode, governance_floor)

    if mode == "off":
        # Fix #2: verify kiro-cli delegation before honoring "off". The
        # documented invariant (sandbox.py:1680-1681) requires that when
        # Kiro Crew's seatbelt is off, kiro-cli's internal sandbox is ON —
        # but the old early return never checked. Now we verify the delegation
        # on macOS kiro-cli spawns; on Linux (where kiro's internal sandbox
        # doesn't apply) or non-kiro spawns, "off" means genuinely unconfined.
        kiro_spawn_off = _spawns_kiro_cli(argv) if is_kiro_cli is None else is_kiro_cli
        if sys.platform == "darwin" and kiro_spawn_off and kiro_internal_sandbox_enabled():
            # Delegation is valid: kiro-cli's sandbox IS active. Apply env scrub
            # (same as _delegate_to_kiro_internal_sandbox) but WITHOUT the
            # seatbelt fallback on SEL failure — mode="off" must never produce a
            # nested seatbelt wrap (the exact EPERM case the design prevents).
            # SEL audit-or-degrade: record the delegation with critical=True
            # (synchronous write for tamper-evident log), but on failure degrade
            # to unconfined passthrough rather than seatbelt wrap (which would
            # EPERM inside kiro-cli's already-active sandbox).
            try:
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=_command_log_label(argv),
                    tool_kind="subprocess",
                    outcome="delegated",
                    resources=(
                        "mode=off: kiro internal sandbox on -> env scrub only "
                        "(no seatbelt, no seatbelt-fallback)"
                    ),
                    critical=True,  # synchronous write for audit integrity
                )
            except Exception:
                # Fail OPEN (not to seatbelt): an unaudited delegation with
                # mode=off still applies env scrub but returns without seatbelt.
                # This is deliberately different from _delegate_to_kiro_internal_sandbox
                # which falls back to seatbelt — here that fallback would EPERM.
                logger.warning(
                    "SECURITY: SEL audit failed for mode=off delegation; "
                    "proceeding with env scrub but no seatbelt. Command: %s",
                    _command_log_label(argv),
                    exc_info=True,
                )
            unset_args = _sandbox_env_unset_args("standard", strip_python_env)
            if unset_args:
                return [_pinned_env_bin(), *unset_args, *argv], None
            return list(argv), None
        # Fix #3: Make the degradation loud — both layers are inactive.
        _warn_mode_off_unconfined(argv, kiro_spawn_off)
        return argv, None

    # Already inside a KiroCrew sandbox (script cron, sandboxed agent child, app
    # backend, pooled MCP server): the outer sandbox confines every descendant,
    # and a nested wrap is impossible by design — Linux seccomp denies the
    # unshare, macOS Seatbelt refuses sandbox_apply with EPERM. Pass through
    # within the existing isolation boundary.
    #
    # On macOS the marker must agree with the kernel, and the two cover each
    # other's blind spot: the marker proves KiroCrew built the outer sandbox and
    # scrubbed the credential env on the way in, but an env var alone could be
    # forged; the kernel independently confirms a sandbox IS active, but cannot
    # say whose profile it is, so it can never grant this on its own. A definite
    # kernel "not sandboxed" therefore vetoes the marker. An *unanswerable* probe
    # does not: that says nothing, and must not invalidate a marker the Linux
    # path honours unconditionally.
    if _inside_kirocrew_sandbox() and _macos_sandbox_state() is not False:
        if not getattr(wrap_argv, "_nested_passthrough_logged", False):
            wrap_argv._nested_passthrough_logged = True  # type: ignore[attr-defined]
            logger.info(
                "wrap_argv: already inside a KiroCrew sandbox — nested OS "
                "sandboxing is impossible by design (Linux seccomp denies "
                "unshare; macOS Seatbelt refuses sandbox_apply with EPERM); "
                "spawning within the existing isolation boundary rather than "
                "fail-closing on a nesting artifact"
            )
        # Compare the tier the caller asked for against the tier the OUTER
        # sandbox was built at. The passthrough is unavoidable (a nested
        # re-wrap is denied by design on both platforms), but a tier
        # downgrade must be visible, not silent. ``unknown`` = launcher
        # predates the level export (or the value is unrecognized); it has no
        # ordinal, so no downgrade can be proven against it.
        requested_level = _mode_to_level(mode)
        active_level = os.environ.get(_IN_SANDBOX_LEVEL_VAR) or "unknown"
        if active_level not in _TIER_ORDINALS:
            active_level = "unknown"
        tier_downgrade = (
            active_level in _TIER_ORDINALS
            and _TIER_ORDINALS[requested_level] > _TIER_ORDINALS[active_level]
        )
        if tier_downgrade:
            # Loud and per-call (not once-only like the info log above): every
            # downgraded spawn is a distinct security-relevant event.
            logger.warning(
                "SECURITY: nested-sandbox passthrough tier downgrade — caller "
                "requested %r but the outer sandbox runs at %r, so %s executes "
                "at the weaker tier (a nested re-wrap is impossible by design). "
                "Applying the stricter tier's env scrub to the passthrough.",
                requested_level,
                active_level,
                _command_log_label(argv),
            )
        # Emit an SEL audit event for this security-relevant passthrough so the
        # decision to spawn without a *fresh* wrap is tamper-evidently recorded,
        # mirroring the ``denied`` event on the fail-closed path. Outcome is
        # ``allowed`` (a permission grant, not a denial). Fires on EVERY
        # passthrough so the audit trail is complete.
        #
        # critical=True gives this the same write reliability as the fail-closed
        # ``denied`` audit and the ``delegated`` audit: the event is written
        # SYNCHRONOUSLY after draining the async backlog (sel.log), so a
        # slow/wedged background writer can NOT silently drop passthrough
        # records. What it does NOT do is re-raise into a *deny*: unlike
        # _delegate_to_kiro_internal_sandbox — which on audit failure falls back
        # to KiroCrew's own seatbelt, an equally-safe audited layer — a nested
        # passthrough has no safe alternative (seccomp denies the re-wrap by
        # design). Failing the spawn on a SEL filesystem error would couple every
        # in-sandbox MCP call to SEL health and reintroduce a prior in-sandbox
        # spawn outage. The child is confined by the outer namespace + seccomp
        # whether or not the record lands, so on a hard write failure we log
        # loudly and proceed: availability of the confinement over a best-effort
        # audit gap during an already-degraded FS.
        try:
            # circular import (see the fail-closed branch below for the full
            # rationale): sandbox.py is a low-level leaf; defer the sel import.
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key="sandbox",
                agent="system",
                source="sandbox.wrap_argv",
                tool_name=_command_log_label(argv),
                tool_kind="subprocess",
                outcome="allowed",
                metadata={
                    "reason": "nested_sandbox_passthrough",
                    "mode": mode,
                    "requested_tier": requested_level,
                    "active_tier": active_level,
                    # tier_known separates "proven no downgrade" from
                    # "unprovable": tier_downgrade=False alone cannot tell a
                    # consumer which of the two it is looking at.
                    "tier_known": active_level in _TIER_ORDINALS,
                    "tier_downgrade": tier_downgrade,
                },
                critical=True,
            )
        except Exception:
            logger.warning(
                "SEL audit failed for nested-sandbox passthrough — proceeding "
                "unaudited: the outer namespace + seccomp still confine this "
                "spawn, and denying it would brick in-sandbox MCP calls whenever "
                "SEL is down",
                exc_info=True,
            )
        if tier_downgrade:
            # The one slice of the stricter tier that IS enforceable without a
            # nested wrap: its env scrub. A standard outer sandbox scrubbed
            # only _SENSITIVE_ENV_PREFIXES, so agent-denied credential keys
            # (Slack tokens, owner id) are still in this environment; prefix
            # the child with the requested tier's ``env -u`` scrub so it does
            # not inherit them (a delta in practice — the outer launcher
            # already removed the shared prefixes). File-level hides still run
            # at the outer tier — that residual gap is exactly what the audit
            # above records. The ``env`` binary is resolved at a trusted
            # absolute path only (:func:`_unset_env_argv`): this environment
            # can carry a PATH that leads with user-writable directories, and
            # a planted ``env`` there would receive exactly the credentials
            # this scrub exists to withhold. No trusted binary → keep the
            # plain passthrough (never fail closed) and say so.
            unset_args = _sandbox_env_unset_args(requested_level, strip_python_env)
            if unset_args:
                scrub_keys = tuple(unset_args[1::2])
                env_prefix = _unset_env_argv(scrub_keys)
                if env_prefix is not None:
                    return [*env_prefix, *argv], None
                logger.warning(
                    "SECURITY: no trusted env binary (%s) — the requested "
                    "tier's env scrub cannot be applied to this passthrough; "
                    "spawning without it",
                    ", ".join(_ENV_BINARY_CANDIDATES),
                )
        return argv, None
    if _inside_kirocrew_sandbox():
        # Marker present, kernel says NOT sandboxed: the marker can only have been
        # forged or inherited into an unconfined process. Refuse the passthrough
        # and fall through to a normal wrap.
        logger.warning(
            "SECURITY: %s is set but the kernel reports this process is NOT "
            "sandboxed — refusing the nested-sandbox passthrough and falling back "
            "to a normal wrap.",
            _IN_SANDBOX_MARKER,
        )

    # "auto"/"standard" allows git-over-SSH, AWS CLI, kubectl.
    # "cc" hides .aws (exposes only .aws/config for Bedrock credential_process).
    # "strict" hides everything.
    sandbox_level = _mode_to_level(mode)

    # macOS sandbox mutual exclusion: kiro-cli >= 2.13's internal sandbox cannot
    # initialize nested inside KiroCrew's seatbelt (kernel EPERM even under an
    # allow-all outer profile), so exactly one layer can own isolation. When
    # kiro's internal sandbox is enabled, it is that layer for kiro-cli spawns;
    # KiroCrew's sandbox stays on for everything else and whenever kiro's is off.
    # Windows has no Kiro Crew OS sandbox backend. Official Kiro ACP spawns are
    # positively classified by their reviewed callers and delegate to Kiro's
    # built-in sandbox; basename inference is deliberately insufficient to grant
    # this exception, and neither is classification ALONE: the delegation hands
    # isolation to a layer that only exists when kiro's internal sandbox is
    # actually enabled, so the capability is VERIFIED here rather than assumed.
    # Without that read, a Windows install with kiro's sandbox off got an
    # unwrapped argv on the strength of a trust label, i.e. no isolation while
    # the audit trail recorded a delegated one. All other Windows spawns — and a
    # classified spawn whose capability is absent — retain the no-backend
    # fail-closed path. Checked before backend detection so this is a
    # deterministic capability decision, never a fallback after a probe failure.
    # Linux namespace isolation is unaffected.
    kiro_spawn = _spawns_kiro_cli(argv) if is_kiro_cli is None else is_kiro_cli
    delegate_to_kiro = (
        sys.platform == "darwin" and kiro_spawn and kiro_internal_sandbox_enabled()
    ) or (sys.platform == "win32" and is_kiro_cli is True and kiro_internal_sandbox_enabled())
    if delegate_to_kiro:
        if extra_hidden_dirs or extra_visible_dirs:
            # A delegated sandbox cannot enforce KiroCrew-specific path hides.
            # macOS keeps the outer seatbelt. Windows falls through to its
            # no-backend policy and fail-closes unless explicitly opted in.
            if sys.platform == "darwin":
                return sandbox_exec_argv(
                    argv,
                    sandbox_level,
                    strip_python_env=strip_python_env,
                    extra_hidden_dirs=extra_hidden_dirs,
                    extra_visible_dirs=extra_visible_dirs,
                )
        else:
            delegated = _delegate_to_kiro_internal_sandbox(
                argv, sandbox_level, strip_python_env=strip_python_env
            )
            if delegated is not None:
                return delegated
            if sys.platform == "darwin":
                # Preserve macOS's audit-failure fallback: once delegation is
                # refused, Kiro Crew's own seatbelt remains the safe owner.
                return sandbox_exec_argv(argv, sandbox_level, strip_python_env=strip_python_env)

    backend = detect_backend(config_mode=mode)

    if backend == "namespace":
        if extra_hidden_dirs or extra_visible_dirs:
            wrapped = namespace_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
        else:
            wrapped = namespace_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
            )
        # Caller deletes the generated launcher script. Its position is
        # ``1 + len(flags)``, NOT a hardcoded 1: the interpreter flags sit between
        # the executable and the script, so hardcoding leaks the tempfile (and
        # hands the caller a flag to unlink) the moment that list changes.
        return wrapped, _launcher_script_of(wrapped)
    if backend == "sandbox-exec":
        if extra_hidden_dirs or extra_visible_dirs:
            return sandbox_exec_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
        return sandbox_exec_argv(
            argv,
            sandbox_level,
            strip_python_env=strip_python_env,
        )

    if backend == "none":
        # Reaching here while the kernel says this process IS sandboxed means the
        # outer sandbox is NOT one KiroCrew built: a KiroCrew-built one carries
        # KIROCREW_SANDBOX_ACTIVE and was already passed through above. The
        # remaining nested case is a foreign confiner — kiro-cli's own internal
        # seatbelt, or an operator-wrapped gateway — whose profile macOS gives us
        # no supported way to identify, and whose environment our scrub never
        # touched. We therefore do NOT pass through. What we DO fix is the
        # diagnosis: the probe's EPERM is a nesting artifact, not a host verdict,
        # so the error must not claim this host lacks a sandbox backend.
        #
        # FAIL-CLOSED: refuse to execute without sandbox unless explicitly opted in.
        # This addresses a penetration-test finding — the previous behavior silently
        # returned unmodified argv, allowing the agent subprocess to access all
        # credential paths without any OS-level isolation.
        #
        # ONE read of the opt-in: the gate below and the message that explains a
        # refusal must describe the same state, and a concurrent config reload
        # must not let them disagree about the same spawn.
        opted_in = _allow_unsandboxed_exec()
        # A governance ``sandbox.min_level`` floor OVERRIDES the config opt-in
        # (issue #3162).  Before this, the floor did the opposite of what pinning
        # it implies: it disabled the audited first-party carve-out below while
        # leaving this broad opt-in untouched, so a governed fleet lost the
        # constrained path and kept the unconstrained one.  ``config.json`` is not
        # policy — the floor is — so the flag cannot re-open this on a governed
        # host.  Derived from the ONE floor read taken at the top of this call,
        # and via ``_floor_mandates_sandbox`` rather than raw truthiness, because
        # a pinned floor of the loosest tier requires nothing and must not deny.
        floor_mandates_sandbox = _floor_mandates_sandbox(governance_floor)
        if floor_mandates_sandbox or not opted_in:
            # ONE read of the pair: a concurrent re-probe swaps the whole tuple,
            # so failure and remedy can never come from different probes.
            transient, probe_reason, probe_remedy = _last_unshare_failure or (
                False,
                "no probe detail recorded",
                "",
            )
            # First-party carve-out (issue #1563): a spawn whose full argv is
            # derived inside this package (never agent/repo/user-config text)
            # may proceed unconfined on a host that GENUINELY has no backend.
            # All three preconditions, structurally:
            #   * the caller vouched via ``first_party_fixed_argv`` — a reviewed
            #     property, ratcheted by test_spawn_audit.py::FIRST_PARTY_SPAWNS;
            #   * the unavailability class is ``no_backend``: a ``transient``
            #     failure still raises (it self-heals on the next spawn and must
            #     not buy a bypass) and ``foreign_sandbox`` still raises (the
            #     host's sandbox is fine; the remedy is config, not bypass);
            #   * no governance ``sandbox.min_level`` floor is active — reuses
            #     the ONE floor read taken at the top of this call (the same
            #     value the clamp used), so no second profile walk runs and the
            #     two checks cannot disagree; a governed host keeps fail-closing
            #     for first-party spawns too.
            if (
                first_party_fixed_argv
                and _classify_unavailable(transient) == "no_backend"
                and not governance_floor
            ):
                return _first_party_no_backend_passthrough(argv, sandbox_level, strip_python_env)
            if transient:
                # The mechanism follows the retry advice rather than leading it: the
                # cap case is permanently reported transient, so withholding it here
                # would leave doctor and the logs unable to name the one sysctl that
                # fixes the host, while leading with it would read as "reconfigure"
                # to someone whose host is merely busy.
                guidance = (
                    "This probe failure looks TRANSIENT (momentary resource "
                    "pressure) — it is not cached and the next spawn re-probes "
                    "automatically. Do NOT disable the sandbox for this; retry "
                    "instead. "
                ) + _linux_remedy_guidance(probe_remedy)
            elif _inside_macos_sandbox():
                # Nesting under a FOREIGN sandbox — say so, and point at the
                # config-level fix that hands isolation back to KiroCrew's own
                # profile, not at sandbox_allow_unsandboxed_exec (which would
                # disable isolation everywhere to fix a case where a sandbox
                # demonstrably exists).
                guidance = (
                    "This host's sandbox is NOT broken: the kernel reports this "
                    "process is already inside a macOS Seatbelt sandbox that "
                    "KiroCrew did not create, and Seatbelt cannot nest, so "
                    "sandbox-exec fails with EPERM. Spawns under KiroCrew's OWN "
                    "sandbox are unaffected — they carry an isolation marker and "
                    "pass through. The usual cause is kiro-cli's internal "
                    'sandbox: set {"sandbox": false} in '
                    "~/.kiro/settings/amazon-internal.json so KiroCrew's own "
                    "profile owns isolation (that profile is the one that hides "
                    "the credential directories, so this keeps isolation rather "
                    "than weakening it), then restart the gateway. Other outer "
                    "sandboxes (e.g. an operator-wrapped gateway) hit the same "
                    "nesting limit — see docs/system-specs/modules/security.md "
                    '("macOS marker site and the kernel cross-check"). '
                )
            elif is_docker_container():
                # Inside a Docker/OCI container the runtime's seccomp or
                # AppArmor policy blocked unshare(CLONE_NEWUSER).  This is a
                # container-policy restriction, NOT a kernel-level limitation
                # on the host — the correct fix is at the container level, not
                # disabling the sandbox everywhere.
                guidance = (
                    "Running inside a Docker/OCI container where the runtime's "
                    "seccomp or AppArmor policy blocks user namespace creation "
                    f"(probe: {probe_reason}). "
                    "This is a container policy restriction, not a host kernel "
                    "limitation. To resolve, choose one of:\n"
                    "  (a) Use the Kiro Crew custom seccomp profile (adds "
                    "unconditional unshare/clone/mount allows to the Docker "
                    "default — less permissive than seccomp=unconfined):\n"
                    "        # With a repo checkout:\n"
                    "        docker run --security-opt "
                    "seccomp=docker/seccomp/kirocrew-seccomp.json ...\n"
                    "        # Without a checkout (image-only):\n"
                    "        curl -fsSL https://raw.githubusercontent.com/"
                    "kirodotdev/KiroCrew/main/docker/seccomp/kirocrew-seccomp.json"
                    " -o kirocrew-seccomp.json\n"
                    "        docker run --security-opt seccomp=kirocrew-seccomp.json ...\n"
                    "  (b) Restart with explicit unsandboxed consent "
                    "(the container is then the only isolation boundary):\n"
                    "        docker run -e KIROCREW_ALLOW_UNSANDBOXED=1 ...\n"
                    "  (c) Manually set agent.sandbox_allow_unsandboxed_exec=true "
                    "in ~/.kiro/crew/config.json inside the container.\n"
                    "See docs/guides/docker.md for the full sandbox troubleshooting guide."
                )
            else:
                guidance = _no_backend_guidance()
            # When the policy floor is what refused, every guidance above points
            # at the wrong lever: the operator HAS set the opt-in and the flag is
            # deliberately powerless here, so naming it would send them down a
            # dead end.  Replace the remedy rather than appending to it.
            policy_overrode_opt_in = floor_mandates_sandbox and opted_in
            if policy_overrode_opt_in:
                guidance = (
                    "This host is GOVERNED: an enterprise policy pins "
                    f"sandbox.min_level={governance_floor!r}, which forbids "
                    "unsandboxed execution regardless of "
                    "agent.sandbox_allow_unsandboxed_exec — that flag is set on "
                    "this host and is deliberately powerless against the policy, "
                    "so editing config.json cannot resolve this. A governed host "
                    "also withholds the first-party carve-out, so Kiro Crew's own "
                    "built-in spawns are refused here too: this host runs no "
                    "agent subprocess until it has a working sandbox backend "
                    "(see docs/system-specs/modules/security.md) or the policy "
                    "owner relaxes sandbox.min_level."
                )
                sel_reason = (
                    "No sandbox backend available and a governance "
                    f"sandbox.min_level={governance_floor!r} floor forbids "
                    "unsandboxed exec (the config opt-in is set but overridden)"
                )
                refusal = (
                    "Sandbox backend unavailable and a governance policy forbids "
                    "unsandboxed execution. "
                )
            else:
                sel_reason = "No sandbox backend available and allow_unsandboxed_exec is not set"
                refusal = "Sandbox backend unavailable and allow_unsandboxed_exec is not set. "
            # Emit SEL audit event for this security-relevant denial so it
            # appears in the tamper-evident audit log (security-review requirement).
            try:
                from kiro_crew.sel import sel  # circular import: sandbox is low-level

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=_command_log_label(argv),
                    tool_kind="subprocess",
                    outcome="denied",
                    error=(f"{sel_reason} (probe: {probe_reason})"),
                )
            except Exception:
                logger.warning("Failed to emit SEL audit event for sandbox denial", exc_info=True)
            raise SandboxUnavailableError(
                refusal + "No OS-level sandbox backend is available on this host, and the "
                "agent subprocess cannot be safely isolated. "
                f"Probe detail: {probe_reason}. " + guidance,
                kind=_classify_unavailable(transient),
                detail=probe_reason,
                # A transient verdict is never cached, so the host is free to
                # recover on the next call — but it can still name a mechanism.
                # `user.max_user_namespaces` exhaustion surfaces as ENOSPC, which
                # is indistinguishable from momentary fd/disk pressure, so a
                # configured cap of 0 is permanently reported as transient. Withholding
                # the remedy there leaves the one host this token exists for with
                # no way out; the steps are framed as "if this keeps happening" so
                # they never read as advice to reconfigure a merely busy host.
                remedy=probe_remedy,
            )
        # Opted in: warn (or info) and return unmodified argv
        _warn_no_isolation(mode)
    return argv, None


async def wrap_argv_async(
    argv: list[str],
    mode: str = "auto",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    is_kiro_cli: bool | None = None,
    first_party_fixed_argv: bool = False,
    _prepare: Callable[..., tuple[list[str], str | None]] | None = None,
) -> tuple[list[str], str | None]:
    """Cancellation-safe, off-loop sandbox preparation for async spawn paths.

    Sandbox construction probes the host and creates a launcher/profile. It also
    resolves the protected voice-runtime paths on the first call. None of that
    filesystem work may run on a gateway event loop. If the caller is cancelled
    while the worker is finishing, settle it and remove any newly-created
    launcher/profile before propagating cancellation. ``_prepare`` preserves
    each caller's module-local test seam; production callers pass their imported
    :func:`wrap_argv`, and the default is this module's implementation.
    """
    options: dict[str, Any] = {"mode": mode}
    if strip_python_env:
        options["strip_python_env"] = True
    if extra_hidden_dirs:
        options["extra_hidden_dirs"] = extra_hidden_dirs
    if extra_visible_dirs:
        options["extra_visible_dirs"] = extra_visible_dirs
    if is_kiro_cli is not None:
        options["is_kiro_cli"] = is_kiro_cli
    if first_party_fixed_argv:
        options["first_party_fixed_argv"] = True
    prepare = functools.partial(wrap_argv if _prepare is None else _prepare, argv, **options)

    def _prepare_wrapped() -> tuple[list[str], dict[str, str], str | None]:
        wrapped, cleanup = prepare()
        return wrapped, {}, cleanup

    wrapped, _unused_env, cleanup = await shielded_prepare_off_loop(_prepare_wrapped)
    return wrapped, cleanup


# Environment keys always scrubbed from an agent-influenced subprocess'
# environment, regardless of sandbox backend. These are the credential-bearing
# names that must never reach a spawn whose command, arguments, or working
# directory the agent (or a hostile MCP-config / repo) can influence. The OS
# sandbox launcher already drops these when a backend is present (see
# ``ENV_PREFIXES`` in ``namespace_argv`` / ``sandbox_exec_argv``), but scrubbing
# at the parent level too means the guarantee holds even on the opted-in
# ``sandbox_allow_unsandboxed_exec`` fail-open path where no launcher runs.
# Prefix match via ``startswith`` (mirrors the launcher's ENV_PREFIXES check).
_SPAWN_SCRUB_ENV_PREFIXES: list[str] = list(_SENSITIVE_ENV_PREFIXES) + list(_AGENT_DENIED_ENV_KEYS)


def scrub_env(
    env: dict[str, str] | None = None,
    *,
    extra_prefixes: list[str] | None = None,
) -> dict[str, str]:
    """Return a copy of *env* (default ``os.environ``) with credential-bearing
    keys removed.

    Drops every key whose name starts with one of ``_SPAWN_SCRUB_ENV_PREFIXES``
    (AWS secret/session vars, SSH_AUTH_SOCK, GNUPGHOME, GIT_ASKPASS, and the
    Slack/owner tokens seeded into ``os.environ`` for trusted children). Used to
    build the environment for agent-influenced spawns so a spawned process
    cannot read secrets straight out of the inherited environment.

    *extra_prefixes* adds more name prefixes to drop (e.g.
    ``_PYTHON_ENV_PREFIXES`` when the spawn is a foreign Python child).
    """
    prefixes = _SPAWN_SCRUB_ENV_PREFIXES + (extra_prefixes or [])
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if not any(k.startswith(p) for p in prefixes)}


def scrub_agent_denied_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of *env* with gateway-owned channel credentials removed.

    Drops every key matching ``_AGENT_DENIED_ENV_KEYS`` — the Slack/WeCom/
    Telegram tokens and owner id that ``config/loader.load_credentials()`` seeds
    into ``os.environ`` for trusted children only.

    This is the PARENT-level complement to the OS-sandbox launcher scrub. The
    launcher (``namespace_argv`` / ``sandbox_exec_argv``) only strips these keys
    for the ``cc``/``strict`` tiers; on the default ``auto``/``standard`` tier
    they are left in place. The production ACP spawn paths
    (:meth:`AcpRuntime._spawn` / :meth:`AcpClient._spawn`) copy a raw
    ``os.environ`` and call :func:`wrap_argv` directly (not
    :func:`sandboxed_spawn_argv`), so without this scrub the channel credentials
    would be inherited by the agent subprocess on the default tier — reachable
    via ``env`` / ``os.environ`` and usable to control those channel identities
    outside KiroCrew.

    Unlike :func:`scrub_env`, this deliberately does NOT strip
    ``_SENSITIVE_ENV_PREFIXES`` (AWS/SSH/GPG): the ``standard`` sandbox is
    designed to leave git-over-SSH, the AWS CLI and kubectl usable, so those
    vars must survive the parent scrub. Prefix match via ``startswith`` mirrors
    the launcher's ENV_PREFIXES check.
    """
    return {
        k: v for k, v in env.items() if not any(k.startswith(p) for p in _AGENT_DENIED_ENV_KEYS)
    }


def scrub_agent_subprocess_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the full environment scrub required for a Kiro/ACP child.

    This is the parent-side equivalent of the OS launchers' sensitive-variable
    removal plus ``strip_python_env=True``. It is mandatory for Windows Kiro
    delegation because Windows cannot express the POSIX ``env -u`` prefix, and
    keeping it on every platform makes delegated and wrapped ACP spawns inherit
    the same environment policy.
    """
    return scrub_env(env, extra_prefixes=_PYTHON_ENV_PREFIXES)


def sandboxed_spawn_argv(
    argv: list[str],
    mode: str = "standard",
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    first_party_fixed_argv: bool = False,
) -> tuple[list[str], dict[str, str], str | None]:
    """Single chokepoint for agent-influenced subprocess spawns.

    Wraps *argv* with the OS-level sandbox (:func:`wrap_argv`) AND returns a
    credential-scrubbed environment (:func:`scrub_env`), so every caller gets
    both the filesystem-isolation and the environment-hiding layer without
    having to remember to apply each separately. This is the wrapper the
    subprocess-spawn audit test (``test/test_spawn_audit.py``) requires every
    agent-influenced spawn in ``src/kiro_crew`` to route through.

    Args:
        argv: Original command + args.
        mode: Sandbox mode passed to :func:`wrap_argv` (default ``"standard"``:
            hides non-workflow credential dirs while leaving git-over-SSH and
            the AWS CLI usable).
        env: Base environment to scrub (default ``os.environ``). Pass a
            pre-augmented env (e.g. with a resolved ``PATH``) to have the scrub
            applied on top of it.
        strip_python_env: Strip ``PYTHONPATH``/``PYTHONHOME`` so a foreign
            Python child does not inherit KiroCrew's interpreter paths. Applied
            BOTH inside :func:`wrap_argv`'s launcher AND to the returned env, so
            the strip holds even on the fail-open path where no launcher runs.
        extra_hidden_dirs: Additional absolute directory trees the caller needs
            hidden in both the macOS Seatbelt and Linux namespace profiles.
        extra_visible_dirs: Trusted paths that must remain visible when an
            otherwise-hidden parent contains them.
        first_party_fixed_argv: Threaded to :func:`wrap_argv`. True ONLY for
            spawns whose full argv is derived inside this package with zero
            agent/repo/user-config influence; every passing site must be
            allowlisted in ``test/test_spawn_audit.py::FIRST_PARTY_SPAWNS``.
            See :func:`wrap_argv` for the no-backend carve-out it gates.

    Returns:
        ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)``. The caller MUST
        pass *scrubbed_env* as the subprocess ``env=`` and unlink *cleanup_path*
        (a temp launcher/profile) after the child exits.
    """
    if extra_hidden_dirs or extra_visible_dirs:
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=strip_python_env,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
            first_party_fixed_argv=first_party_fixed_argv,
        )
    else:
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=strip_python_env,
            first_party_fixed_argv=first_party_fixed_argv,
        )
    # ``wrap_argv`` only strips PYTHONPATH/PYTHONHOME inside the launcher script,
    # so on the fail-open path (no sandbox backend, opted-in unsandboxed exec) it
    # returns argv unmodified and the strip never happens. Apply the same strip
    # to the scrubbed env here so ``strip_python_env=True`` holds regardless of
    # whether a backend is available.
    extra = _PYTHON_ENV_PREFIXES if strip_python_env else None
    scrubbed = scrub_env(env, extra_prefixes=extra)
    # The cgroup wrapper prepended below needs the user session bus in the
    # environment it is spawned with, so restore its locator vars after the
    # scrub. Callers that pass a strict allowlist env (e.g. the source-provider
    # CLI) otherwise hand systemd-run an environment it cannot reach the bus
    # from and the spawn dies before exec'ing the real command.
    patched, injected = cgroup_scope_bus_env(scrubbed)
    if injected:
        # The locators are the WRAPPER's capability, never the child's: a bus
        # address in the sandboxed process is a sandbox escape (it can ask the
        # user systemd manager to start a unit that runs outside the namespace).
        # So they live only until systemd-run has used them — an ``env -u`` shim
        # inside the scope drops them again before the real command execs. If no
        # ``env`` binary is available we fail CLOSED: keep the scrubbed env, let
        # the wrapper fail loudly, and never hand the child a live bus.
        unset = _unset_env_argv(injected)
        if unset is None:
            logger.warning(
                "SECURITY: no `env` binary to drop %s inside the cgroup scope; "
                "not forwarding the bus locators (systemd-run will fail rather "
                "than leak a user-bus address into the sandboxed child).",
                ", ".join(injected),
            )
        else:
            scrubbed = patched
            wrapped = [*unset, *wrapped]
    # cgroup v2 scope (OUTERMOST layer): bound the spawned process tree with
    # pids.max + memory.max. Applied here so every sandboxed_spawn_argv caller
    # gets the fork-bomb / memory-DoS ceiling without threading it through each
    # site. No-op (with a one-time loud warning) where cgroup delegation is
    # unavailable. Safe re: the cleanup path — that is returned separately, not
    # re-derived from an argv index, so prepending systemd-run does not disturb
    # it. See docs/architecture/resource-protection.md.
    wrapped = cgroup_scope_argv(wrapped)
    # Positive-identity marker for the orphan sweep: every tree spawned through
    # this chokepoint (and its descendants, via env inheritance) is identifiable
    # as KiroCrew-spawned even when its cmdline carries no KiroCrew fingerprint
    # (e.g. ``npx @playwright/mcp``).
    scrubbed[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    return wrapped, scrubbed, cleanup


async def shielded_prepare_off_loop(
    prepare: Callable[[], tuple[list[str], dict[str, str], str | None]],
    *,
    executor: ThreadPoolExecutor | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Run a spawn-preparation callable off the loop, shielded from cancellation.

    ``prepare`` must follow the :func:`sandboxed_spawn_argv` contract: it returns
    ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)`` where the third element
    is a temp launcher/profile the CALLER must unlink after the child exits.

    Every async caller reaches the sync chokepoint through a worker hop.
    Cancelling that hop abandons the returned tuple while the worker still
    materializes the launcher/profile, leaking the temp file forever.  Shielding
    the hop keeps the worker's result recoverable: on cancellation we wait for
    the thread to settle, unlink the launcher it created, and re-raise.

    A REPEAT cancellation landing on a bare recovery ``await`` is a
    ``BaseException`` that would abandon the recovery before the unlink runs,
    leaking the materialized launcher (#5841).  The settle-then-unlink therefore
    runs as its own task, shielded from cancellations aimed at this caller; each
    absorbed repeat is ``uncancel()``-ed so an enclosing ``asyncio.timeout``
    still reports ``TimeoutError``, and the ORIGINAL cancellation is re-raised
    once the launcher is gone.

    ``executor`` keeps pool choice with the CALLER, because which pool absorbs a
    wedged preparation is per-site policy, not a shield concern: the chokepoint
    can cold-probe the sandbox backend with a synchronous subprocess, and
    :mod:`kiro_crew.executors` partitions blocking work into named pools so such
    a probe cannot occupy the workers another subsystem (the orphan-reaping
    sweep) needs.  Defaulting it to ``None`` — the loop's default pool, via
    ``asyncio.to_thread`` — would silently collapse that partition for callers
    that had chosen a pool, so every site that had one passes it explicitly.
    """

    Prepared = tuple[list[str], dict[str, str], str | None]
    task: asyncio.Future[Prepared]
    if executor is None:
        task = asyncio.ensure_future(asyncio.to_thread(prepare))
    else:
        task = asyncio.get_running_loop().run_in_executor(executor, prepare)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        async def _settle_then_unlink() -> None:
            cleanup: str | None = None
            with contextlib.suppress(Exception, asyncio.CancelledError):
                _, _, cleanup = await task
            if not cleanup:
                return
            target = cleanup

            def _unlink() -> None:
                with contextlib.suppress(OSError):
                    os.unlink(target)

            if executor is None:
                await asyncio.to_thread(_unlink)
            else:
                await asyncio.get_running_loop().run_in_executor(executor, _unlink)

        current = asyncio.current_task()
        recovery = asyncio.create_task(_settle_then_unlink())
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError:
                uncancel = getattr(current, "uncancel", None)  # 3.11+
                if uncancel is not None:
                    uncancel()
            except Exception:
                logger.warning(
                    "sandbox launcher cleanup failed after cancellation",
                    exc_info=True,
                )
        raise


async def sandboxed_spawn_argv_async(
    argv: list[str],
    mode: str | None = None,
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    first_party_fixed_argv: bool = False,
    executor: ThreadPoolExecutor | None = None,
    _prepare: Callable[..., tuple[list[str], dict[str, str], str | None]] | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Prepare a sandboxed spawn safely off-loop, retaining caller test seams."""
    # Preserve the long-standing injectable preparation seam: many focused
    # callers replace ``sandboxed_spawn_argv`` with a narrow ``(argv, *, env)``
    # test double. ``None`` means the caller omitted the argument, in which case
    # the synchronous function supplies its own ``standard`` default. An
    # explicitly supplied value -- including ``standard`` -- is forwarded.
    options: dict[str, Any] = {}
    if mode is not None:
        options["mode"] = mode
    if env is not None:
        options["env"] = env
    if strip_python_env:
        options["strip_python_env"] = True
    if extra_hidden_dirs:
        options["extra_hidden_dirs"] = extra_hidden_dirs
    if extra_visible_dirs:
        options["extra_visible_dirs"] = extra_visible_dirs
    if first_party_fixed_argv:
        options["first_party_fixed_argv"] = True
    return await shielded_prepare_off_loop(
        functools.partial(
            sandboxed_spawn_argv if _prepare is None else _prepare,
            argv,
            **options,
        ),
        executor=executor,
    )


# ── cgroup v2 scope enforcement (fork bomb + memory DoS) ──
# The RLIMIT preexec (resource_limit_preexec) caps a SINGLE process's FDs, but
# RLIMIT is the wrong tool for the finding's headline threats: RLIMIT_NPROC is
# per-real-UID (not per-spawn-subtree) and RLIMIT_AS caps virtual not resident
# memory. cgroup v2 pids.max / memory.max are the correct per-cgroup ceilings —
# they bound the agent + all its MCP-server/tool descendants as one unit, and
# the kernel enforces at fork()/alloc time (no reaper race). We place each
# agent-influenced spawn in a transient systemd --user --scope, which works
# UNPRIVILEGED when the user session has cgroup v2 delegation (pids + memory
# controllers). See docs/architecture/resource-protection.md.

# Default cgroup ceilings (per agent scope). Overridable via the same
# ``resource_limits`` config block used by apply_resource_limits.
_CGROUP_DEFAULT_MAX_PROCESSES = 8192  # pids.max counts TASKS (threads), not processes;
# 1024 starved legitimate JVM build trees (Gradle + parallel test workers need
# thousands of threads -> pthread_create EAGAIN / 'unable to create native thread'
# while the host is idle); 8192 still bounds fork bombs which spawn tens of
# thousands of tasks near-instantly. Override via resource_limits.max_processes.

# CPUWeight — proportional CPU share for agent scopes (systemd default is 100).
# Setting 50 makes agent scopes yield to interactive work under CPU contention
# while still using 100% of idle CPU — proportional share, never a hard throttle.
# Both grok-build and OpenClaw ship no default CPU quota; fair-share weight is
# the correct default for agent workloads that include legitimate builds.
_CGROUP_DEFAULT_CPU_WEIGHT = 50

# The memory.max default is HOST-PROPORTIONAL, not a flat cap: the agent
# subprocess tree may occupy up to this fraction of physical RAM before the
# kernel OOM-kills the scope. This is a PER-SCOPE ceiling (each spawn gets its
# own transient scope), so 65% bounds a single runaway tree to a share that
# leaves headroom for the OS + gateway — it is NOT an aggregate host guarantee
# across many concurrent scopes. It gives the agent real headroom on the 16–32
# GB machines this targets (16 GB → ~10.6 GB, 32 GB → ~21.3 GB) — where a flat
# 8 GB cap was both too tight on big boxes and too loose on small ones. There
# is deliberately NO floor: a floor could push a tiny box above 65%, and 65% is
# the ceiling on our take.
_CGROUP_MEMORY_FRACTION = 0.65
# Fallback memory.max (MB) used only when physical RAM can't be read (sysconf
# missing/unknown). The cgroup path is Linux-only, where SC_PHYS_PAGES exists,
# so this is a belt-and-suspenders default, not the normal path.
_CGROUP_FALLBACK_MAX_MEMORY_MB = 8192

# The slice every agent scope nests under (systemd dash-hierarchy places it at
# kirocrew.slice/kirocrew-agents.slice inside the user manager). It is also
# the aggregate enforcement boundary — see ensure_agents_slice_limits().
_CGROUP_AGENTS_SLICE = "kirocrew-agents.slice"


def _instance_slice_token() -> str:
    """A short, systemd-safe token identifying THIS instance's data home.

    Hex only, and specifically NO dashes: systemd reads a dash in a slice name
    as a hierarchy separator, so a token containing one would silently insert
    another slice level (and two tokens could collide on a shared prefix).

    Keyed on the resolved data home because that is already the ownership key
    the rest of the codebase uses — ``cleanup_orphaned_session_roots`` reaps
    from a per-data-home account book, which is exactly why it cannot reach a
    co-resident instance's children. Deriving the slice from the same key keeps
    one notion of "whose process is this" instead of adding a second.
    """
    return hashlib.sha256(str(config_dir()).encode("utf-8")).hexdigest()[:12]


def _agents_slice_name() -> str:
    """The slice new agent scopes are placed in: one child per instance.

    Every gateway on a host shared ONE slice, because the name was a module
    constant with no per-instance component. Nothing downstream could then tell
    one instance's scopes from another's, since a scope's identity is its slice
    plus a timestamp and neither names an owner. That is a correctness floor
    for anything operating on scopes as a population: a host routinely runs
    several gateways at once — ``kirocrew pod up`` creates one per pod by
    design — so "every scope in the agents slice" is not "my scopes", and a
    sweep that assumes it is reaches into a live co-resident session.

    Returns a dash-nested CHILD of :data:`_CGROUP_AGENTS_SLICE`, so the parent
    stays the aggregate enforcement boundary: cgroup v2 bounds a descendant by
    the minimum effective limit along its ancestor chain, so the MemoryHigh /
    MemoryMax on the parent keep applying to every instance's scopes exactly as
    before. Callers that match on the parent's name in a cgroup path (it is a
    path component of every scope either way) are likewise unaffected.

    Degrades to the bare parent slice if the data home cannot be resolved: a
    missing ownership component must not fail the spawn, matching how an
    unavailable cgroup backend is handled.
    """
    try:
        token = _instance_slice_token()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "could not derive a per-instance agent slice (%s); falling back to "
            "the shared %s — scopes from this instance will not be "
            "distinguishable from a co-resident gateway's",
            exc,
            _CGROUP_AGENTS_SLICE,
        )
        return _CGROUP_AGENTS_SLICE
    return f"{_CGROUP_AGENTS_SLICE[: -len('.slice')]}-{token}.slice"


def _default_max_memory_mb() -> int:
    """Return the default cgroup ``memory.max`` in MB: a fixed fraction
    (:data:`_CGROUP_MEMORY_FRACTION`) of physical RAM, so the ceiling scales
    with the machine instead of being a flat cap. Falls back to
    :data:`_CGROUP_FALLBACK_MAX_MEMORY_MB` if host RAM can't be determined.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    # Windows has no ``os.sysconf``, so the probe above raises AttributeError and
    # would leave a FLAT cap that ignores the machine entirely. That is not a
    # cosmetic gap now that ``apply_windows_resource_ceiling`` consumes this
    # value: on an 8 GB host the fallback EQUALS physical RAM and on a smaller
    # one it exceeds it, so the Job object's memory limit could never engage
    # before the host was exhausted — the ceiling would exist and enforce
    # nothing. Ask the kernel instead. ``system_memory()`` returns None off
    # Windows, so POSIX still reaches the fallback below unchanged.
    mem = platform_compat.system_memory()
    if mem is not None:
        mb = int(mem[0] * _CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    return _CGROUP_FALLBACK_MAX_MEMORY_MB


# Cached (available, reason) probe result — the environment doesn't change
# within a process, and the probe shells out, so compute it once.
_CGROUP_SCOPE_PROBE: tuple[bool, str] | None = None
_CGROUP_WARNED = False


def _warn_cgroup_unavailable(reason: str) -> None:
    """Emit the one-time SECURITY warning for a host without cgroup enforcement.

    Shared by the per-spawn wrapper and the slice-limit application so a host
    where delegation is missing produces exactly ONE warning, no matter which
    site notices first — both react to the same host condition.
    """
    global _CGROUP_WARNED
    if _CGROUP_WARNED:
        return
    _CGROUP_WARNED = True
    logger.warning(
        "SECURITY: cgroup v2 scope enforcement unavailable (%s); agent "
        "subprocess fork-bomb / memory-DoS ceilings are NOT enforced on "
        "this host. RLIMIT_NOFILE still applies. See "
        "docs/architecture/resource-protection.md.",
        reason,
    )


def _probe_cgroup_scope() -> tuple[bool, str]:
    """Return (available, reason) for unprivileged cgroup-v2 scope enforcement.

    Requires, on Linux: a pure cgroup-v2 mount, the ``pids`` and ``memory``
    controllers delegated to our user slice, a ``systemd-run`` binary, and a
    user session bus (XDG_RUNTIME_DIR). Any missing piece → not available.
    """
    global _CGROUP_SCOPE_PROBE
    if _CGROUP_SCOPE_PROBE is None:
        _CGROUP_SCOPE_PROBE = _compute_cgroup_scope_probe()
    return _CGROUP_SCOPE_PROBE


def _compute_cgroup_scope_probe() -> tuple[bool, str]:
    """Uncached capability check backing :func:`_probe_cgroup_scope`."""
    if sys.platform != "linux":
        return (False, "not Linux")
    if shutil.which("systemd-run") is None:
        return (False, "systemd-run not found")
    # A user session bus is required for `systemd-run --user`.
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return (False, "no XDG_RUNTIME_DIR (no systemd user session)")
    # Pure cgroup v2 unified hierarchy.
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            # v2 is a single line beginning "0::".
            if not any(line.startswith("0::") for line in fh):
                return (False, "not a cgroup v2 unified hierarchy")
    except OSError as exc:
        return (False, f"cannot read /proc/self/cgroup: {exc}")
    # The pids + memory controllers must be delegated to our user slice, else
    # systemd-run --scope can set the knobs but the kernel won't enforce them.
    try:
        uid = os.getuid()
        ctrl_path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/cgroup.controllers"
        with open(ctrl_path, encoding="utf-8") as fh:
            controllers = set(fh.read().split())
        missing = {"pids", "memory"} - controllers
        if missing:
            return (False, f"controllers not delegated: {sorted(missing)}")
    except OSError as exc:
        return (False, f"cannot read delegated controllers: {exc}")
    return (True, "ok")


_CPU_DELEGATED: bool | None = None


def _cpu_controller_delegated() -> bool:
    """Return True when the ``cpu`` controller is delegated to our user slice.

    CPUWeight / CPUQuota on a ``systemd-run --user`` scope are only enforced
    when the cpu controller is delegated; emitting them without delegation is
    a silent no-op at best and a warning at worst, so callers gate the CPU
    properties on this check. Cached alongside the main probe (the environment
    is process-stable). Failure to read → False (skip CPU properties, keep
    pids/memory enforcement).
    """
    global _CPU_DELEGATED
    if _CPU_DELEGATED is None:
        try:
            uid = os.getuid()
            ctrl_path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/cgroup.controllers"
            with open(ctrl_path, encoding="utf-8") as fh:
                _CPU_DELEGATED = "cpu" in fh.read().split()
        except OSError:
            _CPU_DELEGATED = False
    return _CPU_DELEGATED


def _cgroup_limits_from_config() -> tuple[int, int, int, int]:
    """Return ``(max_processes, max_memory_mb, cpu_weight, max_cpu_percent)``
    for the cgroup scope.

    Reads the same ``resource_limits`` config block as apply_resource_limits;
    falls back to the module defaults. ``0`` (or junk) means "use default" for
    the cgroup ceiling — unlike the RLIMIT path, we never leave the cgroup DoS
    ceiling unset by default (that is the whole point of this control). The
    memory default is host-proportional (see :func:`_default_max_memory_mb`).

    ``max_cpu_percent`` is the OPT-IN hard CPU quota (``CPUQuota``): ``0``
    (the default) means "no quota property emitted at all" — hard CPU caps
    slow legitimate builds, so unlike the other ceilings this one is off
    unless an operator explicitly sets ``resource_limits.max_cpu_percent``.
    """
    max_procs = _CGROUP_DEFAULT_MAX_PROCESSES
    max_mem_mb = _default_max_memory_mb()
    cpu_weight = _CGROUP_DEFAULT_CPU_WEIGHT
    max_cpu_percent = 0  # opt-in: 0 = emit no CPUQuota
    try:
        # circular import: sandbox is a low-level module imported by
        # config/security consumers — importing kiro_crew.config.loader at
        # module load would create an import cycle, so it stays function-level
        # (same pattern as resource_limit_preexec below).
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        # One validated read for the whole block. ResourceLimitsConfig.from_raw
        # is the only place these keys are coerced, and it is what refuses a
        # fraction, a NaN/Infinity from json.loads, and a non-number before
        # ``int()`` can raise on them and abort the remaining fields.
        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
        # ``>= 1``, so 0 lands on the default with everything else out of domain:
        # TasksMax=0 / MemoryMax=0M are rejected by systemd and the scope would
        # never start, so this ceiling is never left unset. The SAME two keys
        # mean "leave inherited" when 0 reaches the rlimit path in
        # security.apply_resource_limits — ResourceLimitsConfig carries both
        # domains so neither side can be tightened without seeing the other.
        if rl.max_processes is not None and rl.max_processes >= 1:
            max_procs = rl.max_processes
        if rl.max_memory_mb is not None and rl.max_memory_mb >= 1:
            max_mem_mb = rl.max_memory_mb
        # Range-checked at the parse site (1..10000), so any value that arrives
        # here is usable as-is.
        if rl.cpu_weight is not None:
            cpu_weight = rl.cpu_weight
        # Opt-in: 0 keeps the "emit no CPUQuota" default rather than capping.
        if rl.max_cpu_percent is not None and rl.max_cpu_percent > 0:
            max_cpu_percent = rl.max_cpu_percent
    except Exception:
        logger.debug("cgroup limits: config unavailable, using defaults")
    return max_procs, max_mem_mb, cpu_weight, max_cpu_percent


# ── aggregate agent-slice soft ceiling (memory.high on kirocrew-agents.slice) ──
# Per-scope MemoryMax bounds ONE runaway spawn tree, but scopes are created per
# spawn: several concurrent agent trees, each legitimately under its own 65%
# cap, can still sum past physical RAM and livelock a swapless host. memory.high
# on the slice all agent scopes share throttles-and-reclaims the whole subtree
# once the SUM of agent memory crosses it, keeping the kernel and the gateway
# (which lives outside the slice) responsive — a soft layer BELOW the slice's
# hard aggregate memory.max (ensure_agents_slice_limits), which OOM-kills a
# scope only when throttling was not enough, while each scope's own memory.max
# still hard-kills an individual runaway. Same trust model as the scope
# ceilings: unprivileged, enforced by the user manager, requires the memory
# controller delegated to the user slice (the existing _probe_cgroup_scope
# check).

# Fraction of physical RAM used for the default slice memory.high. Higher than
# the per-scope 65% because it bounds the SUM of all agent scopes, and below
# the slice's hard 80% memory.max so throttling engages before the kernel
# OOM-kills, with OS + gateway headroom preserved even while the whole fleet
# is being throttled.
_SLICE_MEMORY_HIGH_FRACTION = 0.75
# Fallback slice memory.high (MB) used only when physical RAM can't be read.
# The slice path is Linux-only, where SC_PHYS_PAGES exists, so this is a
# belt-and-suspenders default, not the normal path.
_SLICE_FALLBACK_MEMORY_HIGH_MB = 12288

# Last MemoryHigh value applied to the slice by THIS process ("24576M" /
# "infinity"), or None before the first reconcile. The desired value is
# host-derived and constant for the process's life (no config input), so
# after the first successful apply every later reconcile reduces to a
# string compare and no-ops. Kept as a reconcile (rather than a one-shot)
# so an apply that failed transiently is retried on the next spawn.
_SLICE_MEMHIGH_APPLIED: str | None = None
# Process-level kill switch: set after a failed apply so a broken systemctl is
# warned about ONCE and never hammered on every subsequent spawn.
_SLICE_MEMHIGH_DISABLED = False


def _default_slice_memory_high_mb() -> int:
    """Return the default slice ``memory.high`` in MB: a fixed fraction
    (:data:`_SLICE_MEMORY_HIGH_FRACTION`) of physical RAM, falling back to
    :data:`_SLICE_FALLBACK_MEMORY_HIGH_MB` if host RAM can't be determined.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _SLICE_MEMORY_HIGH_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    return _SLICE_FALLBACK_MEMORY_HIGH_MB


def _ensure_agent_slice_memory_high() -> None:
    """Reconcile ``MemoryHigh`` on :data:`_CGROUP_AGENTS_SLICE` with the host default.

    The slice is UID-GLOBAL: every gateway instance under this user (live,
    dev-backend, pods with delegation) parents scopes into the same slice, so
    the ceiling is deliberately NOT config-driven — a per-instance config key
    would let one instance (e.g. a dev gateway configured permissively) lift
    or lower the ceiling that protects the others. The value is always the
    host-derived default (:func:`_default_slice_memory_high_mb`).

    Runs ``systemctl --user set-property --runtime`` — unprivileged: the user
    manager owns the slice, and the memory controller is delegated wherever the
    caller's probe passed. ``--runtime`` is deliberate: the drop-in lives under
    ``$XDG_RUNTIME_DIR`` and vanishes with the login session, so no persistent
    unit files accumulate under ``~/.config`` and a stale ceiling can never
    outlive the login session that set it.

    Never raises: agent spawns must not fail because the ceiling could not be
    applied. On failure it logs one loud warning and disarms for the rest of
    the process.
    """
    global _SLICE_MEMHIGH_APPLIED, _SLICE_MEMHIGH_DISABLED
    if _SLICE_MEMHIGH_DISABLED:
        return
    desired = f"{_default_slice_memory_high_mb()}M"
    if desired == _SLICE_MEMHIGH_APPLIED:
        return
    try:
        systemctl = platform_compat.trusted_system_bin("systemctl")
        if systemctl is None:
            raise FileNotFoundError("systemctl not found in trusted system dirs")
        proc = subprocess.run(
            [
                systemctl,
                "--user",
                "set-property",
                "--runtime",
                _CGROUP_AGENTS_SLICE,
                f"MemoryHigh={desired}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "non-zero exit")
    except Exception as exc:
        _SLICE_MEMHIGH_DISABLED = True
        logger.warning(
            "SECURITY: could not apply MemoryHigh=%s to %s (%s); the AGGREGATE "
            "agent memory ceiling is NOT enforced on this host — per-scope "
            "MemoryMax still applies. See "
            "docs/architecture/resource-protection.md.",
            desired,
            _CGROUP_AGENTS_SLICE,
            exc,
        )
        return
    _SLICE_MEMHIGH_APPLIED = desired
    logger.info("agent slice %s: MemoryHigh=%s applied", _CGROUP_AGENTS_SLICE, desired)


# Last observed value of the slice's memory.events `high` counter, or None
# before the first successful read. The first read only baselines — the
# counter is monotonic for the slice cgroup's lifetime, so a nonzero value
# may predate this process — and climbs are judged against it.
_SLICE_MEMHIGH_EVENTS_SEEN: int | None = None
# True while inside a climbing episode that has already been warned about, so
# sustained throttling logs once per episode instead of on every spawn. Reset
# when an observation finds the counter stable (episode over) or lower (slice
# cgroup recreated).
_SLICE_MEMHIGH_CLIMB_WARNED = False


def _slice_memory_events_high() -> int | None:
    """Return the ``high`` counter from the slice cgroup's ``memory.events``.

    The slice directory comes from :func:`_agents_slice_cgroup_dir`, which
    understands systemd's dash-hierarchy (``kirocrew-agents.slice`` nests
    under ``kirocrew.slice`` in the user manager's subtree). ``None`` when it
    cannot be read: not Linux, no cgroup v2, the slice cgroup not currently
    materialized (systemd releases an empty slice), or unparseable content.
    """
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return None
    return _read_cgroup_counters(slice_dir / "memory.events").get("high")


def _check_slice_memory_pressure() -> None:
    """Warn when the slice's ``memory.events`` ``high`` counter climbs.

    Past ``memory.high`` the kernel throttles-and-reclaims the subtree
    SILENTLY — agents just slow down; nothing kills and nothing alerts, since
    per-scope ``MemoryMax`` never fired. The ``high`` counter climbing is the
    kernel's only signal that the aggregate ceiling is throttling, and
    surfacing it makes "agents mysteriously slow" diagnosable from the
    gateway log as ceiling throttling rather than a hang.

    Warned once per climbing episode: the first observed increase logs, later
    increases stay silent until an observation finds the counter stable,
    which closes the episode. A DECREASE means the slice cgroup was recreated
    (an empty slice is released and its counters reset) — re-baseline
    silently, never warn.
    """
    global _SLICE_MEMHIGH_EVENTS_SEEN, _SLICE_MEMHIGH_CLIMB_WARNED
    current = _slice_memory_events_high()
    if current is None:
        return
    previous = _SLICE_MEMHIGH_EVENTS_SEEN
    _SLICE_MEMHIGH_EVENTS_SEEN = current
    if previous is None or current <= previous:
        # First read, counter stable, or slice cgroup recreated: (re)baseline
        # and close any open episode.
        _SLICE_MEMHIGH_CLIMB_WARNED = False
        return
    if _SLICE_MEMHIGH_CLIMB_WARNED:
        return
    _SLICE_MEMHIGH_CLIMB_WARNED = True
    logger.warning(
        "agent slice %s: memory.events high counter climbed %d -> %d — "
        "aggregate agent memory crossed the slice MemoryHigh ceiling and the "
        "kernel is throttling the whole agent subtree; agents run slowly "
        "(not hung) until aggregate memory drops. See "
        "docs/architecture/resource-protection.md.",
        _CGROUP_AGENTS_SLICE,
        previous,
        current,
    )


# Serializes reconciliation workers. Deliberately a plain blocking mutex held
# for the whole reconcile body: every schedule spawns its own short-lived
# worker and workers queue on the mutex, so concurrent spawns never interleave
# systemctl calls and a failed apply is retried by the next spawn's worker.
# The desired value is host-derived and process-constant (no config input) —
# the mutex guards the apply/retry handoff, not value freshness. Redundant
# workers are near-free (the applied-value check reduces them to a string
# compare), and thread count is bounded by concurrent agent spawns.
_SLICE_MEMHIGH_MUTEX = threading.Lock()


def _reconcile_slice_memory_high_off_thread() -> None:
    """Reconcile ``MemoryHigh`` and check slice throttling in a daemon thread.

    The reconciliation reads config and shells out to ``systemctl`` (up to
    10s), and its caller sits on the agent-spawn path, which runs on the
    gateway event loop — so it must never execute inline. Fire-and-forget is
    semantically safe: ``MemoryHigh`` set on a slice applies to members that
    are already running, so a reconciliation that lands moments after the
    spawn still bounds it, and every later spawn re-reconciles. The worker
    also runs :func:`_check_slice_memory_pressure`, so throttle visibility
    shares the reconcile cadence (per spawn) and its kill switch.
    """
    global _SLICE_MEMHIGH_DISABLED
    if _SLICE_MEMHIGH_DISABLED:
        return

    def _worker() -> None:
        with _SLICE_MEMHIGH_MUTEX:
            _ensure_agent_slice_memory_high()
            _check_slice_memory_pressure()

    try:
        threading.Thread(target=_worker, name="agent-slice-memhigh", daemon=True).start()
    except RuntimeError as exc:
        # Thread exhaustion. This sits on the agent-spawn path, so it must
        # never abort the spawn. Disarm like any other reconciliation
        # failure: per-scope MemoryMax still applies.
        _SLICE_MEMHIGH_DISABLED = True
        logger.warning(
            "SECURITY: could not start the MemoryHigh reconciliation thread "
            "(%s); the AGGREGATE agent memory ceiling is NOT enforced on this "
            "host — per-scope MemoryMax still applies.",
            exc,
        )


def apply_windows_resource_ceiling(pid: int) -> bool:
    """Windows counterpart to :func:`cgroup_scope_argv`, applied AFTER the spawn.

    ``cgroup_scope_argv`` bounds an agent subprocess and all its descendants by
    prepending ``systemd-run --user --scope`` with ``TasksMax`` / ``MemoryMax``.
    That has no Windows equivalent expressible as an argv prefix, so there it
    returns argv unchanged and logs a one-time loud SECURITY warning — the
    fork-bomb and memory-DoS ceilings were simply absent on that platform.

    A Job object is the native mechanism (limits cover every process in the job,
    and a member's descendants join automatically), but it must be applied to a
    live pid rather than baked into argv. Callers therefore invoke this right
    after the spawn returns, in ADDITION to the ``cgroup_scope_argv`` call they
    already make (a no-op on Windows), and while the child is still suspended —
    see :func:`platform_compat.apply_job_limits` for why that ordering is what
    makes the ceiling airtight.

    Reads the SAME ``resource_limits`` config as the cgroup path, so one operator
    setting governs both platforms.

    Returns ``True`` when a ceiling was installed; ``False`` on non-Windows
    (nothing to do — the cgroup wrapper owns it) or on any failure, which
    :func:`platform_compat.apply_job_limits` has already logged as a SECURITY
    warning. Never raises: a missing ceiling must not fail the spawn, matching
    how an unavailable cgroup scope is handled.
    """
    if not platform_compat.IS_WINDOWS:
        return False
    max_procs, max_mem_mb, _cpu_weight, _max_cpu_percent = _cgroup_limits_from_config()
    return platform_compat.apply_job_limits(
        pid,
        max_procs=max_procs,
        max_memory_bytes=max_mem_mb * 1024 * 1024,
    )


def cgroup_scope_argv(argv: list[str]) -> list[str]:
    """Wrap *argv* in a transient systemd --user --scope with cgroup v2 limits.

    Prepends ``systemd-run --user --scope`` with ``TasksMax`` (pids.max, the
    fork-bomb ceiling), ``MemoryMax`` + ``MemorySwapMax=0`` (memory.max, the
    RSS balloon ceiling), and — when the cpu controller is delegated —
    ``CPUWeight`` (proportional fair-share: agents run full speed on an idle
    host but yield to interactive work under contention; never a hard
    throttle) plus an OPT-IN ``CPUQuota`` hard cap
    (``resource_limits.max_cpu_percent``, off by default because hard quotas
    slow legitimate builds), so the spawned agent AND all its MCP-server/tool
    descendants are bounded as one cgroup and the kernel kills the scope on
    breach. ``--scope`` execs into the target (it does NOT fork a wrapper), so
    the returned argv's eventual PID is the real child — parent PID tracking,
    ``killpg``, and descendant scans are unaffected.

    Every scope is parented under a per-instance child of
    :data:`_CGROUP_AGENTS_SLICE` (see :func:`_agents_slice_name`, which explains
    why the owner has to be expressible), and the slice-level soft ceiling
    (``MemoryHigh``, see :func:`_ensure_agent_slice_memory_high`) — a
    host-derived constant (75% of RAM) — is ensured before each wrap on the
    PARENT, throttling the SUM of all concurrent agent trees before the slice's
    hard ``MemoryMax`` (:func:`ensure_agents_slice_limits`) OOM-kills a scope.
    cgroup v2 bounds a descendant by the minimum effective limit along its
    ancestor chain, so nesting one level deeper does not loosen either ceiling.

    Layers OUTSIDE the OS-level sandbox: callers pass the already-``wrap_argv``-ed
    argv here so the child is filesystem-isolated AND cgroup-bounded.

    On a host without cgroup v2 delegation (older Linux, no systemd user
    session, macOS), returns *argv* unchanged and logs a one-time loud SECURITY
    warning — the RLIMIT_NOFILE preexec still applies, but the fork-bomb/memory
    DoS ceiling is NOT enforced there. The same degradation applies when
    ``systemd-run`` resolves outside the trusted system directories: prepending
    an unpinned wrapper name would trade a DoS ceiling for an exec-hijack
    channel, which is the worse of the two.

    The returned wrapper is an ABSOLUTE path, so callers may hand the result to
    a spawn whose ``env`` carries a config-declared PATH without that PATH being
    able to redirect argv[0].
    """
    available, reason = _probe_cgroup_scope()
    if not available:
        _warn_cgroup_unavailable(reason)
        return argv
    # SECURITY: the wrapper this function prepends becomes argv[0], and a caller
    # that hands the result to a spawn with a config-influenced ``env`` has
    # CPython resolve a slash-less argv[0] through THAT env's PATH
    # (os.get_exec_path) -- so a bare name here is an exec-hijack channel that
    # runs BEFORE ``--scope`` establishes confinement. Pin it at the layer that
    # prepends it, so every caller inherits the protection rather than each
    # spawn site remembering to re-pin (the same reason ``sandbox_exec_argv``
    # pins its own wrappers). ``trusted_system_bin`` ignores PATH entirely: a
    # gateway's PATH can legitimately lead with agent-writable directories, so
    # resolving through it would leave the hole half-open. An unresolvable
    # wrapper degrades exactly like a missing cgroup backend -- no ceiling, loud
    # warning -- rather than emitting an unpinned name.
    systemd_run = platform_compat.trusted_system_bin("systemd-run")
    if not systemd_run:
        _warn_cgroup_unavailable("systemd-run is not in a trusted system directory")
        return argv
    # Reconcile the slice-level aggregate ceiling off-thread: this call site
    # runs on the gateway event loop during agent spawn, and reconciliation
    # does config reads + a systemctl subprocess. MemoryHigh on a slice
    # applies to already-running members, so the spawn need not wait for it.
    _reconcile_slice_memory_high_off_thread()
    max_procs, max_mem_mb, cpu_weight, max_cpu_percent = _cgroup_limits_from_config()
    props = [
        "-p",
        f"TasksMax={max_procs}",
        "-p",
        f"MemoryMax={max_mem_mb}M",
        "-p",
        "MemorySwapMax=0",
    ]
    # CPU properties only when the cpu controller is delegated — otherwise the
    # kernel won't enforce them and systemd may warn on every spawn.
    if _cpu_controller_delegated():
        props += ["-p", f"CPUWeight={cpu_weight}"]
        if max_cpu_percent > 0:
            props += ["-p", f"CPUQuota={max_cpu_percent}%"]
    return [
        systemd_run,
        "--user",
        "--scope",
        "-q",
        f"--slice={_agents_slice_name()}",
        *props,
        "--",
        *argv,
    ]


# ── aggregate ceiling on the parent slice ──
# The per-scope MemoryMax above bounds ONE spawn tree, but scopes are siblings:
# N concurrent spawns may collectively request N x 65% of host RAM with no
# single cgroup ever breaching its own limit. cgroup v2 enforces limits down
# the tree — a descendant is bounded by the MINIMUM effective limit of itself
# and all ancestors — so the parent slice every scope already nests under is
# the natural aggregate boundary. ensure_agents_slice_limits() puts a ceiling
# on it, yielding a two-level model: slice = aggregate across all concurrent
# agent work, scope = per-tree (unchanged).

# Aggregate memory.max as a fraction of physical RAM. Must sit ABOVE the
# per-scope fraction (0.65) — otherwise the slice would shrink a single
# spawn's existing headroom — and meaningfully below 1.0 so the OS and the
# gateway keep breathing room even when agent work saturates the ceiling.
_CGROUP_TOTAL_MEMORY_FRACTION = 0.80
# Fallback aggregate memory.max (MB) when physical RAM can't be read. Above
# the per-scope fallback (8192) for the same "never clamp a single scope
# tighter than its own ceiling" reason as the fraction.
_CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB = 12288
# Aggregate pids.max across all concurrent scopes: four fully-loaded scopes'
# worth (4 x 8192). Bounds the composition blow-up (32 scopes x 8192 tasks =
# 262144 otherwise) while still allowing several concurrent JVM-scale builds,
# each of which legitimately needs thousands of threads.
_CGROUP_DEFAULT_MAX_TOTAL_TASKS = 32768


def _default_max_total_memory_mb() -> int:
    """Default aggregate ``memory.max`` (MB) for the agents slice: a fixed
    fraction (:data:`_CGROUP_TOTAL_MEMORY_FRACTION`) of physical RAM, falling
    back to :data:`_CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB` when RAM is unreadable.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _CGROUP_TOTAL_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    return _CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB


def _slice_limits_from_config() -> tuple[int, int]:
    """Return ``(max_total_memory_mb, max_total_tasks)`` for the agents slice.

    Reads ``resource_limits.max_total_memory_mb`` / ``max_total_processes``
    from the same config block as the per-scope knobs. ``0`` or junk means
    "use default" — the aggregate ceiling is never left unset, matching the
    per-scope rule in :func:`_cgroup_limits_from_config`. The two memory knobs
    are deliberately independent of one another: per-scope answers "how big may
    one tree get", aggregate answers "how much may all trees claim together".
    """
    total_mem_mb = _default_max_total_memory_mb()
    total_tasks = _CGROUP_DEFAULT_MAX_TOTAL_TASKS
    try:
        # circular import: same constraint as _cgroup_limits_from_config —
        # config.loader consumers import sandbox, so the import stays local.
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        # Same single validated read as the per-scope knobs. This function used
        # to test ``int(m) >= 1`` directly, which raises on a NaN/Infinity that
        # json.loads happily produces — and the raise landed in the except below,
        # discarding a VALID max_total_processes set alongside a junk memory
        # value. from_raw refuses both before int() sees them, per key.
        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
        if rl.max_total_memory_mb is not None and rl.max_total_memory_mb >= 1:
            total_mem_mb = rl.max_total_memory_mb
        if rl.max_total_processes is not None and rl.max_total_processes >= 1:
            total_tasks = rl.max_total_processes
    except Exception:
        logger.debug("slice limits: config unavailable, using defaults")
    return total_mem_mb, total_tasks


_SLICE_LIMITS_APPLIED = False


def ensure_agents_slice_limits() -> bool:
    """Apply the aggregate cgroup ceiling to the agents slice. Idempotent.

    Runs ``systemctl --user set-property --runtime`` on
    :data:`_CGROUP_AGENTS_SLICE`, setting ``MemoryMax`` (aggregate across ALL
    concurrent agent scopes), ``MemorySwapMax=0`` (consistent with the
    per-scope property: a true RSS ceiling, no swap escape), and ``TasksMax``
    (aggregate fork-bomb ceiling). Called once at gateway startup.

    ``--runtime`` over a shipped unit drop-in, deliberately: the property is
    re-derived from config and re-applied on every gateway start, so a config
    change can never leave a stale on-disk artifact behind, and uninstalling
    leaves nothing to clean up. The property persists on the user manager
    until logout/reboot — long enough, since the gateway is the long-lived
    process that re-applies it.

    Gated on the same :func:`_probe_cgroup_scope` capability check as the
    per-scope wrapper: where delegation is unavailable this is skipped and the
    single shared SECURITY warning covers both layers — no second warning for
    the same host condition.

    Blocking (shells out): call off-loop (``asyncio.to_thread``).

    Returns True when the ceiling is in place (now or from an earlier call).
    """
    global _SLICE_LIMITS_APPLIED
    if _SLICE_LIMITS_APPLIED:
        return True
    available, reason = _probe_cgroup_scope()
    if not available:
        _warn_cgroup_unavailable(reason)
        return False
    total_mem_mb, total_tasks = _slice_limits_from_config()
    # PATH can legitimately lead with agent-writable directories (a worktree
    # venv's bin, ~/.local/bin), so a bare "systemctl" would let a planted
    # shim run with the gateway's environment. Resolve from fixed system
    # directories only; unavailable = ceiling not applied (per-scope ceilings
    # still hold).
    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s: no trusted "
            "systemctl binary — per-scope ceilings still apply.",
            _CGROUP_AGENTS_SLICE,
        )
        return False
    cmd = [
        systemctl,
        "--user",
        "set-property",
        "--runtime",
        _CGROUP_AGENTS_SLICE,
        f"MemoryMax={total_mem_mb}M",
        "MemorySwapMax=0",
        f"TasksMax={total_tasks}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s: %s — "
            "per-scope ceilings still apply, but N concurrent spawns may "
            "collectively exceed host RAM.",
            _CGROUP_AGENTS_SLICE,
            exc,
        )
        return False
    if proc.returncode != 0:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s (rc=%d): %s — "
            "per-scope ceilings still apply, but N concurrent spawns may "
            "collectively exceed host RAM.",
            _CGROUP_AGENTS_SLICE,
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return False
    _SLICE_LIMITS_APPLIED = True
    logger.info(
        "aggregate cgroup ceiling on %s: MemoryMax=%dM MemorySwapMax=0 TasksMax=%d "
        "(per-scope ceilings unchanged)",
        _CGROUP_AGENTS_SLICE,
        total_mem_mb,
        total_tasks,
    )
    return True


# cgroup v2 directory of the systemd user manager's subtree (transient
# --user units always live under user@<uid>.service on the unified
# hierarchy); ``{uid}`` is filled at resolve time. Module-level so tests can
# point the resolver at a fabricated tree.
_USER_MANAGER_CGROUP_BASE = "/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"


def _agents_slice_cgroup_dir() -> Path | None:
    """Resolve the agents slice's cgroup directory, or None when absent.

    systemd's dash-hierarchy places ``kirocrew-agents.slice`` under
    ``kirocrew.slice`` inside the user manager's subtree; the direct
    construction covers that. The shallow scan tolerates a manager that laid
    the slice out differently (one extra level only — never a recursive walk).
    The directory exists only while the slice is active (a runtime property or
    a live scope holds it); None simply means "no agent work to observe".
    """
    if sys.platform != "linux":
        return None
    base = Path(_USER_MANAGER_CGROUP_BASE.format(uid=os.getuid()))
    direct = base / "kirocrew.slice" / _CGROUP_AGENTS_SLICE
    if direct.is_dir():
        return direct
    try:
        for child in base.iterdir():
            cand = child / _CGROUP_AGENTS_SLICE
            if cand.is_dir():
                return cand
    except OSError:
        pass
    return None


def _read_cgroup_counters(path: Path) -> dict[str, int]:
    """Parse a ``memory.events``-style key/value cgroup file into a dict."""
    counters: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                counters[key] = int(value)
    except OSError:
        pass
    return counters


# Last-seen slice-level OOM counters, so only NEW kills are reported. Seeded
# lazily from the current values on first read: kills that predate this
# process must not fire a spurious warning at boot.
_SLICE_OOM_SEEN: dict[str, int] | None = None


def check_agents_slice_pressure() -> str | None:
    """Report (and log) new OOM kills inside the agents slice, else None.

    With an aggregate ceiling on the slice, a breach OOM-kills SOME scope in
    it — the kernel picks the victim, not necessarily the spawn that grew.
    Without attribution the operator-visible failure is "a random subagent
    died". This turns it into a diagnosable event: which scopes took kills
    (each scope's own ``memory.events.local oom_kill``), the slice's
    ``memory.current`` vs ``memory.max`` at observation time, and whether the
    SLICE ceiling itself engaged (``memory.events.local max`` on the slice —
    the discriminator between a slice-level breach and a single scope hitting
    its own per-tree limit).

    Reads a handful of cgroup files; never raises. Polled from the resource
    pressure sampler's worker thread, so it is already off-loop. The same
    poll also re-applies the slice ceiling if a user-manager restart dropped
    the --runtime property (see the self-heal block below).
    """
    global _SLICE_OOM_SEEN, _SLICE_LIMITS_APPLIED
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return None
    # Self-heal: the ceiling is a --runtime property, so a user-manager
    # restart (logout/reboot) silently drops it while the gateway keeps
    # running. This sampler already reads the slice each tick — if the
    # ceiling we applied has vanished (memory.max reads "max"), re-apply it
    # here instead of waiting for the next gateway start. Only when WE
    # applied it before: a host that never passed the delegation gate must
    # not start shelling out from the sampler.
    if _SLICE_LIMITS_APPLIED:
        try:
            if (slice_dir / "memory.max").read_text().strip() == "max":
                _SLICE_LIMITS_APPLIED = False
                ensure_agents_slice_limits()
        except OSError:
            pass
    events = _read_cgroup_counters(slice_dir / "memory.events")
    local = _read_cgroup_counters(slice_dir / "memory.events.local")
    current = {"oom_kill": events.get("oom_kill", 0), "max": local.get("max", 0)}
    if _SLICE_OOM_SEEN is None:
        _SLICE_OOM_SEEN = current
        return None
    new_kills = current["oom_kill"] - _SLICE_OOM_SEEN["oom_kill"]
    slice_max_hits = current["max"] - _SLICE_OOM_SEEN["max"]
    _SLICE_OOM_SEEN = current
    if new_kills <= 0:
        return None
    victims: list[str] = []

    def _record_if_killed(scope_dir: Path) -> None:
        scope_local = _read_cgroup_counters(scope_dir / "memory.events.local")
        if scope_local.get("oom_kill", 0) > 0:
            victims.append(scope_dir.name)

    try:
        # Scopes sit one level deeper than they used to: each instance places
        # its own under a per-instance child slice (_agents_slice_name), so
        # scanning only direct children would report "(already reaped)" for
        # every real victim. Both shapes are walked rather than just the nested
        # one, because the aggregate slice is shared host-wide: a co-resident
        # gateway still on an older build keeps putting scopes directly here,
        # and its OOM kills are still worth naming.
        for child in slice_dir.iterdir():
            if not child.is_dir():
                continue
            if child.suffix == ".scope":
                _record_if_killed(child)
            elif child.suffix == ".slice":
                for grandchild in child.iterdir():
                    if grandchild.suffix == ".scope" and grandchild.is_dir():
                        _record_if_killed(grandchild)
    except OSError:
        pass
    mem_current = -1
    try:
        mem_current = int((slice_dir / "memory.current").read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        mem_max = (slice_dir / "memory.max").read_text().strip()
    except OSError:
        mem_max = "?"
    message = (
        f"cgroup OOM kill inside {_CGROUP_AGENTS_SLICE}: {new_kills} new kill(s); "
        f"slice memory.current={mem_current} memory.max={mem_max}; "
        f"slice-level aggregate ceiling engaged: "
        f"{'yes' if slice_max_hits > 0 else 'no (a scope hit its own per-tree limit)'}; "
        f"scopes with recorded kills: {victims or '(already reaped)'}"
    )
    logger.warning("%s", message)
    return message


# ``systemd-run --user`` finds the caller's session bus through these two
# variables. They hold a socket path owned by the current user, not a
# credential, and they are a dependency of the WRAPPER rather than of the
# sandboxed child — which is why they are restored after the credential scrub
# and then dropped again inside the scope (see :func:`_unset_env_argv`) instead
# of being added to any caller's env allowlist.
_CGROUP_SCOPE_BUS_ENV_KEYS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
# Absolute paths only: the shim that drops the locators again must not be
# resolvable through a caller- or agent-influenced PATH.
_ENV_BINARY_CANDIDATES = ("/usr/bin/env", "/bin/env")


def _unset_env_argv(keys: tuple[str, ...]) -> list[str] | None:
    """Return an ``env -u KEY …`` prefix dropping *keys*, or None if impossible.

    ``env`` ``exec``s its target in place (it does not fork), so prepending this
    inside the scope leaves PID tracking, ``killpg`` and descendant scans intact
    — the eventual PID is still the real child.

    Returns None when no ``env`` binary exists at a trusted absolute path, which
    callers must treat as "do not forward the bus locators at all".
    """
    for candidate in _ENV_BINARY_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            argv = [candidate]
            for key in keys:
                argv += ["-u", key]
            return argv
    return None


def cgroup_scope_bus_env(env: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return ``(env_with_bus_locators, keys_this_call_added)``.

    Only applied when :func:`cgroup_scope_argv` actually wraps the spawn (same
    :func:`_probe_cgroup_scope` gate), so hosts that never see a ``systemd-run``
    prefix keep the exact environment their caller asked for and the returned
    key tuple is empty.

    Values are taken from the gateway's own environment and only fill keys the
    caller left unset — an explicit value in *env* always wins and is NOT
    reported as injected, so a caller that deliberately passes a bus address
    keeps it end to end. The probe requires ``XDG_RUNTIME_DIR`` in
    ``os.environ``, so whenever wrapping is applied at least that locator is
    available to forward.

    The returned key tuple is what lets the caller drop exactly what it added
    again *inside* the scope: the locators must reach ``systemd-run``, but must
    not reach the sandboxed child — a live user-bus address there can be used to
    ask the user systemd manager to start a unit that runs outside the sandbox.

    Without this, a caller that builds its child environment from a strict
    allowlist (``dashboard/handlers/source_providers.py`` is the live example)
    hands ``systemd-run`` an environment with no reachable bus; it exits 1 with
    ``Failed to connect to bus: No medium found`` and the wrapped command never
    runs at all.
    """
    available, _ = _probe_cgroup_scope()
    if not available:
        return env, ()
    patched = dict(env)
    injected: list[str] = []
    for key in _CGROUP_SCOPE_BUS_ENV_KEYS:
        if patched.get(key):
            continue
        value = os.environ.get(key)
        if value:
            patched[key] = value
            injected.append(key)
    return patched, tuple(injected)


# Cached preexec_fn shared by every agent-influenced spawn. Built once from the
# loaded config (limits are process-global, not per-spawn) so the hot path adds
# nothing but a dict lookup. ``_UNSET`` distinguishes "not built yet" from the
# legitimate ``None`` result on non-POSIX platforms.
_UNSET = object()
_RESOURCE_PREEXEC: object = _UNSET


def resource_limit_preexec() -> "Callable[[], None] | None":
    """Return the shared ``preexec_fn`` that caps a spawned child's resources.

    This is the companion to :func:`sandboxed_spawn_argv`: the sandbox wrapper
    gives a child filesystem + credential isolation, and this gives it a
    kernel-enforced ceiling on processes / file descriptors / CPU / memory so a
    fork bomb or runaway allocation in a compromised tool or MCP server cannot
    exhaust the host out from under the gateway. Call sites do not use this
    directly: agent-influenced spawns go through
    :func:`create_subprocess_limited` / :func:`run_limited` /
    :func:`popen_limited`, which deliver the same limits AFTER ``exec`` via the
    spawn shim and fall back to this ``preexec_fn`` only on a host with no
    usable shim (see ``docs/architecture/resource-protection.md``).

    Returns the callable from :func:`kiro_crew.security.apply_resource_limits`,
    or ``None`` on non-POSIX platforms (where there is nothing to enforce and
    ``preexec_fn`` must be ``None``). The callable and the underlying config
    read are computed once and cached — the limits are a host-global policy, not
    a per-spawn decision.
    """
    global _RESOURCE_PREEXEC
    if _RESOURCE_PREEXEC is _UNSET:
        if os.name != "posix":
            # Non-POSIX (Windows): preexec_fn is unsupported by
            # create_subprocess_exec and MUST be None — passing any callable
            # (even a no-op) raises ValueError. Cache None to honor the return
            # contract. (apply_resource_limits also no-ops there, but it returns
            # a callable, so we must not forward it.)
            _RESOURCE_PREEXEC = None
            return None
        # Lazy imports: sandbox is a low-level module (see the SEL import note in
        # wrap_argv) and must not import config/security at module load.
        from kiro_crew.security import apply_resource_limits

        cfg: dict | None = None
        try:
            # Raw config.json (process-cached) — carries the unrecognized
            # ``resource_limits`` key an operator may add; the typed config
            # schema drops unknown keys, so read the raw dict here.
            from kiro_crew.config.loader import _raw_config

            cfg = _raw_config()
        except Exception:
            # Config unavailable (early boot, tests) — apply_resource_limits
            # falls back to its safe built-in defaults.
            logger.debug("resource_limit_preexec: config unavailable, using defaults")
        # POSIX: apply_resource_limits returns a callable (a no-op only when
        # every limit is disabled). Cache it; passing a no-op preexec_fn is fine.
        _RESOURCE_PREEXEC = apply_resource_limits(cfg)
    return _RESOURCE_PREEXEC  # type: ignore[return-value]


# Cached ``--rlimits=`` argv fragment for the process-group supervisor. Same
# policy as ``resource_limit_preexec``, delivered post-exec instead of post-fork.
_RESOURCE_SUPERVISOR_ARGV: object = _UNSET


def resource_limit_supervisor_argv() -> "tuple[str, ...]":
    """Return the supervisor's ``--rlimits=`` argv fragment (empty if none apply).

    The alternative to :func:`resource_limit_preexec` for the one spawn that
    already prepends ``_process_group_supervisor.py``. Passing ``preexec_fn=``
    forces CPython to ``fork()`` the multi-GB, ~118-thread gateway and run Python
    in the child before ``exec``; a lock another thread held at fork time is
    unreleasable there, and that is how a child deadlocked in a futex, never
    exec'd, and pinned every fd it had inherited. Handing the limits to the
    supervisor moves the same ``setrlimit`` calls after ``exec``, where the
    process is single-threaded, and the exec'd child inherits them either way.

    The values are policy numbers, not secrets, so argv (world-readable via
    ``ps``) is a fine channel.
    """
    global _RESOURCE_SUPERVISOR_ARGV
    if _RESOURCE_SUPERVISOR_ARGV is _UNSET:
        if os.name != "posix":
            _RESOURCE_SUPERVISOR_ARGV = ()
            return ()
        from kiro_crew.security import resource_limit_spec

        cfg: dict | None = None
        try:
            from kiro_crew.config.loader import _raw_config

            cfg = _raw_config()
        except Exception:
            logger.debug("resource_limit_supervisor_argv: config unavailable, using defaults")
        spec = ",".join(f"{name}:{value}" for name, value in resource_limit_spec(cfg))
        _RESOURCE_SUPERVISOR_ARGV = (f"--rlimits={spec}",) if spec else ()
    return _RESOURCE_SUPERVISOR_ARGV  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Session host preexec — the inverse of resource_limit_preexec.
# ---------------------------------------------------------------------------

_SESSION_HOST_PREEXEC: object = _UNSET


def session_host_preexec() -> "Callable[[], None] | None":
    """Return a ``preexec_fn`` that *raises* NOFILE for a session host process.

    Session hosts (kiro-cli-chat / claude-agent-acp) are **trusted** internal
    processes — they manage a tree of MCP server subprocesses, each consuming
    pipe fd pairs for stdin/stdout communication.  A single session host may
    hold 100-200 fds under normal operation (10+ MCP servers × pipe pairs +
    sockets + log files).

    The default ``resource_limit_preexec()`` caps NOFILE at 1024 to defend
    against compromised *tool* processes, but applying the same cap to the
    trusted session host causes "Too many open files" crashes when subagent
    concurrency or MCP server count is high.

    This preexec raises NOFILE soft+hard to the *gateway's* inherited hard
    limit (typically 10240 from the systemd unit, or 524288 kernel max) so
    the session host has headroom proportional to the gateway itself.  Other
    resource limits (NPROC, CPU, AS) are left at their sandbox values — a
    session host has no legitimate reason to fork-bomb or allocate unbounded
    memory.

    Returns ``None`` on non-POSIX platforms (preexec_fn must be None there).
    """
    global _SESSION_HOST_PREEXEC
    if _SESSION_HOST_PREEXEC is _UNSET:
        if os.name != "posix" or _resource_mod is None:
            _SESSION_HOST_PREEXEC = None
            return None

        res = _resource_mod

        def _raise_nofile() -> None:
            """Raise NOFILE to the hard limit in the child process."""
            try:
                _soft, hard = res.getrlimit(res.RLIMIT_NOFILE)
                if hard == res.RLIM_INFINITY:
                    # Kernel allows unlimited — cap at a sane maximum but never
                    # reduce below the inherited soft limit.
                    target = max(_soft, 65536)
                else:
                    target = hard
                res.setrlimit(res.RLIMIT_NOFILE, (target, hard))
            except (ValueError, OSError):
                pass  # Leave inherited — better than failing the spawn.

        _SESSION_HOST_PREEXEC = _raise_nofile
    return _SESSION_HOST_PREEXEC  # type: ignore[return-value]


# Build workloads (vite/npm/pip) legitimately hold thousands of descriptors —
# the default 1024 NOFILE ceiling EMFILEs them while still being the right cap
# for one-shot tools. Same policy, higher finite descriptor ceiling; every
# other limit still comes from the operator config. Cached like the default.
_BUILD_NOFILE_CEILING = 65536
_BUILD_RESOURCE_PREEXEC: object = _UNSET


def build_resource_limit_preexec() -> "Callable[[], None] | None":
    """``resource_limit_preexec`` variant for build-class children.

    Identical policy except ``max_open_files`` is raised to a still-finite
    65536 (matching the gateway service's own ``LimitNOFILE``); an operator
    ``resource_limits.max_open_files`` override higher than the default wins.
    """
    global _BUILD_RESOURCE_PREEXEC
    if _BUILD_RESOURCE_PREEXEC is _UNSET:
        if os.name != "posix":
            _BUILD_RESOURCE_PREEXEC = None
            return None
        from kiro_crew.security import apply_resource_limits

        cfg: dict | None = None
        try:
            from kiro_crew.config.loader import _raw_config

            cfg = dict(_raw_config() or {})
        except Exception:
            cfg = {}
        raw_limits = (cfg or {}).get("resource_limits")
        limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
        # Malformed operator values must not break the spawn — the shared parse
        # ignores anything out of domain and returns None, which floors to the
        # build ceiling here. Going through it keeps this path from being a
        # second rule for a key security.resource_limit_spec also reads.
        from kiro_crew.config.loader import ResourceLimitsConfig

        configured = ResourceLimitsConfig.from_raw(raw_limits).max_open_files or 0
        limits["max_open_files"] = max(configured, _BUILD_NOFILE_CEILING)
        _BUILD_RESOURCE_PREEXEC = apply_resource_limits({**(cfg or {}), "resource_limits": limits})
    return _BUILD_RESOURCE_PREEXEC  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Post-exec resource limits (the replacement for ``preexec_fn=``)
# ---------------------------------------------------------------------------

_SPAWN_SHIM_SOURCE = str(Path(__file__).with_name("_spawn_exec_shim.py"))
try:
    # Captured once, at gateway import, and passed to the interpreter as a ``-c``
    # source string. Loading it from the package path at SPAWN time would let a
    # same-UID agent rewrite the file between capture and use.
    _SPAWN_SHIM_CODE = Path(_SPAWN_SHIM_SOURCE).read_text(encoding="utf-8")
except OSError:  # pragma: no cover - only if the install is truncated
    _SPAWN_SHIM_CODE = ""

# Resource-limit profiles. ``tool`` is the default for every agent-influenced
# spawn; the others exist because a single policy cannot serve a one-shot tool, a
# build, a session host, and the user's own terminal at once. Each one is a
# faithful port of the ``preexec_fn`` variant it replaces -- including which ones
# bias the OOM killer, which is not uniform across them.
RLIMIT_PROFILE_TOOL = "tool"
RLIMIT_PROFILE_BUILD = "build"
RLIMIT_PROFILE_SESSION_HOST = "session_host"
# No limits and no OOM bias: the interactive terminal is the user's own shell,
# not agent-executed code, and never carried either.
RLIMIT_PROFILE_NONE = "none"

# profile -> (biases the OOM killer, legacy preexec accessor name)
_PROFILE_OOM_BIAS = {
    RLIMIT_PROFILE_TOOL: True,
    RLIMIT_PROFILE_BUILD: True,
    # session_host_preexec raises NOFILE and does nothing else -- notably it does
    # NOT bias the OOM score, and a trusted session host should not be the
    # preferred kill target.
    RLIMIT_PROFILE_SESSION_HOST: False,
    RLIMIT_PROFILE_NONE: False,
}

# The shim's own argv contract, mirrored here so a cached prefix can be extended
# for one spawn. Kept as literals rather than imported from the shim module: the
# shim is consumed as a source string captured at import time, never imported from
# the (agent-writable) package directory at spawn time.
_SHIM_ARGV_SEPARATOR = "--"
_SHIM_CHDIR_FD_FLAG = "--chdir-fd="

_SHIM_ARGV_CACHE: dict[str, tuple[str, ...]] = {}
_SHIM_UNAVAILABLE_LOGGED = False


def _rlimit_spec(profile: str) -> str:
    """Build the shim's ``--rlimits=`` payload for *profile*.

    The values are policy numbers, not secrets, so argv (world-readable through
    ``ps``) is a fine channel for them.
    """
    from kiro_crew.security import resource_limit_spec

    if profile == RLIMIT_PROFILE_NONE:
        return ""
    if profile == RLIMIT_PROFILE_SESSION_HOST:
        # Faithful port of session_host_preexec: it touches NOFILE only, and
        # deliberately leaves NPROC/CPU/AS inherited. A session host multiplexes
        # pipe pairs for a whole tree of MCP servers, and the tool-grade 1024 cap
        # EMFILE-crashed it.
        return "RLIMIT_NOFILE:hard"

    cfg: dict | None = None
    try:
        from kiro_crew.config.loader import _raw_config

        cfg = _raw_config()
    except Exception:
        logger.debug("_rlimit_spec: config unavailable, using defaults")
    if profile == RLIMIT_PROFILE_BUILD:
        raw_limits = (cfg or {}).get("resource_limits")
        limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
        # Same shared parse as the post-fork build path above, so the two
        # spellings of "raise the build NOFILE floor" cannot drift apart.
        from kiro_crew.config.loader import ResourceLimitsConfig

        configured = ResourceLimitsConfig.from_raw(raw_limits).max_open_files or 0
        limits["max_open_files"] = max(configured, _BUILD_NOFILE_CEILING)
        cfg = {**(cfg or {}), "resource_limits": limits}
    return ",".join(f"{name}:{value}" for name, value in resource_limit_spec(cfg))


def spawn_shim_argv(profile: str = RLIMIT_PROFILE_TOOL) -> tuple[str, ...]:
    """Return the argv prefix that applies *profile*'s policy AFTER ``exec``.

    Prepend it to a command and pass ``preexec_fn=None``; the shim replaces
    itself with the command, so PID, process group, inherited fds, and exit
    status all stay the command's own. See ``_spawn_exec_shim.py`` for why this
    cannot ride on ``preexec_fn``: that forks this multi-threaded gateway and runs
    Python in the child, where a wedged child blocks the spawning thread inside
    ``Popen`` and pins every fd it inherited.

    Returns an empty tuple when there is nothing for a shim to do -- on Windows
    (no POSIX rlimits), for a profile that asks for nothing, or if the shim source
    could not be captured. An empty result on a profile that DOES carry policy
    means the caller must fall back to ``preexec_fn`` rather than drop it.
    """
    global _SHIM_UNAVAILABLE_LOGGED
    if os.name != "posix":
        return ()
    key = profile
    cached = _SHIM_ARGV_CACHE.get(key)
    if cached is not None:
        return cached
    if not _SPAWN_SHIM_CODE or not sys.executable:
        if not _SHIM_UNAVAILABLE_LOGGED:
            _SHIM_UNAVAILABLE_LOGGED = True
            logger.warning(
                "post-exec spawn shim unavailable (source_captured=%s, executable=%r); "
                "falling back to preexec_fn",
                bool(_SPAWN_SHIM_CODE),
                sys.executable,
            )
        return ()
    spec = _rlimit_spec(profile)
    bias = _PROFILE_OOM_BIAS.get(profile, True)
    if not spec and not bias:
        # Nothing to do post-exec: skip the interpreter hop entirely rather than
        # pay ~10ms to exec a shim that would only exec again.
        _SHIM_ARGV_CACHE[key] = ()
        return ()
    argv = [sys.executable, "-I", "-S", "-c", _SPAWN_SHIM_CODE]
    if spec:
        argv.append(f"--rlimits={spec}")
    if bias:
        argv.append("--oom-bias")
    argv.append(_SHIM_ARGV_SEPARATOR)
    resolved = tuple(argv)
    _SHIM_ARGV_CACHE[key] = resolved
    return resolved


def _shim_prefix_entering_fd(prefix: "tuple[str, ...]", descriptor: int) -> "tuple[str, ...]":
    """Return *prefix* with ``--chdir-fd`` inserted ahead of its argv separator.

    Copied rather than mutated: the prefix is cached per profile, while the
    descriptor belongs to a single spawn.
    """
    if not prefix or prefix[-1] != _SHIM_ARGV_SEPARATOR:
        raise RuntimeError("spawn shim prefix is missing its argv separator")
    return prefix[:-1] + (f"{_SHIM_CHDIR_FD_FLAG}{descriptor}", _SHIM_ARGV_SEPARATOR)


def _pass_fds_including(passed: Any, descriptor: int) -> "tuple[int, ...]":
    """Return *passed* with *descriptor* inherited, leaving its order alone.

    The shim can only ``fchdir`` a descriptor the child actually holds, and
    ``pass_fds`` is what carries it there: it exempts the fd from
    ``_close_open_fds`` and clears the ``O_CLOEXEC`` the binder opens with. Owned
    here rather than left to each caller so the flag and the inheritance cannot
    drift apart.
    """
    existing = tuple(passed or ())
    if descriptor in existing:
        return existing
    return existing + (descriptor,)


def _preexec_for_profile(profile: str) -> "Callable[[], None] | None":
    """Legacy ``preexec_fn`` for *profile*, used only when the shim is missing."""
    if profile == RLIMIT_PROFILE_NONE:
        return None
    if profile == RLIMIT_PROFILE_SESSION_HOST:
        return session_host_preexec()
    if profile == RLIMIT_PROFILE_BUILD:
        return build_resource_limit_preexec()
    return resource_limit_preexec()


def _resolve_spawn_target(
    argv: "Sequence[str]", env: "Mapping[str, str] | None", cwd: Any = None
) -> str:
    """Resolve a bare command NAME against the child's ``PATH``.

    The shim ``execv``s without a PATH search, so a command given as a bare name
    has to be resolved by someone. It is resolved HERE, in the gateway, on
    purpose: the call sites that vet an executable (provider allowlists, binary
    trust checks) do it in the parent, and letting the shim run its own search
    would add a second resolution path that nothing vetted.

    Resolving here also keeps the contract call sites already depend on -- a
    command that is not on ``PATH`` raises ``FileNotFoundError`` from the spawn
    itself, exactly as ``Popen`` raised it when the child's ``execvpe`` failed.

    This touches the filesystem (``shutil.which`` stats each ``PATH`` entry), so
    the caller runs it in a worker thread: a stalled NFS/autofs ``PATH`` entry
    would otherwise block the event loop, which the child-side search never did.

    ``PATH`` comes from the child's own environment, matching ``Popen``'s
    ``os.get_exec_path(env)``. A relative ``PATH`` entry is resolved against the
    child's *cwd* for the same reason: that is where ``execvpe`` would have looked
    from, not where the gateway happens to be running.
    """
    name = argv[0]
    if os.sep in name or (os.altsep and os.altsep in name):
        # An explicit path: exec resolves it (against cwd when relative), and
        # stat-ing it here would only pre-empt a failure exec reports anyway.
        return name
    search_path = (env or os.environ).get("PATH") or os.defpath
    if cwd:
        base = os.fspath(cwd)
        search_path = os.pathsep.join(
            entry if os.path.isabs(entry) else os.path.join(base, entry)
            for entry in search_path.split(os.pathsep)
        )
    found = shutil.which(name, path=search_path)
    if not found:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
    return found


def _pinned_spawn_path(
    env: "Mapping[str, str] | None", *, chdir_fd: int | None = None
) -> "dict[str, str]":
    """A copy of *env* whose ``PATH`` keeps only entries safe under a pinned cwd.

    For resolving a command when the child's working directory is pinned by
    descriptor. Two screens, cheapest first:

    * **Lexical** -- only absolute entries survive. A relative entry (``''``,
      ``.``, ``tools``) is resolved against the pinned directory, which is the
      one place the pin says not to trust by name.
    * **Identity** (when *chdir_fd* is given) -- an absolute entry that IS the
      pinned directory, or lives anywhere beneath it, is dropped too.
      ``PATH=/home/me/.kiro/crew/workspace/bin:/usr/bin`` passes the lexical
      screen unchanged, yet a binary planted behind such an entry wins the
      child's own later lookup the moment the shim has entered the pinned
      directory. Entries are compared by ``(st_dev, st_ino)`` ancestry walked
      over descriptors -- never by pathname -- so a symlink or other alias of
      the pinned directory cannot dodge the screen. A kept entry is emitted as
      the OPENED descriptor's own canonical path, never the caller's spelling:
      the child re-resolves its ``PATH`` strings later, so a spelling that
      traverses a retargetable symlink could be pointed somewhere else between
      this screen and that lookup. An entry that cannot be opened, walked, or
      re-spelled is dropped, fail-closed per entry: an unopenable entry cannot
      contribute a resolvable binary today, and dropping is the direction that
      cannot be gamed by making a directory un-``stat``-able.

    When the BOUND descriptor's own identity cannot be read there is nothing to
    compare entries against, so the lexical screen stands alone for that spawn.
    That is a deliberate degrade, not a silent fallback: in production
    ``chdir_fd`` always originates from a real opened directory descriptor, and
    one that cannot be ``fstat``-ed is one the shim's own ``fchdir`` rejects
    before any command runs.

    Dropping entries can leave ``PATH`` empty, and that is the intended outcome
    -- the resolve then raises ``FileNotFoundError`` exactly as an unresolvable
    command already did, rather than silently searching somewhere else.
    """
    source = dict(env if env is not None else os.environ)
    raw = source.get("PATH") or os.defpath
    entries = [entry for entry in raw.split(os.pathsep) if entry and os.path.isabs(entry)]
    bound_identity: "tuple[int, int] | None" = None
    if chdir_fd is not None:
        try:
            bound_info = os.fstat(chdir_fd)
        except OSError:
            bound_identity = None
        else:
            bound_identity = (bound_info.st_dev, bound_info.st_ino)
    if bound_identity is not None:
        # Local import: hooks imports sandbox at call time, so a module-level
        # dependency would be circular. `_fd_real_path` is private but already
        # borrowed this way by `bound_agent_workspace_target` above; issue
        # #6907 tracks promoting it to a shared home.
        from kiro_crew.hooks import _fd_real_path

        screened: list[str] = []
        for entry in entries:
            try:
                entry_fd = _open_directory_descriptor(entry)
            except OSError:
                continue
            try:
                ancestors = _directory_ancestor_identities(entry_fd)
                if bound_identity in ancestors:
                    # The walk yields the entry's OWN identity first, so one
                    # membership test covers both "the entry IS the pinned
                    # directory" and "the entry lives beneath it".
                    continue
                # Keep the OPENED descriptor's own canonical path, never the
                # caller's spelling. The child re-resolves whatever string ends
                # up in its PATH, so a kept spelling that traverses a symlink
                # could be retargeted between this screen and that lookup --
                # the identity verified here must be the identity the child
                # reaches. A canonical path has no symlink components, and one
                # inside the pinned directory cannot exist here (its target
                # would have failed the ancestry test above). Unresolvable ==
                # dropped: falling back to the mutable spelling would reopen
                # the window this screen exists to close.
                resolved_entry = _fd_real_path(entry_fd)
            except OSError:
                continue
            finally:
                os.close(entry_fd)
            if resolved_entry is None:
                continue
            screened.append(resolved_entry)
        entries = screened
    source["PATH"] = os.pathsep.join(entries)
    return source


def _needs_path_search(argv: "Sequence[str]") -> bool:
    """Whether ``argv[0]`` is a bare name, i.e. whether resolution touches disk."""
    name = argv[0]
    return not (os.sep in name or (os.altsep and os.altsep in name))


async def create_subprocess_limited(
    *argv: str,
    profile: str = RLIMIT_PROFILE_TOOL,
    chdir_fd: int | None = None,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec`` with resource limits applied post-exec.

    The drop-in replacement for ``create_subprocess_exec(..., preexec_fn=
    resource_limit_preexec())``. Every keyword argument is forwarded untouched
    except ``preexec_fn``, which this owns: passing one would reintroduce the fork
    hazard the shim exists to remove, so it is refused.

    The returned ``Process`` describes the command itself, not a wrapper -- the
    shim ``exec``s in place -- so ``pid``, ``returncode``, signal delivery, and
    ``platform_compat.kill_process_tree`` all behave as they did before.

    ``chdir_fd`` pins the child's working directory to a directory IDENTITY
    rather than to a name: the descriptor is inherited, the shim ``fchdir``s into
    it and closes it, and only then is the command exec'd. Callers pass it when a
    pathname re-resolved in the child could be retargeted between the check and
    the chdir. It is deliberately not spelled ``cwd="/dev/fd/<n>"`` -- that is a
    Linux-only trick, and macOS refuses ``chdir()`` on those entries (``EACCES`` or
    ``ENOTDIR`` depending on the OS version). It needs the shim, and is refused rather than quietly downgraded
    to ``cwd``'s pathname when the shim is missing: entering a name nobody
    re-verified would reopen the window the descriptor exists to close.

    Setting it also DROPS ``cwd`` from the spawn, since ``Popen`` would otherwise
    chdir that pathname in the fork child before the shim ever runs, and screens
    ``PATH`` by directory IDENTITY -- for the search that resolves a bare command
    name here AND for the child's own environment. Relative entries are dropped
    (``execvpe`` resolved them against the child's cwd, the directory this
    descriptor exists to distrust), and so is any absolute entry that is the
    pinned directory itself or lives beneath it, compared by ``(st_dev, st_ino)``
    ancestry rather than by pathname. Resolving ``argv[0]`` is not the last
    lookup that happens: the wrapper this spawns looks its own target up on
    ``PATH`` after the shim has entered that directory. ``PATH=.:/usr/bin`` --
    or the same directory spelled absolutely -- would otherwise exec a binary
    out of the agent's own workspace, ahead of the sandbox meant to contain it.
    """
    if "preexec_fn" in kwargs:
        raise TypeError(
            "create_subprocess_limited owns preexec_fn: limits are applied "
            "post-exec by the spawn shim, not post-fork"
        )
    if not argv:
        raise ValueError("create_subprocess_limited requires a command")
    prefix = spawn_shim_argv(profile)
    if not prefix:
        if chdir_fd is not None:
            raise RuntimeError(
                "a descriptor-pinned working directory requires the post-exec "
                "spawn shim; refusing to enter an unverified pathname instead"
            )
        # No shim (Windows, a no-op profile, or a truncated install): keep
        # whatever policy the profile carries on the legacy fork path. Dropping
        # the caps silently would be worse than the fork hazard.
        return await asyncio.create_subprocess_exec(
            *argv, preexec_fn=_preexec_for_profile(profile), **kwargs
        )
    search_cwd = kwargs.get("cwd")
    search_env = kwargs.get("env")
    if chdir_fd is not None:
        prefix = _shim_prefix_entering_fd(prefix, chdir_fd)
        kwargs["pass_fds"] = _pass_fds_including(kwargs.get("pass_fds"), chdir_fd)
        # THE INVARIANT: while the cwd is pinned by descriptor, NO resolution of a
        # program name -- not the one below, and not one the child performs later --
        # may consult a relative PATH entry, the pinned directory, or anything
        # inside it. Three things enforce it together, and each was a hole on its
        # own:
        #
        # (a) `cwd` leaves the spawn. ``Popen`` chdirs it in the fork child BEFORE it
        #     execs the shim, so leaving it in place would resolve the very pathname
        #     the descriptor exists to bypass -- and fail the spawn outright
        #     (EACCES/ENOENT/ENOTDIR) if that name was removed or retargeted since the
        #     bind, with the pinned descriptor never reached.
        # (b) The search below gets no cwd and a PATH screened by directory IDENTITY:
        #     relative entries are dropped, and so is any absolute entry that IS the
        #     pinned directory or lives beneath it -- compared by (st_dev, st_ino)
        #     ancestry, so an alias cannot dodge it; kept entries are re-spelled from
        #     the verified descriptor, so a retargetable symlink in the caller's
        #     spelling cannot redirect the child's later lookup. A bare name IS the
        #     normal shape here -- the macOS sandbox wrapper hands back "env" as
        #     argv[0] and the Linux cgroup wrapper hands back "systemd-run" -- so the
        #     search cannot simply be refused, and `execvpe` resolved a relative entry
        #     against the child's cwd, i.e. the pinned workspace. An absolute entry
        #     pointing INTO that workspace reaches the same binary by a different
        #     spelling.
        # (c) The CHILD gets that same screened PATH. Resolving argv[0] here is
        #     not the last resolution that happens: `env` looks `sandbox-exec` up on
        #     PATH itself, inside the child, after the shim has already entered the
        #     workspace. Narrowing only (b) left `PATH=.:/usr/bin` exec'ing a
        #     `sandbox-exec` the agent dropped in its own workspace -- ahead of the
        #     sandbox that was supposed to contain it. One sanitized PATH, used for
        #     both, is what makes the invariant hold rather than move down a level.
        #
        # Those two wrapper names are spelled in prose on purpose: test_spawn_audit
        # matches its routed-through-the-sandbox tokens against this function's raw
        # source, comments included, so writing either identifier here would make the
        # spawn chokepoint read as if it routed on its own behalf.
        kwargs.pop("cwd", None)
        pinned_env = search_env

        def _screened_spawn_plan() -> "tuple[dict[str, str], str]":
            # One worker-thread hop covers the identity screen AND the resolve:
            # the screen opens and walks PATH entries and the resolve stats
            # them, so a stalled NFS/autofs entry would block either one, and
            # neither may freeze the event loop. Returning the screened env
            # alongside the resolved target keeps clauses (b) and (c) fed from
            # the SAME value by construction.
            screened = _pinned_spawn_path(pinned_env, chdir_fd=chdir_fd)
            if _needs_path_search(argv):
                return screened, _resolve_spawn_target(argv, screened, None)
            return screened, argv[0]

        search_env, resolved = await asyncio.to_thread(_screened_spawn_plan)
        kwargs["env"] = search_env
    elif not _needs_path_search(argv):
        # Explicit path: nothing to resolve, so no filesystem access and no
        # thread hop -- exec does the work.
        resolved = argv[0]
    else:
        # A PATH search stats every entry, so it runs off the loop. One stalled
        # NFS/autofs entry would otherwise freeze the gateway -- and the search it
        # replaces used to happen in the child, never in this process.
        resolved = await asyncio.to_thread(_resolve_spawn_target, argv, search_env, search_cwd)
    return await asyncio.create_subprocess_exec(
        *prefix, resolved, *argv[1:], preexec_fn=None, **kwargs
    )


def _prepare_limited_spawn(
    argv: "Sequence[str]", profile: str, kwargs: "dict[str, Any]", caller: str
) -> "tuple[list[str], Callable[[], None] | None]":
    """Resolve *argv* into the command to spawn plus the ``preexec_fn`` to pass.

    Shared by :func:`run_limited` and :func:`popen_limited`, which differ only in
    which ``subprocess`` entry point they hand the result to.

    Two things make this the sync twin of :func:`create_subprocess_limited`
    rather than a copy of it:

    * The PATH search runs INLINE. The async wrapper hops to a worker thread
      because ``shutil.which`` stats every ``PATH`` entry and one stalled
      NFS/autofs mount would freeze the event loop. A synchronous caller is
      already off the loop, so the hop would buy nothing and cost a thread.
    * ``shell=True`` is refused. The shim ``exec``s an argv vector, so there is
      no correct place to put a prefix in front of a shell command STRING;
      wrapping it anyway would change what the shell parses.
    """
    if "preexec_fn" in kwargs:
        raise TypeError(
            f"{caller} owns preexec_fn: limits are applied post-exec by the "
            "spawn shim, not post-fork"
        )
    if kwargs.get("shell"):
        raise TypeError(
            f"{caller} cannot wrap shell=True: the shim prefixes an argv "
            "vector, and a shell command is a single string"
        )
    if not argv:
        raise ValueError(f"{caller} requires a command")
    prefix = spawn_shim_argv(profile)
    if not prefix:
        # No shim (Windows, a no-op profile, or a truncated install): keep
        # whatever policy the profile carries on the legacy fork path. Dropping
        # the caps silently would be worse than the fork hazard.
        return list(argv), _preexec_for_profile(profile)
    if not _needs_path_search(argv):
        # Explicit path: exec resolves it, so stat-ing it here would only
        # pre-empt a failure exec reports anyway.
        resolved = argv[0]
    else:
        resolved = _resolve_spawn_target(argv, kwargs.get("env"), kwargs.get("cwd"))
    return [*prefix, resolved, *argv[1:]], None


def run_limited(
    argv: "Sequence[str]",
    *,
    profile: str = RLIMIT_PROFILE_TOOL,
    **kwargs: "Any",
) -> "subprocess.CompletedProcess[Any]":
    """``subprocess.run`` with resource limits applied AFTER ``exec``.

    The synchronous counterpart of :func:`create_subprocess_limited`, and the
    drop-in replacement for ``subprocess.run(..., preexec_fn=
    resource_limit_preexec())``. Every keyword argument is forwarded untouched
    except ``preexec_fn``, which this owns.

    A synchronous spawn wedges the calling worker thread rather than the event
    loop, so it does not take the whole gateway down the way the async hazard
    did -- but it is the same ``fork()`` of the same multi-GB, ~118-thread
    process, and the child still inherits a duplicate of every open fd until it
    ``exec``s. Taking ``preexec_fn`` out of the picture removes both.

    ``CompletedProcess.args`` and the ``cmd`` of a ``CalledProcessError`` /
    ``TimeoutExpired`` are the command's own argv, not the shim's. That is
    maintained here rather than free: the shim source rides in argv as a ~8 KB
    ``-c`` string, and both exceptions render ``cmd`` into their message, so
    reporting the spawned argv would put the whole shim in every failure log
    line.
    """
    cmd, preexec = _prepare_limited_spawn(argv, profile, kwargs, "run_limited")
    reported = list(argv)
    try:
        result = subprocess.run(cmd, preexec_fn=preexec, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        exc.cmd = reported
        raise
    result.args = reported
    return result


def popen_limited(
    argv: "Sequence[str]",
    *,
    profile: str = RLIMIT_PROFILE_TOOL,
    **kwargs: "Any",
) -> "subprocess.Popen[Any]":
    """``subprocess.Popen`` with resource limits applied AFTER ``exec``.

    Same contract as :func:`run_limited`, for callers that need the handle
    rather than the result -- a long-running child they will ``communicate()``
    with, poll, or signal later.

    The returned ``Popen`` is the command's own process, not a wrapper's, so
    ``pid``, ``returncode``, signal delivery, and
    ``platform_compat.kill_process_tree`` behave as they did before.

    ``Popen.args`` is reset to the command's own argv for the same reason
    :func:`run_limited` rewrites ``cmd``: ``communicate(timeout=...)`` builds its
    ``TimeoutExpired`` from ``self.args``, so leaving the shim there would put
    ~8 KB of shim source into the timeout message. Nothing in CPython reads
    ``self.args`` functionally -- only ``__repr__`` and that exception.
    """
    cmd, preexec = _prepare_limited_spawn(argv, profile, kwargs, "popen_limited")
    proc = subprocess.Popen(cmd, preexec_fn=preexec, **kwargs)
    proc.args = list(argv)
    return proc
