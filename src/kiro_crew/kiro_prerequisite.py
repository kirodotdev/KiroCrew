"""Cross-platform Kiro CLI readiness detection.

The public KiroCrew provider is KiroACP-only, so a healthy, authenticated
``kiro-cli`` is a hard runtime prerequisite. This module's job is to answer one
question about the gateway host: **is there a Kiro CLI that runs, and is it
signed in?** It answers that by running the CLI's own read-only probes
(``--version``, then ``whoami``) inside the OS sandbox.

**It performs no setup of its own.** Both setup steps belong to Kiro CLI and are
taken by the user:

* obtaining the CLI — from Kiro's official setup page
  (:data:`OFFICIAL_INSTALL_DOCS_URL`);
* signing in — ``kiro-cli login`` (:data:`KIRO_CLI_LOGIN_COMMAND`), run by the
  user wherever they already use the CLI.

Kiro Crew neither installs nor authenticates on the user's behalf, so there is no
installer download, no installer execution, and no device-flow spawn. The
reasons are the same in both cases: the vendor's own tooling does it better and
stays correct as it changes, owning it here meant owning a large privileged
surface (a remote shell script executed unsandboxed; a credential-writing child
process), and every copy of that logic on this side was one more thing to drift
out of date — the installer's pinned digest silently broke setup on any upstream
change.

What remains is detection, and detection alone: every subprocess this module
spawns is one of the two read-only probes above, sandboxed, with a fixed argv.
No command, argument, URL, or filesystem target is accepted from an HTTP
request, and the dashboard exposes only a ``status`` read plus the agent-spec
repair write (which touches Kiro Crew's own files, never the CLI).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from kiro_crew import hooks, identity_stores, platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import CRED_KIRO_API_KEY, read_env_file_credential
from kiro_crew.config.paths import config_dir
from kiro_crew.kiro_cli import (
    find_kiro_cli_candidates,
    known_kiro_cli_dirs,
)
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    resource_limit_supervisor_argv,
    sandboxed_spawn_argv,
    shielded_prepare_off_loop,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

OFFICIAL_INSTALL_DOCS_URL = "https://kiro.dev/cli/"
# The exact command the user runs to sign in. A CODE CONSTANT, never a catalog
# value: a translated command cannot be typed or executed (see
# website/docs/i18n-catalog.md — "A literal token the user must type must never be
# a catalog value"). Served in the status payload so the UI has one source of
# truth for it rather than hardcoding a second copy that can drift.
KIRO_CLI_LOGIN_COMMAND = "kiro-cli login"
# The organization-SSO counterpart, served alongside the bare command so the gate
# can offer both instead of one ambiguous line. Both flags are load-bearing:
# ``--use-device-flow`` is what makes the others take effect at all (kiro-cli
# discards every login flag unless the environment is remote OR that flag is set,
# falling back to a browser portal), and ``--license pro`` then selects Identity
# Center directly. The pair cannot yield a Builder ID session, which is the
# failure this exists to prevent: on the portal, a free Builder ID sits as a
# visual peer of organization SSO, so a user on an SSO plan can sign in to the
# wrong tier and only discover it when models are missing.
KIRO_CLI_SSO_LOGIN_COMMAND = "kiro-cli login --use-device-flow --license pro"
# The command that updates the CLI in place. Unlike the install/sign-in steps
# above, which Kiro Crew only ever NAMES for the user to run, this one IS run on
# the user's behalf (see :meth:`KiroPrerequisiteService.update_cli`): it is the
# CLI's own self-update subcommand, the same one the auto-update path already
# invokes, so executing it introduces no new privileged surface — it is not a
# remote installer script, just the installed binary updating itself. Offered
# when the installed CLI is too old to expose the ``acp`` subcommand Kiro Crew
# drives every session through. A CODE CONSTANT, never a catalog value: a
# translated command cannot be typed or executed.
KIRO_CLI_UPDATE_COMMAND = "kiro-cli update"
# The subcommand Kiro Crew launches every ACP session through (see
# ``acp.client.KIRO_CLI_SUBCMD`` / ``acp.runtime.KIRO_CLI_SUBCMD``). Probed by
# name so the readiness check learns of a rename the same way a spawn would fail.
_ACP_SUBCOMMAND = "acp"
# How long the self-update is allowed to run before the probe gives up on it.
# ``kiro-cli update`` downloads and swaps a binary, so it is far slower than the
# read-only probes; sized to match the auto-update path's own 120s budget in
# ``slack/gateway.py`` rather than the 10s probe ceiling.
_UPDATE_TIMEOUT_SECS = 120
# Compatibility shim, not live state. Nothing performs an operation any more, but a
# dashboard loaded BEFORE this change reads ``status.operation.status``
# unconditionally in its refetch-interval callback — the optional chain there
# guards ``status``, not ``operation`` — and that callback runs for every user, not
# only the first-run gate. A tab left open across a gateway upgrade would therefore
# throw on its next poll and drop to the root error screen. Serving a permanently
# idle object keeps those tabs working; it can be deleted once no shipped client
# reads the field.
_LEGACY_IDLE_OPERATION: dict[str, str] = {
    "kind": "",
    "status": "idle",
    "message": "",
    "detail": "",
    "url": "",
    "error": "",
}


def legacy_idle_operation() -> dict[str, str]:
    """Return a fresh idle ``operation`` for the pre-upgrade-client shim."""

    return dict(_LEGACY_IDLE_OPERATION)


_MAX_CAPTURED_OUTPUT = 64 * 1024
_MAX_VISIBLE_DETAIL = 4_000
_PROBE_TIMEOUT_SECS = 10
#: Marker identifying a kiro-cli agent-spec REJECTION in the probe's captured
#: output, as opposed to any other reason the probe produced text.
#:
#: ``kiro-cli agent validate`` exits 0 whether or not it accepted the file — the
#: verdict is carried only in the message it writes — so the exit status cannot
#: be used and this match is the whole signal. On acceptance it writes nothing at
#: all; on rejection it writes ``Error: Json supplied at <path> is invalid:
#: <reason>``.
#:
#: Matching the schema-rejection wording rather than "the probe printed
#: something" is deliberate and load-bearing in BOTH directions. A probe that
#: could not read the file (a sandbox denial, a deleted spec) also writes to the
#: captured stream, and reporting that as "kiro-cli rejects your spec" would send
#: the user to rewrite a spec that is fine. Anything unrecognized is therefore
#: treated as acceptance: this module's rule is to report nothing rather than
#: block a working install behind a repair card it cannot clear.
_SPEC_REJECTION_MARKER = "is invalid"
# Substrings that identify an "unknown subcommand" rejection in the captured
# output of an ``acp --help`` probe. A kiro-cli that HAS the ``acp`` subcommand
# exits 0 and prints its help; one too old to have it exits nonzero and (being a
# clap CLI) prints one of these. Matched case-insensitively.
#
# This is deliberately a POSITIVE match on the rejection wording rather than
# "the probe printed something" or "exit was nonzero", for the same reason the
# spec probe matches its rejection marker: a probe that failed for an unrelated
# reason (a sandbox denial, a hung binary) also produces a nonzero exit and
# captured text, and reporting THAT as "your CLI is too old" would push the user
# to run an update that cannot fix it. Anything unrecognized is therefore treated
# as supported — the module's rule is to report nothing rather than block a
# working install behind a card it cannot clear (see :meth:`_probe_acp_support`).
_ACP_UNSUPPORTED_MARKERS = (
    "unrecognized subcommand",
    "unknown subcommand",
    "unrecognized command",
    "unknown command",
    "invalid subcommand",
    "no such subcommand",
)
# The identity probe's own budget, deliberately separate from
# _PROBE_TIMEOUT_SECS. ``whoami`` is not a local read: when the cached token has
# expired, Kiro CLI refreshes an OIDC token against the organization's IdP —
# for IAM Identity Center users that network round trip measures 12–20 seconds,
# so a probe killed at the shared 10-second ceiling reports a fully signed-in
# CLI as ``authenticated=False`` and wedges the first-run gate. The budget is
# NOT applied to ``--version``: that probe is the FIRST execution of an unknown
# candidate and must stay on a short leash, so a genuinely missing or hung
# binary is still reported quickly rather than blocking the gate for the full
# identity budget. Sized to cover the observed refresh with headroom, while the
# in-flight latch in :meth:`KiroPrerequisiteService.snapshot` keeps the gate's
# machine polls answering from cached state instead of queueing behind a probe
# this long.
_IDENTITY_PROBE_TIMEOUT_SECS = 30
_PROBE_CACHE_SECS = 2.0
# Floor between HOST probes driven by the status endpoint, however many callers
# ask. The first-run gate polls with ``refresh=1`` every 5s while it blocks the
# dashboard (it has to: readiness is latched at boot, so a latch read can never
# see the CLI the user just installed). Each browser tab polls independently and
# they cannot coordinate, so without a floor N open tabs mean N times the probes,
# each of them two ``kiro-cli`` spawns. Collapsing them costs a user-driven Check
# again nothing observable: the worst case is an answer a few seconds old, which
# is indistinguishable from a fresh one at human speed.
_FORCED_PROBE_FLOOR_SECS = 4.0
# Yield before the boot-time readiness probe so its kiro-cli spawn does not
# contend with the concurrent app-backend spawns on the boot-critical path.
_WARM_UP_DELAY_SECS = 3.0
_TERMINATION_GRACE_SECS = 2.0
_WINDOWS_DESCENDANT_POLL_SECS = 0.05
_KIRO_AUTH_SANDBOX_MODE = "standard"
_UNVERIFIED_SANDBOX_MODE = "strict"
_SETUP_COMPLETE_FILENAME = ".kiro_cli_setup_complete"
_PROCESS_GROUP_SUPERVISOR = str(Path(__file__).with_name("_process_group_supervisor.py"))
_PROCESS_GROUP_SUPERVISOR_ERROR = "Kiro process-group supervisor is unavailable"
try:
    _PROCESS_GROUP_SUPERVISOR_CODE = Path(_PROCESS_GROUP_SUPERVISOR).read_text(encoding="utf-8")
except OSError:
    _PROCESS_GROUP_SUPERVISOR_CODE = ""
_AUTH_STAGING_RELATIVE = Path(".kiro") / "crew-auth-staging"
# Marker the offline E2E harness sets on the gateway it spawns. It grants NO
# privilege: the packaged fake ACP backend is launched by the ordinary in-place
# path like any other runnable executable. It is kept purely as a "this gateway
# is a test rig" signal for the harness contract (test/test_harness.py).
FAKE_ACP_TEST_MODE_ENV = "KIROCREW_FAKE_ACP_TEST_MODE"
_MAX_AUTH_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_AUTH_STORE_FILE_BYTES = 64 * 1024 * 1024
_AUTH_STORE_READ_ERROR = "Kiro identity file could not be read safely"
# The Kiro CLI identity database is PROJECTED, never byte-copied. It is the CLI's
# main store: identity lives in two small tables, while `history` /
# `conversations*` hold chat transcripts and grow without bound (a real user
# report had it at ~429 MB). A byte copy therefore (a) blew past
# _MAX_AUTH_STORE_FILE_BYTES and aborted sign-in with a message that names
# neither size nor the real cause, and (b) read the whole file into memory to
# write it straight back out. Projection copies the full SCHEMA plus rows from
# _AUTH_IDENTITY_TABLES only, so the staged database is small and bounded by the
# identity data alone, no matter how large the source grows.
#
# Full schema, not just the identity tables: `migrations` is projected WITH its
# rows, so the CLI sees its schema version as already-applied and runs no
# migration. A database holding only the identity tables would then fail with
# "no such table: history" on first use. Copying every table's DDL (and indexes)
# while withholding non-identity ROWS keeps the CLI's queries valid and still
# hands the sandboxed process no transcript content.
_AUTH_SQLITE_DB = identity_stores.AUTH_SQLITE_DB
_AUTH_IDENTITY_TABLES = ("auth_kv", "migrations")
# `state` is a mixed key/value table: a few rows describe WHICH identity is signed
# in (Identity Center region + start URL, CodeWhisperer profile) and the rest is
# unrelated local state — telemetry ids, onboarding flags, prompt counters. Copy
# the table's rows SELECTIVELY by key prefix so the staged store can render a
# complete `whoami` (profile/ARN block) without also handing the sandboxed CLI
# the user's telemetry identifiers. Prefix-matched, not an exact list, so a new
# `auth.idc.*` key is carried automatically rather than silently dropped.
_AUTH_STATE_TABLE = "state"
_AUTH_STATE_KEY_PREFIXES = ("auth.", "api.codewhisperer.")
_AUTH_SQLITE_FILES = (_AUTH_SQLITE_DB,)
# What :func:`identity_fingerprint` returns when the store names no identity --
# signed out, absent, or unreadable. Not a hash, so it can never collide with a
# real fingerprint.
_AUTH_FINGERPRINT_ABSENT = ""
# SEL audit label for the identity-fingerprint read. Registered in
# hooks._AUDIT_ONLY_READ_IDS; an unregistered id is refused there, and this reader
# fails closed on that refusal rather than reading unaudited.
_IDENTITY_FINGERPRINT_READ_ID = "kiro_prerequisite.identity_fingerprint"
# Blob fields that identify WHICH account is signed in and survive a token
# refresh. An ALLOWLIST on purpose: the same blobs carry `access_token`,
# `refresh_token`, `expires_at` and `client_secret`, and a denylist would admit
# every field a future kiro-cli adds -- letting a new secret into the digest, or a
# new rotating field cause an account change on every refresh. `client_id` is the
# OIDC registration, which is replaced when the client re-registers; that costs one
# cold start on re-registration and is what distinguishes two logins whose
# start_url is identical.
_IDENTITY_CLAIM_FIELDS = frozenset(
    {
        "client_id",
        "oauth_flow",
        "region",
        "scopes",
        "start_url",
    }
)
# How long a computed fingerprint may be reused. Bounds both the SQLite reads and
# the SEL audit events a poll storm can produce (N dashboard tabs poll status every
# few seconds), so the store is observed per action rather than per poll. Read off
# time.monotonic() rather than the service's injected clock: this is a real-time
# rate bound, not part of the probe's testable timing contract.
_AUTH_FINGERPRINT_CACHE_SECS = 5.0
# Short: the projection is a handful of small reads against a local file, and a
# stuck open must not hold up sign-in.
_AUTH_SQLITE_TIMEOUT_SECS = 5.0
# Process-lifetime pins for explicit operator overrides. The prerequisite
# service records the canonical path + digest before any agent session starts;
# ACP spawn consumes the same pin so a later agent write cannot turn a stale
# readiness result into execution of replacement bytes.
_OPERATOR_OVERRIDE_ATTESTATIONS: dict[str, str] = {}

_PROBE_ENV_KEYS = frozenset(
    {
        "APPDATA",
        # Session bus + runtime dir: some Kiro CLI builds connect to the D-Bus
        # secret-service keyring at startup — even for ``--version`` — so the
        # probe must pass these through when the host sets them (e.g. AL2023,
        # where without them the CLI exits "Failed to connect to bus"). They are
        # only forwarded when present; hosts without a session bus (macOS, AL2,
        # headless) simply don't set them, so this is a no-op there.
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "ProgramFiles",
        "PROGRAMFILES",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)

# Kiro CLI's OWN model credential, forwarded to the identity probe only.
#
# ``kiro-cli`` accepts an API key through the environment as an alternative to a
# ``kiro-cli login`` token store, and ``whoami`` reports "Authenticated with API
# key" only when it can see that variable. Filtered out, the probe answers "Not
# logged in" (exit 1) on a host where an ACP session authenticates fine, because
# ACP inherits the real environment — so dropping it latches
# ``authenticated=False`` while chat still works, which is the worst of both: the
# first-run gate demands a sign-in the user has already done, and every
# ``verified_ready`` caller (``/api/models``, usage polling, the destructive
# reruns) answers 503.
#
# SECURITY: the exposure delta is one probe's argv, not a new surface. The value
# reaches the same resolved binary this same probe already executes, in the same
# standard sandbox posture, against the same real home (see
# :meth:`KiroPrerequisiteService._run_auth_command`'s ``isolate_home=False`` note),
# on a fixed ``whoami`` argv. It is kept OUT of :data:`_PROBE_ENV_KEYS` because
# ``--version`` is the FIRST execution of a candidate that has not yet answered
# anything, and nothing about resolving a version needs a key.
#
# The CLI's exit status reports which credential kind is CONFIGURED, not whether
# the credential is accepted, so a stale or mistyped key reads as signed in. That
# is the same answer an ACP session acts on, which is the point — but it means
# presence of this variable must never become a shortcut that skips the probe.
#
# In a post-scrub Docker container the variable lives only in the data home's
# .env (the entrypoint removed it from the environ), so the identity probe also
# falls back to reading it from that file — see _audited_identity_probe.
_IDENTITY_PROBE_ENV_KEYS = frozenset({CRED_KIRO_API_KEY})

# Proxy configuration, forwarded to the ``whoami`` identity probe only.
#
# On a host that reaches the network only through an HTTP proxy, ``whoami``
# must talk to the IdP the same way the user's own shell does — filtered out,
# the child cannot connect, the probe answers "Not logged in", and the setup
# gate latches unauthenticated on a host where sign-in actually works. Both
# case spellings are listed because matching is exact on POSIX and the HTTP
# stacks disagree on which case they honour (curl-family reads the lowercase
# names, Rust/Python stacks read either), so forwarding only one case
# reproduces the bug on half of hosts. ALL_PROXY is included deliberately: a
# SOCKS-only host sets it INSTEAD of the scheme-specific pair, and curl and
# reqwest both honour it as the fallback, so omitting it keeps the exact same
# defect for that host class.
#
# These are kept OUT of :data:`_PROBE_ENV_KEYS` for the same reason as the
# credential above: a proxy URL can embed credentials, and ``--version`` is
# the FIRST execution of a candidate that has not yet answered anything — and
# nothing about resolving a version needs the network. ``whoami`` runs only
# after ``--version`` succeeds, against the same resolved binary an ACP
# session already runs with the full inherited environment, so the exposure
# delta is confined to a candidate that has already passed the version gate.
_IDENTITY_PROXY_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


@dataclass
class ProcessResult:
    """Bounded result from one fixed child process."""

    ok: bool
    output: str = ""
    returncode: int | None = None
    timed_out: bool = False
    error: str = ""
    # ``(kind, detail, remedy)`` when the spawn was refused because the sandbox
    # could not be built — set ONLY from the typed SandboxUnavailableError, never
    # inferred from host capability. A probe that failed for any other reason
    # leaves this None, so an unrelated failure can never be misreported as a
    # sandbox problem.
    sandbox_failure: tuple[str, str, str] | None = None


@dataclass
class PrerequisiteStatus:
    """Last known Kiro CLI readiness state."""

    platform: str
    installed: bool = False
    authenticated: bool = False
    ready: bool = False
    # Something on the host needs an owner-driven repair before ``ready`` can go
    # true. Only the agent-spec overlay sets it — a missing CLI is NOT a repair
    # (the user installs it from OFFICIAL_INSTALL_DOCS_URL, which Kiro Crew has no
    # action for), so a false value here says nothing about ``installed``.
    repair_required: bool = False
    initial_setup_complete: bool = False
    docs_url: str = OFFICIAL_INSTALL_DOCS_URL
    # What the user runs to sign in. Kiro Crew never runs it for them.
    login_command: str = KIRO_CLI_LOGIN_COMMAND
    # The organization-SSO alternative, offered next to ``login_command`` so the
    # tier is an explicit choice rather than whichever option the sign-in page
    # happens to make prominent.
    sso_login_command: str = KIRO_CLI_SSO_LOGIN_COMMAND
    # Whether the installed CLI exposes the ``acp`` subcommand every Kiro Crew
    # session is launched through. Defaults True so nothing regresses when the
    # probe cannot answer (a sandbox refusal, a timeout): those are reported by
    # their own fields, and asserting "too old" off a probe that never ran would
    # push the user to update a CLI that is fine. Only a CLEAN "unknown
    # subcommand" verdict from ``acp --help`` sets it False — at which point the
    # CLI runs and is signed in, but cannot start a single session, so it narrows
    # ``ready`` exactly like a rejected spec. The remedy is an UPDATE, not a
    # reinstall: the binary is present and only out of date.
    acp_supported: bool = True
    # What the user's CLI is updated with when ``acp_supported`` is False. Unlike
    # ``login_command`` (which Kiro Crew only names), this one is also run FOR the
    # user by ``update_cli`` — it is the CLI's own in-place self-update. Served in
    # the payload so the UI has one source of truth for the string.
    update_command: str = KIRO_CLI_UPDATE_COMMAND
    # Exception / failure text from an ``update_cli`` attempt. Empty when no
    # update was attempted or it succeeded. Shown verbatim, untranslated: it
    # names why the self-update did not complete, which is what a support
    # conversation needs.
    cli_update_error: str = ""
    # A Kiro CLI binary that is present and executable but could not be VERIFIED
    # (verification runs the binary inside the sandbox) is a categorically
    # different condition from a missing binary, and a failed sandbox build
    # carries zero information about whether the CLI is installed. Without these
    # fields the two collapse into ``installed=False`` and the dashboard tells
    # the user to install a CLI that is already there and authenticated.
    sandbox_unavailable: bool = False
    # Machine-readable: "transient" | "foreign_sandbox" | "no_backend". The
    # presentation layer maps this to its own translated remedy copy instead of
    # parsing English prose out of ``sandbox_detail``.
    sandbox_failure_kind: str = ""
    # Technical probe reason, e.g. "unshare(CLONE_NEWNS) failed with errno 1
    # (EPERM)". Names the failing step, so it is shown verbatim, untranslated.
    sandbox_detail: str = ""
    # Machine-readable host mechanism behind a Linux userns denial — one of the
    # sandbox ``REMEDY_*`` tokens, or "" when unknown. Without it the gate could
    # only show the raw errno, which is the dead end reported in issue #1660: the
    # probe knows the fix is an AppArmor profile and the user cannot tell.
    sandbox_remedy: str = ""
    # The verification probe hit ``_PROBE_TIMEOUT_SECS`` instead of answering. A
    # THIRD condition, distinct from both a missing binary and a sandbox refusal:
    # the spawn was accepted and raised no typed failure, so every ``sandbox_*``
    # field stays empty and none of them can carry it. Without this the timeout
    # collapsed into bare ``installed=False`` with all ``sandbox_*`` empty — a
    # combination that does not merely fail to help, it actively rules out the true
    # cause and sends the operator to reinstall or re-login, neither of which can
    # work on a host whose CLI is installed, signed in, and serving turns the whole
    # time (issue #4577: 4081 SEL ``probe_version`` events, every one
    # ``outcome=failed error=timeout``, on exactly such a host).
    probe_timed_out: bool = False
    # Kiro Crew's own agent specs (~/.kiro/agents/kirocrew*.json). ``ready``
    # requires these on disk, not merely a viable binary and a good ``whoami``:
    # without them kiro-cli answers every ``session/set_mode`` with
    # "Mode '<name>' not found", so an install missing one cannot run a single
    # turn. Filenames rather than a bool so the surface can name what is missing.
    missing_agent_specs: list[str] = field(default_factory=list)
    # Exception text from the repair attempt the Refresh / Check again button
    # makes when specs are missing. Empty when no repair was attempted or it
    # succeeded. Shown verbatim, untranslated: it names the failing install step,
    # which is the one thing a support conversation actually needs.
    agent_spec_repair_error: str = ""
    # Kiro Crew's own specs that are PRESENT but which kiro-cli refuses to load.
    # Presence and acceptance are different questions: a spec on disk that the
    # installed kiro-cli rejects is dropped from its agent table entirely, so
    # ``--agent kirocrew`` resolves to the default agent with only a line on
    # stderr — every Kiro Crew MCP server silently absent from the session. That
    # is indistinguishable from a working install to anything that only stats the
    # file, which is why ``missing_agent_specs`` cannot cover it.
    #
    # The oracle is the binary, never a schema mirrored here: kiro-cli is asked
    # whether it accepts each spec, so a future release that changes the spec
    # schema is reported rather than guessed at.
    rejected_agent_specs: list[str] = field(default_factory=list)
    # kiro-cli's own reason for the first rejection above, sanitized. Its message
    # names the offending file and construct, which is the only thing that turns
    # "my agents stopped working" into an actionable report.
    agent_spec_rejection_detail: str = ""


@dataclass(frozen=True)
class _AuthStoreMapping:
    """One real Kiro identity store mapped into the temporary auth home."""

    source: Path
    staged_relative: Path
    filenames: tuple[str, ...]
    # Mappings sharing a group are ALTERNATE locations of ONE store, only one of
    # which a given host uses. Staging must abort when a matched store cannot be
    # read, because a staged home with no identity looks signed-out -- but that
    # rule is right per LOCATION and wrong across alternates, where a stale
    # leftover in the root this host abandoned would abort staging from the root
    # it actually uses. Within a group the abort is therefore deferred: it fires
    # only when NO alternate yielded an identity. ``None`` means "not an
    # alternate of anything" and keeps the strict per-location rule -- the AWS
    # SSO cache is a single location holding several token files, and losing any
    # one of those must still abort.
    group: str | None = None


@dataclass(frozen=True)
class _AuthWorkspace:
    """Temporary credential-minimal home for a trusted Kiro CLI auth call."""

    root: Path
    env: dict[str, str]


@dataclass(frozen=True)
class TrustedAcpExecutableSnapshot:
    """The resolved Kiro CLI path for one ACP launch.

    ``launch_path`` is ALWAYS the user's installed binary, launched in place —
    KiroCrew never copies the CLI and runs the copy, because a multi-call Kiro
    CLI resolves its sibling subcommand executable relative to its own path.
    """

    launch_path: str


ProcessRunner = Callable[..., Awaitable[ProcessResult]]
AuditWriter = Callable[..., Awaitable[None]]


def _platform_label(platform_name: str) -> str:
    if platform_name == "darwin":
        return "macOS"
    if platform_name == "win32":
        return "Windows"
    if platform_name.startswith("linux"):
        return "Linux"
    return platform_name or "Unknown"


def _append_capped(current: str, chunk: str) -> str:
    combined = current + str(chunk or "").replace("\r", "")
    if len(combined) <= _MAX_CAPTURED_OUTPUT:
        return combined
    return combined[-_MAX_CAPTURED_OUTPUT:]


def _sanitize_detail(text: str) -> str:
    safe, _ = redact_exfiltration_urls(str(text or ""))
    safe, _ = redact_credentials(safe)
    return safe[-_MAX_VISIBLE_DETAIL:]


def _terminal_audit_detail(result: ProcessResult, succeeded: bool) -> str:
    """The terminal audit event's error label for a run that finished or timed out.

    *succeeded* is the verdict the caller already reached, not ``result.ok``, so
    the label always follows that verdict and can never contradict the
    ``outcome`` recorded beside it. The update path spells the verdict
    ``update.ok and not error``, where ``error`` is itself set from this same
    run, so the two agree there.
    """

    if succeeded:
        return ""
    if result.timed_out:
        return "timeout"
    return "nonzero exit"


def _canonical_candidate(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _is_runnable_executable(path: str, platform_name: str | None = None) -> bool:
    """True if *path* resolves to an executable regular file on this platform.

    The single trust primitive for the "runs + valid login" model: a Kiro CLI
    that can be executed is eligible for sign-in and ACP launch, regardless of
    install source, owner, or fixed path.
    """

    return platform_compat.is_executable_file(
        _canonical_candidate(path),
        platform_name=platform_name,
    )


def _binary_sha256(path: str) -> str:
    """Hash one regular executable without following a final symlink."""

    canonical = _canonical_candidate(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(canonical, flags)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_AUTH_EXECUTABLE_BYTES
        ):
            raise OSError("Kiro CLI candidate is not a bounded regular executable")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _register_operator_override_attestation(path: str, digest: str | None) -> None:
    """Remember the first gateway-start digest for one explicit override."""

    if not path or not digest:
        return
    key = os.path.normcase(_canonical_candidate(path))
    # First observation wins for the lifetime of this process. Reconstructing a
    # service after the file changes must not silently bless the new bytes.
    _OPERATOR_OVERRIDE_ATTESTATIONS.setdefault(key, digest)


def _acp_executable_is_runnable(
    executable: str,
    *,
    platform_name: str,
) -> bool:
    """Return whether ACP may launch *executable*.

    Trust is "it runs" — install source, owner, and path do not gate ACP launch,
    so a toolbox / Homebrew / self-updated CLI is accepted like any other
    (KiroCrew is not the authority on where Kiro CLI is installed).

    A zero-byte candidate is still refused. An interrupted install or
    self-update can leave a truncated but executable ``kiro-cli``; exec'ing it
    dies without an ACP frame, surfacing as the same opaque
    "process exited (rc=None)" this module works to avoid. Rejecting it here
    turns that into a readable prerequisite error instead. (The retired copy
    path got this for free from the snapshot's ``fstat`` size guard.)
    """

    canonical = _canonical_candidate(executable)
    if not _is_runnable_executable(canonical, platform_name):
        return False
    try:
        return os.path.getsize(canonical) > 0
    except OSError:
        return False


def snapshot_trusted_acp_executable(
    executable: str,
    *,
    data_home: Path | None = None,
    platform_name: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> TrustedAcpExecutableSnapshot:
    """Return the user's installed Kiro CLI, to be launched IN PLACE.

    KiroCrew always executes the Kiro CLI the user installed, at the path it was
    resolved from. It never copies the binary elsewhere and runs the copy.

    Kiro CLI 2.15+ is a **multi-call binary**: it dispatches subcommands by
    exec'ing a SIBLING executable (e.g. ``kiro-cli-chat``) that it locates
    relative to its own path — on macOS by finding ``.app/Contents/MacOS/`` in
    that path. Copying the binary into a flat private directory destroys the
    sibling layout, so every dispatch fails with ``No such file or directory (os
    error 2)`` and ACP dies at handshake with ``process exited (rc=None)``. The
    same breaks any launcher that resolves adjacent resources: a multiplexer
    dispatching on ``argv[0]``, a wrapper reading a sibling registry, or a
    self-updating install whose payload lives beside it.

    An earlier design copied the bytes into a private snapshot (sealed memfd on
    Linux, verified copy on macOS) to close the resolve-to-exec window in which
    the file could be swapped. That is deliberately **not** done anymore: it
    defends against an attacker who already has write access to the user's own
    machine — a threat the rest of the product does not defend against either —
    and the cost was breaking every multi-call and multiplexer install outright.

    Trust is "the CLI runs": install source, owner, and path do not gate launch,
    so a toolbox / Homebrew / self-updated Kiro CLI launches like any other.
    Raises ``ValueError`` when the candidate is not a runnable executable.
    """

    platform_name = platform_name or sys.platform
    del data_home, environ  # neither provenance nor a data home gates ACP launch
    if platform_name == "win32":
        return TrustedAcpExecutableSnapshot(launch_path=_canonical_candidate(executable))
    if not _acp_executable_is_runnable(executable, platform_name=platform_name):
        raise ValueError("Kiro CLI is not a runnable executable for ACP execution")

    # Launch the path the caller resolved, NOT its realpath: a multiplexer
    # launcher (e.g. ``~/.toolbox/bin/kiro-cli`` → ``toolbox-exec``) dispatches
    # on its own argv[0] basename, and a multi-call binary resolves its sibling
    # subcommand relative to the invoked path. Both break under the realpath.
    #
    # ``abspath``, NOT ``realpath``: it anchors a relative candidate (an operator
    # ``KIROCREW_KIRO_BIN=./kiro-cli`` resolves against the GATEWAY's cwd, but ACP
    # spawns with ``cwd=<session work_dir>``, so a relative launch path would die
    # with ENOENT there) while leaving symlinks intact, so the multiplexer and
    # sibling-layout properties above still hold.
    return TrustedAcpExecutableSnapshot(launch_path=os.path.abspath(executable))


def _read_bounded_regular_file(path: Path) -> bytes | None:
    """Read an allowlisted auth-store file with size and symlink defenses."""

    if path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_AUTH_STORE_FILE_BYTES:
            return None
        output = bytearray()
        while len(output) <= _MAX_AUTH_STORE_FILE_BYTES:
            chunk = os.read(fd, min(1024 * 1024, _MAX_AUTH_STORE_FILE_BYTES + 1 - len(output)))
            if not chunk:
                return bytes(output)
            output.extend(chunk)
        return None
    finally:
        os.close(fd)


def _open_identity_db_readonly(path: Path) -> Any | None:
    """Open a Kiro identity database read-only, with the same path defenses.

    Mirrors :func:`_read_bounded_regular_file`'s gates — reject a symlink, and
    require a regular file — then hands SQLite a read-only URI. Deliberately NOT
    size-gated: only the identity tables are read out, so the source file's size
    does not bound what is staged. Returns ``None`` when the path fails a gate or
    SQLite cannot read it (missing, corrupt, or not a database).
    """

    if path.is_symlink():
        return None
    try:
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return None
    except OSError:
        return None
    # mode=ro WITHOUT immutable=1. `immutable=1` would promise never to touch a
    # sidecar, but it also tells SQLite the file cannot change, so the WAL is
    # IGNORED: against a store in WAL mode whose newest commits are still in
    # `data.sqlite3-wal`, the token row reads as missing and the CLI is handed a
    # store it reports as signed-out — a worse failure than the size abort this
    # projection replaces. Plain `mode=ro` applies the WAL, so the staged
    # identity always matches what the CLI itself would read. The cost is that
    # SQLite may create/refresh the `-shm` index beside the live database exactly
    # as any other reader does; `-shm` is a shared-memory index holding no
    # identity data, and no identity bytes are ever written back.
    uri = f"file:{urllib.request.pathname2url(str(path))}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=_AUTH_SQLITE_TIMEOUT_SECS)
    except sqlite3.Error:
        return None


def _project_identity_database(source: Path, destination: Path) -> bool:
    """Stage ONLY Kiro's identity tables into a fresh owner-only database.

    Copies every table/index DDL (so the CLI never hits "no such table" on a
    store whose ``migrations`` rows say the schema is current) but only the ROWS
    of :data:`_AUTH_IDENTITY_TABLES`. Transcript tables arrive empty, so the
    staged file stays small however large the source grows.

    Returns ``False`` when the source cannot be read or lacks an identity table,
    so the caller aborts staging instead of running against a store that looks
    signed-out.
    """

    connection = _open_identity_db_readonly(source)
    if connection is None:
        return False
    try:
        with contextlib.closing(connection):
            schema = connection.execute(
                "SELECT sql FROM sqlite_master "  # wokeignore:rule=master
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"  # wokeignore:rule=master
                ).fetchall()
            }
            # EVERY identity table must exist. `all`, not `any`: a future kiro-cli
            # that renames one of them (say `auth_kv`) while keeping the other
            # would pass an `any` gate and stage a store with the schema present
            # but ZERO identity rows — silently producing exactly the "signed out"
            # outcome this gate exists to prevent. Requiring all of them turns a
            # schema change into the loud _AUTH_STORE_READ_ERROR abort instead.
            if not all(table in present for table in _AUTH_IDENTITY_TABLES):
                return False
            rows = {
                table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
                for table in _AUTH_IDENTITY_TABLES
            }
            # `state`: carry only the identity-describing keys (see
            # _AUTH_STATE_KEY_PREFIXES). Absent on an older schema — optional by
            # design, so a store without it still stages.
            if _AUTH_STATE_TABLE in present:
                predicate = " OR ".join(["key LIKE ?"] * len(_AUTH_STATE_KEY_PREFIXES))
                rows[_AUTH_STATE_TABLE] = connection.execute(
                    f'SELECT * FROM "{_AUTH_STATE_TABLE}" WHERE {predicate}',
                    tuple(f"{prefix}%" for prefix in _AUTH_STATE_KEY_PREFIXES),
                ).fetchall()
    except sqlite3.Error:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Create the file before writing, so the identity rows are never briefly
    # world-readable between SQLite's create and the chmod.
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    platform_compat.restrict_to_owner(str(destination))
    try:
        staged = sqlite3.connect(str(destination), timeout=_AUTH_SQLITE_TIMEOUT_SECS)
        with contextlib.closing(staged):
            with staged:
                for (statement,) in schema:
                    staged.execute(str(statement))
                for table, table_rows in rows.items():
                    if not table_rows:
                        continue
                    placeholders = ",".join("?" * len(table_rows[0]))
                    staged.executemany(
                        f'INSERT INTO "{table}" VALUES ({placeholders})', table_rows
                    )
    except sqlite3.Error:
        with contextlib.suppress(OSError):
            os.unlink(str(destination))
        return False
    return True


def _atomic_write_secret_bytes(path: Path, content: bytes) -> None:
    """Atomically stage one bounded Kiro identity file, owner-only from birth.

    ``restrict_to_owner=True`` locks the staged temp file down BEFORE the
    identity bytes reach it (``0o600`` on POSIX, an owner-only DACL on
    Windows), so those bytes never sit in a file readable at the parent's
    inherited DACL. The default ``restrict_on_error="raise"`` keeps that
    unconditional: a lockdown failure aborts before the final path is touched,
    so an unprotectable identity file never exists there at all.
    """

    atomic_write(path, content, fsync=True, restrict_to_owner=True)


def kiro_identity_store_path(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> Path:
    """Return kiro-cli's OWN live identity database path.

    Deliberately not amazon-q's: that store often holds the same account but is a
    different product's credential, so it cannot answer "which account is this
    CLI signed in as".

    Every platform resolves among FIXED, home-anchored locations, drawn from the
    same set as the trusted live-store list in
    ``dashboard/handlers/kiro_usage_api.py`` (``_CLI_SQLITE_DBS``). No
    environment variable is consulted -- not ``XDG_DATA_HOME`` on Linux, not
    ``APPDATA`` or ``LOCALAPPDATA`` on Windows -- because the fence that makes
    this store unwritable by agent file tools (``_SENSITIVE_HOME_DIRS``) is
    anchored at exactly these paths. A redirected location either resolves to the
    same place or falls OUTSIDE the fence, where an agent can author the rows
    this function reads: it could then forge an identity that keeps matching and
    the children signed in as the previous account would never be retired. A
    fixed anchor cannot be pointed at something the agent may write.

    On Windows current kiro-cli writes its store under the local app-data
    directory (``AppData/Local/kiro-cli``); older layouts used the roaming one
    (``AppData/Roaming/kiro-cli``). When only one store exists it is chosen;
    when both exist the most recently written one wins, so a leftover from
    the other layout never masks the live store's account (the WAL-aware
    mtime tie-break lives in :func:`identity_stores.selected_store`). Both
    anchors sit inside the
    ``_SENSITIVE_HOME_DIRS`` fence, so neither choice widens what an agent
    can forge. This branch stats the filesystem, so callers on the event loop
    should resolve the path inside the same worker thread as the read itself.

    The cost is that a host which relocates its data home is read as having no
    identity, so the change is reported as "absent" -- which errs toward retiring
    children, never toward trusting them. ``environ`` is kept in the signature so
    callers need not know which platforms consult it, and so a future platform
    that genuinely requires it does not change every call site.
    """

    return identity_stores.selected_store(platform_name, home)


def identity_store_is_relocated(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> bool:
    """Whether the CLI's data home is configured somewhere other than our anchor.

    :func:`kiro_identity_store_path` deliberately reads a FIXED path, because the
    fence that keeps agent file tools out of that store is home-anchored and a
    redirected path can land outside it. But refusing to follow the redirection is
    not the same as being safe: if the environment points the CLI elsewhere and a
    LEFTOVER database still sits at the default location, reading it yields a
    confident fingerprint of an account nobody is signed into. A logout in the real
    store would then change nothing we can see, and the old-account child would be
    reused -- strictly worse than reporting "cannot tell".

    So a configured relocation is reported here, and the caller treats it as
    absent: no read, no false confidence, and the absent path already means "never
    reconciled, re-sweep every turn".

    Both variables count, unconditionally. The live store's directory is
    resolved by the CLI from ``LOCALAPPDATA`` (current layout) or ``APPDATA``
    (legacy layout), and which generation is writing cannot be observed --
    so once EITHER variable is redirected, a database at a fixed anchor
    cannot be attributed to a live writer: it may be the live store of the
    other generation, or a leftover of either, and reading a leftover yields
    a confident fingerprint of an account nobody is signed into. Refusing to
    guess errs toward absent, the module's safe side. The cost is that a
    host with Group-Policy folder redirection (which targets Roaming)
    reports absent even when a current-layout Local store is healthy -- the
    same answer such hosts got when the anchor lived under Roaming, so the
    posture is status quo there, and the once-per-service log in
    :meth:`KiroPrerequisiteService.current_identity_fingerprint` makes it
    diagnosable. A variable set to exactly the default location is not a
    relocation.

    The (variable, default root) pairs are PROJECTED from
    :data:`identity_stores.IDENTITY_STORE_ROOTS` -- each kiro-cli row's
    ``env_var`` against the parent of its home-relative directory -- so this
    check learns about a new or relocated store row the same way the fence,
    staging, and state-db discovery do. macOS falls out naturally: its rows
    carry no ``env_var`` (no standard variable relocates
    ``~/Library/Application Support``), so no pair is checked there.
    """

    plat = (
        identity_stores.Platform(platform_name)
        if platform_name in ("darwin", "win32")
        else identity_stores.Platform.POSIX
    )
    for root in identity_stores.IDENTITY_STORE_ROOTS:
        if (
            root.platform is not plat
            or root.product is not identity_stores.Product.KIRO_CLI
            or root.env_var is None
        ):
            continue
        default = home.joinpath(*root.home_relative_dir.split("/")).parent
        configured = environ.get(root.env_var, "").strip()
        if configured and Path(configured) != default:
            return True
    return False


def identity_fingerprint(path: Path) -> str:
    """Return a digest naming WHICH account an identity store is signed in as.

    Two rules shape what participates.

    **Stable claims only.** The credential blobs mix fields that identify an
    account with fields that are replaced on every token refresh. Digesting a
    rotating field would report an account change roughly hourly and retire
    healthy sessions, so the rotating and secret ones -- ``access_token``,
    ``refresh_token``, ``expires_at``, ``client_secret`` -- are excluded and the
    identifying ones are kept: the SSO ``start_url``, ``region``, ``oauth_flow``,
    ``scopes`` and the OIDC registration's ``client_id``. Key NAMES also
    participate, so a change of credential kind counts even when no value moved.

    **An allowlist, not a denylist** (:data:`_IDENTITY_CLAIM_FIELDS`). A field
    added to these blobs by a future kiro-cli cannot join the fingerprint by
    default -- which keeps both a new secret out of it and a new rotating field
    from causing spurious retirement.

    Values are hashed, never returned, so the fingerprint carries no credential.

    The read is SEL-audited and FAILS CLOSED: this file holds live credential
    material, so a read whose audit cannot be recorded returns "absent" rather
    than an unaudited answer. "Absent" is also what a logout leaves behind, and
    the caller treats it as "cannot confirm the running children" -- which errs
    toward retiring them, never toward trusting them.
    """

    audit_ok = hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "invoked")
    if not audit_ok:
        # A logger line is not an SEL audit. Refuse rather than read.
        logger.warning("Kiro identity fingerprint read denied: audit unavailable")
        return _AUTH_FINGERPRINT_ABSENT
    connection = _open_identity_db_readonly(path)
    if connection is None:
        hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "unreadable")
        return _AUTH_FINGERPRINT_ABSENT
    parts: list[str] = []
    try:
        with contextlib.closing(connection):
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"  # wokeignore:rule=master
                ).fetchall()
            }
            if "auth_kv" in present:
                for key, value in connection.execute("SELECT key, value FROM auth_kv").fetchall():
                    claims = _identity_claims(str(key), value)
                    if not claims:
                        # A row carrying NO stable claim is deliberately skipped
                        # ENTIRELY -- its key name is not recorded either. Recording
                        # the name alone would make two different accounts stored
                        # under the same key look identical, which is exactly the
                        # false confidence to avoid: a social login (GitHub, Google)
                        # has no SSO start_url, so account A and account B under
                        # `kirocli:social:token` would fingerprint the same and the
                        # child authenticated as A would never be retired.
                        #
                        # Contributing nothing means such a store can come out
                        # ABSENT, which is never reconciled (see the caller) and so
                        # re-sweeps every turn. "Cannot distinguish" is reported as
                        # "cannot confirm" rather than as "unchanged".
                        continue
                    parts.append(f"k:{key}")
                    parts.extend(claims)
            if _AUTH_STATE_TABLE in present:
                predicate = " OR ".join(["key LIKE ?"] * len(_AUTH_STATE_KEY_PREFIXES))
                rows = connection.execute(
                    f'SELECT key, value FROM "{_AUTH_STATE_TABLE}" WHERE {predicate}',
                    tuple(f"{prefix}%" for prefix in _AUTH_STATE_KEY_PREFIXES),
                ).fetchall()
                for key, value in rows:
                    parts.append(f"s:{key}={_claim_digest(value)}")
    except sqlite3.Error:
        hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "error")
        return _AUTH_FINGERPRINT_ABSENT
    if not parts:
        # Schema present but zero identity rows reads as signed out, not as an
        # identity whose fingerprint happens to be the digest of nothing.
        hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "signed_out")
        return _AUTH_FINGERPRINT_ABSENT
    if not hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "success"):
        # The read HAPPENED and its terminal audit could not be written, so the
        # answer is unaudited. Discard it rather than let unaudited identity data
        # drive retirement. The failure-shaped outcomes above need no such guard:
        # they already return "absent" whatever their audit does.
        logger.warning("Kiro identity fingerprint discarded: terminal audit unavailable")
        return _AUTH_FINGERPRINT_ABSENT
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def _claim_digest(value: object) -> str:
    """Hash one claim value so no credential material can leave the reader."""

    raw = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _identity_claims(key: str, value: object) -> list[str]:
    """Return hashed STABLE claims from one ``auth_kv`` blob.

    Anything that is not a JSON object contributes nothing: the row's key name is
    already recorded by the caller, and guessing at an opaque value risks pulling
    in a rotating one. Fields outside :data:`_IDENTITY_CLAIM_FIELDS` are skipped
    whatever their name.
    """

    try:
        blob = json.loads(value if isinstance(value, (str, bytes)) else str(value))
    except (ValueError, TypeError):
        return []
    if not isinstance(blob, dict):
        return []
    claims: list[str] = []
    for claim in sorted(_IDENTITY_CLAIM_FIELDS):
        if claim not in blob:
            continue
        raw = blob[claim]
        # Lists (``scopes``) are order-normalized so a reordered grant of the same
        # scopes is not mistaken for a different account.
        if isinstance(raw, list):
            rendered = ",".join(sorted(str(item) for item in raw))
        else:
            rendered = str(raw)
        claims.append(f"c:{key}:{claim}={_claim_digest(rendered)}")
    return claims


def _auth_store_mappings(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> tuple[_AuthStoreMapping, ...]:
    """Return only Kiro identity stores, never the surrounding credential dirs.

    Built over the canonical :func:`identity_stores.store_mappings` projection so
    the source directories, the env-var source-side honouring
    (``LOCALAPPDATA`` / ``APPDATA`` / ``XDG_DATA_HOME``), the fixed staged side,
    and the Local-before-Roaming ordering all come from the single table. This
    wrapper keeps staging's own concerns: the ``.aws/sso/cache`` token mapping
    (not an identity store, so not in the table), the ``_AuthStoreMapping`` shape
    with ``filenames=_AUTH_SQLITE_FILES``, and the ``win32:{app}`` group that
    defers the abort across a product's two alternate AppData roots.
    """

    mappings = [
        _AuthStoreMapping(
            source=home / ".aws" / "sso" / "cache",
            staged_relative=Path(".aws") / "sso" / "cache",
            filenames=("kiro-auth-token*.json",),
        )
    ]
    for row in identity_stores.store_mappings(platform_name, home, environ):
        # Both AppData roots of one Windows product are alternates: only one is
        # this host's live store, so a stale leftover in the unused root must
        # not abort staging from the used one. A shared group defers the abort
        # across them; distinct ``staged_relative`` values (from the table) keep
        # them from overwriting each other. macOS/Linux have a single location
        # per product, so no group. (The env-var source-side honouring and the
        # fixed staged side are already applied by ``store_mappings``.)
        group = (
            f"win32:{row.product.value}" if platform_name == "win32" else None
        )
        mappings.append(
            _AuthStoreMapping(
                source=row.source,
                staged_relative=row.staged_relative,
                filenames=_AUTH_SQLITE_FILES,
                group=group,
            )
        )
    return tuple(mappings)


def _ensure_auth_staging_parent(home: Path) -> Path:
    """Create the fixed sandbox-hidden parent before agent sessions can start."""

    staging_parent = home / _AUTH_STAGING_RELATIVE
    # A stray file or dangling/loop symlink at this path makes a plain
    # mkdir(exist_ok=True) raise FileExistsError and crash gateway boot. Remove
    # it before mkdir so boot self-heals. UNLINK rather than rename-aside: the
    # staging root holds credential material, and moving an unexpected file to a
    # sibling name would leave its (possibly sensitive) contents readable outside
    # the sandbox-hidden staging prefix. unlink() acts on the symlink itself,
    # never its target. (#561)
    if staging_parent.is_symlink() or (staging_parent.exists() and not staging_parent.is_dir()):
        try:
            staging_parent.unlink()
        except OSError as exc:
            # A concurrent gateway boot may have won the race and already cleared
            # the stray path (FileNotFoundError) or replaced it with the real
            # private directory. Only abort if a non-directory we cannot clear is
            # STILL sitting here; otherwise fall through to the idempotent mkdir.
            # (#561, concurrent-boot race)
            if staging_parent.is_symlink() or (staging_parent.exists() and not staging_parent.is_dir()):
                raise OSError(
                    f"Kiro auth staging root {staging_parent} is not a private "
                    "directory and could not be reset"
                ) from exc
    staging_parent.mkdir(parents=True, exist_ok=True)
    if staging_parent.is_symlink() or not staging_parent.is_dir():
        raise OSError("Kiro auth staging root is not a private directory")
    if platform_compat.IS_POSIX:
        platform_compat.chmod_safe(str(staging_parent), 0o700)
    else:
        platform_compat.restrict_dir_to_owner(str(staging_parent))
    return staging_parent


def _prepare_auth_workspace(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
    base_env: dict[str, str],
) -> _AuthWorkspace:
    """Build a sandbox-hidden HOME containing only Kiro identity artifacts."""

    staging_parent = _ensure_auth_staging_parent(home)
    root = Path(tempfile.mkdtemp(prefix="auth-", dir=str(staging_parent)))
    try:
        if platform_compat.IS_POSIX:
            platform_compat.chmod_safe(str(root), 0o700)
        else:
            platform_compat.restrict_dir_to_owner(str(root))
        # Deferred aborts, keyed by group: a group records its failure and is
        # judged only after every alternate has been attempted.
        group_staged: dict[str, bool] = {}
        group_failed: dict[str, str] = {}
        for mapping in _auth_store_mappings(platform_name, home, environ):
            if mapping.group is not None:
                group_staged.setdefault(mapping.group, False)
            for pattern in mapping.filenames:
                for source in mapping.source.glob(pattern):
                    staged_path = root / mapping.staged_relative / source.name
                    # The identity DATABASE is projected (identity tables only);
                    # every other identity file is a small JSON token copied
                    # under the bounded byte rules. Both abort staging on
                    # failure — never omit a matched store as though absent.
                    # For a mapping in a GROUP the abort is deferred to the
                    # group verdict below, because the alternates of one store
                    # are not each independently required.
                    if source.name == _AUTH_SQLITE_DB:
                        if not _project_identity_database(source, staged_path):
                            if mapping.group is None:
                                raise OSError(_AUTH_STORE_READ_ERROR)
                            group_failed[mapping.group] = _AUTH_STORE_READ_ERROR
                            continue
                        if mapping.group is not None:
                            group_staged[mapping.group] = True
                        continue
                    content = _read_bounded_regular_file(source)
                    if content is None:
                        if mapping.group is None:
                            raise OSError(_AUTH_STORE_READ_ERROR)
                        group_failed[mapping.group] = _AUTH_STORE_READ_ERROR
                        continue
                    _atomic_write_secret_bytes(staged_path, content)
                    if mapping.group is not None:
                        group_staged[mapping.group] = True

        # A group that had a readable store in ANY of its alternates is staged.
        # A group whose every matched store failed is the signed-out-looking
        # case the strict rule exists for, so it still aborts.
        for group, message in group_failed.items():
            if not group_staged.get(group, False):
                raise OSError(message)

        env = dict(base_env)
        env.update(
            {
                "HOME": str(root),
                "USERPROFILE": str(root),
                "XDG_CACHE_HOME": str(root / ".cache"),
                "XDG_CONFIG_HOME": str(root / ".config"),
                "XDG_DATA_HOME": str(root / ".local" / "share"),
                "APPDATA": str(root / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(root / "AppData" / "Local"),
            }
        )
        return _AuthWorkspace(root=root, env=env)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _existing_binary_digest(path: str) -> str | None:
    try:
        return _binary_sha256(path)
    except (OSError, ValueError):
        return None


def register_process_start_override_attestation(
    *,
    platform_name: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, str | None]:
    """Pin the first observed POSIX override bytes for this process."""

    active_platform = platform_name or sys.platform
    active_environ = environ if environ is not None else os.environ
    override = active_environ.get("KIROCREW_KIRO_BIN", "")
    if not override or active_platform == "win32":
        return "", None

    canonical = _canonical_candidate(override)
    key = os.path.normcase(canonical)
    digest = _OPERATOR_OVERRIDE_ATTESTATIONS.get(key)
    if digest is None:
        digest = _existing_binary_digest(canonical)
        _register_operator_override_attestation(canonical, digest)
    return canonical, _OPERATOR_OVERRIDE_ATTESTATIONS.get(key)


def _allowlisted_env(
    environ: MutableMapping[str, str],
    allowed: frozenset[str],
) -> dict[str, str]:
    """Filter *environ* down to *allowed*, honoring Windows' case-insensitive env.

    Thin wrapper binding this module's allowlists to the shared matching
    convention — exact on POSIX, case-folded on Windows. The rationale (why a
    literal membership test silently drops ``SystemRoot`` on Windows, killing
    probe and sign-in spawns with an unrelated-looking error, and why POSIX
    must stay exact) lives on :func:`platform_compat.env_key_allowed`.
    """

    return {
        key: value
        for key, value in environ.items()
        if platform_compat.env_key_allowed(key, allowed)
    }


def _probe_env(environ: MutableMapping[str, str], search_path: str) -> dict[str, str]:
    """Build a non-interactive probe environment from the fixed allowlist.

    Network-reachability configuration (TLS trust, the session bus where a CLI
    build needs it) passes through; proxy settings join only the ``whoami``
    stage via :func:`_identity_probe_env`, because a proxy URL can embed
    credentials and ``--version`` executes a candidate that has not yet
    answered anything. Ambient credentials (cloud, Slack, SSH agent,
    application secrets) never pass.
    """

    result = _allowlisted_env(environ, _PROBE_ENV_KEYS)
    result["PATH"] = search_path
    result["NO_COLOR"] = "1"
    result["TERM"] = "dumb"
    return result


def _identity_probe_env(
    environ: MutableMapping[str, str], probe_environment: dict[str, str]
) -> dict[str, str]:
    """Add Kiro CLI's own credential and proxy env to a probe environment.

    Separate from :func:`_probe_env` so only the identity probe carries the
    credential and the (possibly credentialed) proxy configuration; see
    :data:`_IDENTITY_PROBE_ENV_KEYS` and :data:`_IDENTITY_PROXY_ENV_KEYS` for
    why ``whoami`` needs them and why ``--version`` must not get them.
    """

    result = dict(probe_environment)
    result.update(_allowlisted_env(environ, _IDENTITY_PROXY_ENV_KEYS))
    result.update(_allowlisted_env(environ, _IDENTITY_PROBE_ENV_KEYS))
    return result


async def _terminate_process(
    proc: asyncio.subprocess.Process,
    windows_descendants: dict[int, int] | None = None,
) -> None:
    group_signalled = False
    if platform_compat.IS_POSIX:
        if proc.returncode is None:
            try:
                # Resolve the group through the still-live leader instead of
                # signalling a retained numeric PGID that the OS could reuse.
                await platform_compat.kill_process_tree_async(
                    proc.pid,
                    platform_compat.SIGTERM,
                )
                group_signalled = True
            except (ProcessLookupError, OSError, ValueError):
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
    else:
        if proc.returncode is None:
            try:
                # asyncio retains the real Windows process handle, so this
                # targets the original process even if its PID is later reused.
                proc.terminate()
            except ProcessLookupError:
                pass

    leader_exited = proc.returncode is not None
    if not leader_exited:
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATION_GRACE_SECS)
            leader_exited = True
        except asyncio.TimeoutError:
            pass

    if platform_compat.IS_POSIX:
        if group_signalled and not leader_exited:
            # The live group leader anchors the PGID identity. Once it exits,
            # never signal that retained integer because POSIX may reuse it.
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                await platform_compat.kill_process_tree_async(
                    proc.pid,
                    platform_compat.SIGKILL,
                )
        elif not leader_exited:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    else:
        # Retained Windows process handles refer to the original kernel process
        # objects, unlike PIDs, which may be recycled during a long operation.
        for handle in tuple((windows_descendants or {}).values()):
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                platform_compat.terminate_process_handle(handle)
        if not leader_exited:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    if not leader_exited:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATION_GRACE_SECS)


async def _track_windows_descendants(
    proc: asyncio.subprocess.Process,
    tracked: dict[int, int],
    primary_root_handle: int | None = None,
    initial_snapshot: asyncio.Future[None] | None = None,
    primary_terminal_snapshot: asyncio.Future[None] | None = None,
) -> None:
    """Retain the complete Windows tree until every observed anchor exits."""

    primary_snapshot_pending = primary_root_handle is not None
    primary_terminally_scanned = primary_root_handle is None and proc.returncode is not None
    terminally_scanned: set[int] = set()
    try:
        while True:
            descendant_roots: list[tuple[int, int, bool]] = []
            for pid, handle in tuple(tracked.items()):
                active_before = platform_compat.process_handle_active(handle)
                if active_before or pid not in terminally_scanned:
                    descendant_roots.append((pid, handle, active_before))
            primary_active = (
                platform_compat.process_handle_active(primary_root_handle)
                if primary_root_handle is not None
                else proc.returncode is None
            )
            primary_needs_scan = (
                not primary_terminally_scanned
                if primary_root_handle is not None
                else primary_active
            )
            roots = (
                [(proc.pid, primary_root_handle, True, primary_active)]
                if primary_needs_scan
                else []
            ) + [
                (root_pid, retained_handle, False, active_before)
                for root_pid, retained_handle, active_before in descendant_roots
            ]
            if not roots:
                return
            for (
                root_pid,
                retained_root_handle,
                is_primary_root,
                root_active_before,
            ) in roots:
                initial_primary_snapshot = is_primary_root and primary_snapshot_pending
                try:
                    discovered = await platform_compat.descendant_termination_handles_async(
                        root_pid,
                        tracked,
                        retained_root_handle,
                    )
                    # The platform snapshot validates each numeric Toolhelp
                    # parent edge against exact-handle creation/exit times. A
                    # root that exits during discovery can therefore contribute
                    # genuine children without admitting a recycled PID's tree.
                    tracked.update(discovered)
                except (OSError, ValueError) as exc:
                    if initial_primary_snapshot and initial_snapshot is not None:
                        if not initial_snapshot.done():
                            initial_snapshot.set_exception(exc)
                        return
                    if (
                        is_primary_root
                        and not root_active_before
                        and primary_terminal_snapshot is not None
                    ):
                        if not primary_terminal_snapshot.done():
                            primary_terminal_snapshot.set_exception(exc)
                        return
                    raise
                root_active = (
                    platform_compat.process_handle_active(retained_root_handle)
                    if retained_root_handle is not None
                    else proc.returncode is None
                )
                if is_primary_root:
                    primary_terminally_scanned = not root_active_before and not root_active
                    if (
                        primary_terminally_scanned
                        and primary_terminal_snapshot is not None
                        and not primary_terminal_snapshot.done()
                    ):
                        primary_terminal_snapshot.set_result(None)
                elif root_active or root_active_before:
                    terminally_scanned.discard(root_pid)
                else:
                    terminally_scanned.add(root_pid)
                if initial_primary_snapshot:
                    primary_snapshot_pending = False
                    if initial_snapshot is not None and not initial_snapshot.done():
                        initial_snapshot.set_result(None)
            await asyncio.sleep(_WINDOWS_DESCENDANT_POLL_SECS)
    finally:
        if initial_snapshot is not None and not initial_snapshot.done():
            initial_snapshot.cancel()
        if primary_terminal_snapshot is not None and not primary_terminal_snapshot.done():
            primary_terminal_snapshot.cancel()


async def _unlink_off_loop(path: str | None) -> None:
    """Remove a sandbox launcher/profile without blocking the event loop."""

    if not path:
        return

    def _unlink() -> None:
        with contextlib.suppress(OSError):
            os.unlink(path)

    await asyncio.to_thread(_unlink)


async def _prepare_sandboxed_spawn(
    argv: list[str],
    *,
    mode: str,
    env: dict[str, str],
    extra_hidden_dirs: tuple[str, ...],
    extra_visible_dirs: tuple[str, ...],
) -> tuple[list[str], dict[str, str], str | None]:
    """Prepare filesystem-heavy sandbox state on a worker thread.

    Delegates to the shared :func:`shielded_prepare_off_loop`, which owns the
    shield-and-recover pattern (including the repeat-cancellation semantics of
    #5841) for every async caller of the chokepoint.  The chokepoint call itself
    stays in this module so the ``mode``/``strip_python_env`` policy — and this
    module's own seam over ``sandboxed_spawn_argv`` — remain local.
    """
    return await shielded_prepare_off_loop(
        functools.partial(
            sandboxed_spawn_argv,
            argv,
            mode=mode,
            env=env,
            strip_python_env=True,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
        )
    )


async def _run_process(
    command: str,
    args: list[str],
    *,
    env: dict[str, str],
    timeout_secs: float,
    sandbox_mode: str = _UNVERIFIED_SANDBOX_MODE,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> ProcessResult:
    """Run one fixed argv with bounded output, always inside the OS sandbox.

    There is no opt-out: every spawn this module makes is a probe or a Kiro auth
    call, and all of them are sandboxed. Nothing here may run a child
    unsandboxed.
    """

    if platform_compat.IS_POSIX and not _PROCESS_GROUP_SUPERVISOR_CODE:
        return ProcessResult(ok=False, error=_PROCESS_GROUP_SUPERVISOR_ERROR)

    output = ""
    cleanup_path: str | None = None
    creationflags = platform_compat.CREATE_NEW_PROCESS_GROUP
    if platform_compat.IS_WINDOWS:
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        spawn_argv = [command, *args]
        spawn_env = env
        if not platform_compat.IS_WINDOWS:
            # An absolute system `env` entrypoint prevents the generic sandbox
            # layer from mistaking an unverified candidate named `kiro-cli` for
            # the trusted provider spawn that may delegate to Kiro's internal
            # macOS sandbox. The candidate still executes inside the requested
            # outer sandbox with its original absolute path and argv.
            spawn_argv = ["/usr/bin/env", *spawn_argv]
            spawn_argv, spawn_env, cleanup_path = await _prepare_sandboxed_spawn(
                spawn_argv,
                mode=sandbox_mode,
                env=env,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
            if spawn_argv and not os.path.isabs(spawn_argv[0]):
                # Off the loop: a miss walks the whole ``PATH`` once per name to
                # report where the tool actually is, so a stalled network mount
                # anywhere on it would otherwise freeze the gateway rather than
                # just this probe.
                resolved_wrapper = await asyncio.to_thread(
                    platform_compat.trusted_system_bin, spawn_argv[0]
                )
                if not resolved_wrapper:
                    raise OSError(f"sandbox wrapper is unavailable: {spawn_argv[0]}")
                spawn_argv[0] = resolved_wrapper
        if platform_compat.IS_POSIX:
            # The immutable, gateway-captured supervisor is the outermost
            # process. Putting it inside the Linux namespace launcher makes the
            # two parent wait loops depend on each other; loading it from a
            # mutable package path would also let a same-UID agent replace code
            # immediately before an owner-triggered install.
            #
            # It also carries the resource limits (``--rlimits=``) that used to
            # ride on ``preexec_fn``. See resource_limit_supervisor_argv: a
            # preexec_fn forces a plain fork() of this multi-threaded gateway and
            # runs Python in the child before exec, which is how a child wedged
            # in a futex and pinned the fds it had inherited. The supervisor
            # applies the same setrlimits after exec, single-threaded, and the
            # exec'd child inherits them.
            spawn_argv = [
                sys.executable,
                "-I",
                "-c",
                _PROCESS_GROUP_SUPERVISOR_CODE,
                *resource_limit_supervisor_argv(),
                *spawn_argv,
            ]
        proc = await asyncio.create_subprocess_exec(
            *spawn_argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=creationflags,
        )
    except SandboxUnavailableError as exc:
        # The sandbox itself refused this spawn. Record it structurally so the
        # caller can report "present but unverifiable" without guessing from
        # host state — on Windows this wrap is skipped entirely, and the
        # allow_unsandboxed_exec opt-in bypasses it, so host capability is not
        # evidence about why THIS spawn failed.
        await _unlink_off_loop(cleanup_path)
        return ProcessResult(
            ok=False, error=str(exc), sandbox_failure=(exc.kind, exc.detail, exc.remedy)
        )
    except (OSError, RuntimeError) as exc:
        await _unlink_off_loop(cleanup_path)
        return ProcessResult(ok=False, error=str(exc))

    windows_descendants: dict[int, int] = {}
    windows_root_handle: int | None = None
    descendant_task: asyncio.Task[None] | None = None
    initial_snapshot: asyncio.Future[None] | None = None
    primary_terminal_snapshot: asyncio.Future[None] | None = None
    if platform_compat.IS_WINDOWS:
        # Anchor the primary kernel object before yielding after spawn. Without
        # this handle, a launcher that exits immediately could leave helpers
        # behind while its numeric PID is recycled before the first snapshot.
        windows_root_handle = platform_compat.duplicate_asyncio_process_handle(proc)
        if windows_root_handle is None:
            await _terminate_process(proc)
            await _unlink_off_loop(cleanup_path)
            return ProcessResult(
                ok=False,
                returncode=proc.returncode,
                error="could not retain the Windows process tree",
            )
        initial_snapshot = asyncio.get_running_loop().create_future()
        primary_terminal_snapshot = asyncio.get_running_loop().create_future()
        descendant_task = asyncio.create_task(
            _track_windows_descendants(
                proc,
                windows_descendants,
                windows_root_handle,
                initial_snapshot,
                primary_terminal_snapshot,
            )
        )

    async def _capture(stream: asyncio.StreamReader | None) -> None:
        nonlocal output
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            output = _append_capped(output, chunk.decode("utf-8", "replace"))

    stdout_task = asyncio.create_task(_capture(proc.stdout))
    stderr_task = asyncio.create_task(_capture(proc.stderr))
    operation_tasks = (stdout_task, stderr_task)
    wait_task = asyncio.create_task(proc.wait())
    cleanup_tasks = (*operation_tasks, wait_task)

    async def _wait_for_completion() -> None:
        completion_tasks: list[Awaitable[Any]] = list(cleanup_tasks)
        if descendant_task is not None:
            # A Windows launcher may exit after re-spawning its real work as a
            # child. Keep the retained-tree tracker in the success condition so a
            # zero exit cannot release live descendant handles while the child is
            # still running.
            # Shield the tracker from the operation timeout: the timeout path
            # still needs its latest retained handles while terminating the tree.
            completion_tasks.append(asyncio.shield(descendant_task))
        if initial_snapshot is not None:
            # A fast launcher and its readers cannot finish the operation until
            # the exact-object primary snapshot has settled.
            await initial_snapshot
        if primary_terminal_snapshot is not None:
            await asyncio.gather(primary_terminal_snapshot, *completion_tasks)
        else:
            await asyncio.gather(*completion_tasks)

    try:
        await asyncio.wait_for(
            _wait_for_completion(),
            timeout=timeout_secs,
        )
    except asyncio.TimeoutError:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        return ProcessResult(
            ok=False,
            output=output,
            returncode=proc.returncode,
            timed_out=True,
            error="process timed out",
        )
    except asyncio.CancelledError:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        raise
    except Exception as exc:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        return ProcessResult(
            ok=False,
            output=output,
            returncode=proc.returncode,
            error=str(exc),
        )
    finally:
        if descendant_task is not None:
            descendant_task.cancel()
            await asyncio.gather(descendant_task, return_exceptions=True)
        for handle in windows_descendants.values():
            platform_compat.close_process_handle(handle)
        if windows_root_handle is not None:
            platform_compat.close_process_handle(windows_root_handle)
        await _unlink_off_loop(cleanup_path)

    return ProcessResult(
        ok=proc.returncode == 0,
        output=output,
        returncode=proc.returncode,
        error="" if proc.returncode == 0 else f"process exited with code {proc.returncode}",
    )


async def _write_audit(
    *,
    action: str,
    outcome: str,
    caller: str,
    error: str = "",
    critical: bool = False,
) -> None:
    from kiro_crew.sel import sel

    def _write() -> None:
        sel().log_tool_invocation(
            session_key="dashboard:kiro-prerequisite",
            source="dashboard",
            tool_name=f"kiro_prerequisite_{action}",
            tool_kind="system_setup",
            outcome=outcome,
            error=error,
            metadata={"caller": caller[:100]},
            critical=critical,
        )

    await asyncio.to_thread(_write)


def _probe_filesystem_state(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Collect path/candidate state on a worker thread."""

    separator = ";" if platform_name == "win32" else os.pathsep
    # Setup discovery matches ACP resolution on every OS: a runnable Kiro CLI is
    # recognized wherever it lives (PATH, Scripts, override, package-manager
    # dir), since trust is "the CLI runs". Windows is not restricted to the
    # Program Files tree — a winget/scoop/user install that ACP would launch must
    # also be recognized by setup, or the two disagree and a user who already has
    # the CLI is sent back to Kiro's setup page.
    search_path = separator.join(
        known_kiro_cli_dirs(
            platform_name,
            home,
            environ,
            include_inherited_path=True,
        )
    )
    probe_environment = _probe_env(environ, search_path)
    candidates = find_kiro_cli_candidates(
        platform_name,
        home,
        environ,
        include_inherited_path=True,
    )
    return probe_environment, candidates


def _established_installation(data_home: Path) -> bool:
    """Recognize an existing Kiro Crew home when migrating onto the setup marker."""

    marker = data_home / _SETUP_COMPLETE_FILENAME
    if marker.is_file():
        return True
    for path in (data_home / "sessions", data_home / "history"):
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file() and child.stat().st_size > 0:
                        return True
        except OSError:
            continue
    return False


def _default_spec_lister() -> list[tuple[str, Path]]:
    """Production enumerator for :class:`KiroPrerequisiteService`'s spec probe.

    Delegates so path resolution and the ownership guard stay in ``agent.py``
    (see ``present_required_agent_specs``).
    """
    from kiro_crew.agent import present_required_agent_specs  # circular import

    return present_required_agent_specs()


def _unlaunchable_mcp_servers(spec_path: Path) -> str:
    """Return why *spec_path* declares an MCP server that cannot start, or ``""``.

    ``kiro-cli agent validate`` is deliberately not the only oracle here, because
    it is looser than the loader on one point that matters: an ``mcpServers`` entry
    that names no transport at all validates clean, and the server is then simply
    absent at runtime. The spec looks accepted while the session lacks the tools it
    declares — the same silent shape this probe exists to remove, reached through a
    different door.

    A server is launchable with EITHER a stdio ``command`` or a remote ``url``,
    matching the contract the custom-MCP handler enforces ("spec needs 'command'
    (stdio) or 'url' (remote)"). Treating a URL-only entry as unlaunchable would be
    far worse than the gap being closed: a valid remote server would force a
    healthy install into an unclearable readiness gate, and the only escape would
    discard the user's MCP tool selections.

    Structural only, and narrow on purpose: it reports an entry that names no
    transport whatsoever, or is not an object. It does not judge whether a command
    resolves or a URL answers — those are runtime questions with a different answer
    per host, and they belong to the binary.

    Fails open on an unreadable, oversized or non-JSON file: each is a different
    fault with its own reporting, and guessing here would put a second card on one
    problem. The read goes through the module's bounded helper rather than
    ``read_text`` so a pathologically large spec cannot pull the gateway into an
    OOM through the readiness probe.
    """
    raw = _read_bounded_regular_file(spec_path)
    if raw is None:
        return ""
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(spec, dict):
        return ""
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return ""
    for name, entry in sorted(servers.items()):
        if not isinstance(entry, dict):
            return (
                f"The MCP server {name!r} is not an object, so Kiro CLI cannot "
                f"start it and the session runs without its tools."
            )
        stdio = entry.get("command")
        remote = entry.get("url")
        launchable = (isinstance(stdio, str) and stdio.strip()) or (
            isinstance(remote, str) and remote.strip()
        )
        if not launchable:
            return (
                f"The MCP server {name!r} names neither a command to run nor a "
                f"url to reach, so Kiro CLI cannot start it and the session runs "
                f"without its tools."
            )
    return ""


class KiroPrerequisiteService:
    """Single-gateway coordinator for prerequisite probes and setup operations."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        environ: MutableMapping[str, str] | None = None,
        home: Path | None = None,
        data_home: Path | None = None,
        process_runner: ProcessRunner | None = None,
        audit_writer: AuditWriter | None = None,
        clock: Callable[[], float] | None = None,
        spec_lister: Callable[[], list[tuple[str, Path]]] | None = None,
        assume_ready: bool = False,
        warm_up_delay: float = _WARM_UP_DELAY_SECS,
    ) -> None:
        self._platform = platform_name or sys.platform
        self._environ = environ if environ is not None else os.environ
        self._home = home or Path.home()
        if data_home is not None:
            self._data_home = data_home
        elif home is not None:
            configured_home = self._environ.get("KIROCREW_HOME", "")
            self._data_home = (
                Path(configured_home).expanduser()
                if configured_home
                else self._home / ".kiro" / "crew"
            )
        else:
            self._data_home = config_dir()
        self._auth_staging_parent = _ensure_auth_staging_parent(self._home)
        self._setup_marker = self._data_home / _SETUP_COMPLETE_FILENAME
        (
            self._initial_override_path,
            self._initial_override_sha256,
        ) = register_process_start_override_attestation(
            platform_name=self._platform,
            environ=self._environ,
        )
        self._initial_setup_complete = _established_installation(self._data_home)
        auth_store_dirs = [
            mapping.source
            for mapping in _auth_store_mappings(self._platform, self._home, self._environ)
        ]
        # Kiro Crew's own secret home is always hidden from a probed CLI. The
        # credential-minimal probe additionally hides the identity stores; the
        # real-home callers must leave those visible — the readiness probe so a
        # CLI whose valid session lives outside the staged files (an external
        # auth helper resolved from the real home) can read its own credentials,
        # and device login so kiro-cli can WRITE its own credential store there.
        self._crew_hidden_dirs = tuple(
            dict.fromkeys(
                str(path)
                for path in (
                    self._data_home,
                    self._home / ".kiro" / "crew",
                    self._home / ".kirocrew",
                )
            )
        )
        self._hidden_probe_dirs = tuple(
            dict.fromkeys((*self._crew_hidden_dirs, *(str(path) for path in auth_store_dirs)))
        )
        self._probe_environment: dict[str, str] = {}
        self._run = process_runner or _run_process
        self._audit = audit_writer or _write_audit
        # Injected like the runner and clock above so a test states which specs
        # exist instead of inheriting whatever is in the developer's real agents
        # dir — otherwise spawn-count assertions vary by machine.
        self._spec_lister = spec_lister or _default_spec_lister
        self._clock = clock or time.monotonic
        self._assume_ready = assume_ready
        # `assume_ready` must hold from CONSTRUCTION, not from the first probe:
        # readiness is now a latch that `session_ready()` reads without probing,
        # so an offline/test gateway that never probes would otherwise report
        # not-ready and its blocking gates (e.g. /api/models) would 503.
        self._status = (
            PrerequisiteStatus(
                platform=_platform_label(self._platform),
                installed=True,
                authenticated=True,
                ready=True,
                initial_setup_complete=True,
            )
            if assume_ready
            else PrerequisiteStatus(
                platform=_platform_label(self._platform),
                initial_setup_complete=self._initial_setup_complete,
            )
        )
        self._warm_up_task: asyncio.Task[None] | None = None
        # Injectable so tests need not sleep out the real boot-contention delay.
        self._warm_up_delay = warm_up_delay
        self._probe_lock = asyncio.Lock()
        # Serializes spec repair. `operation_running` does NOT cover it: that
        # tracks `_task`, which only install/login set, so two concurrent owner
        # repair POSTs would both pass it. Without this lock the second rebuild
        # runs over the spec the first just wrote -- the exact
        # rebuild-over-an-existing-file case the main-spec gate exists to avoid,
        # which can drop a concurrent api_mcp_toggle write.
        self._repair_lock = asyncio.Lock()
        self._last_probe_at = 0.0
        self._has_probed = False
        self._viable_binary = ""
        # Which account the store named when the latch was last written.
        # Comparing it against a fresh read is how an out-of-band `kiro-cli
        # logout` (or a switch to another account) is noticed at all: nothing
        # else on the host reports it, and the store's mtime is useless because
        # ordinary chat traffic rewrites the database every few seconds.
        self._probe_identity = _AUTH_FINGERPRINT_ABSENT
        # The retirement consumer's own baseline: which account the RUNNING
        # children were started under. Separate from _probe_identity because two
        # consumers sharing one baseline means the first to observe a change
        # advances it and the second never sees it (see
        # identity_changed_since_sessions). None = never reconciled, which reports
        # CHANGED: an unknown baseline must not resolve to "the children match".
        self._session_identity: str | None = None
        # Real-time cache bounding the store reads and their SEL audit events.
        self._identity_cache = _AUTH_FINGERPRINT_ABSENT
        self._identity_cache_at = 0.0
        # Whether the relocation refusal has been logged. The relocated arm runs
        # on every identity poll, so the diagnostic logs once per service rather
        # than flooding; see current_identity_fingerprint.
        self._relocation_logged = False

    @property
    def initial_setup_complete(self) -> bool:
        """Whether this data home has already completed first-run setup.

        Derived from the data home at construction, so it is available BEFORE
        any CLI probe runs. Callers use it to classify a returning user without
        waiting on the probe, whose cold path spawns two sandboxed subprocesses
        (``--version``, then ``whoami``) and takes long enough for the dashboard
        to flash first-run setup chrome at someone who has used the app for
        months.
        """

        return self._initial_setup_complete

    def _snapshot_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = asdict(self._status)
        # See _LEGACY_IDLE_OPERATION: a pre-upgrade tab crashes without this key.
        result["operation"] = legacy_idle_operation()
        return result

    @staticmethod
    def _missing_agent_specs() -> list[str]:
        """The required agent specs absent from the agents dir (blocking call)."""
        from kiro_crew.agent import missing_required_agent_specs  # circular import

        return missing_required_agent_specs()

    async def _agent_spec_overlay(self, result: dict[str, Any]) -> dict[str, Any]:
        """Fold agent-spec presence into a snapshot, narrowing ``ready``.

        Applied on EVERY read, not only on a forced probe. That is affordable
        because it is two ``stat`` calls rather than the probe's two sandboxed
        ``kiro-cli`` spawns, so it does not violate the boot-and-explicit-action
        rule the subprocess probe follows (see :meth:`snapshot`). Off-loop anyway,
        matching this package's discipline of never doing filesystem work inline
        on the event loop.

        Only ever NARROWS: a missing spec turns ``ready`` off and
        ``repair_required`` on; a present spec grants nothing the probe did not
        already grant.

        Deliberately scoped to the dashboard-facing snapshot and NOT to
        :meth:`session_ready`. That predicate gates poll-driven spawn sites, and
        turn-starting paths intentionally do not block on it — they let the turn
        carry the failure, which now arrives as an actionable missing-spec message
        from ``acp/runtime.py`` instead of a raw JSON-RPC dict. Narrowing it here
        would re-introduce the stale-latch lockout that design removed.
        """

        if self._assume_ready:
            # ``assume_ready`` is the deliberate host-reality bypass (test mode,
            # fixtures, offline E2E). Those homes have no managed specs on disk, so
            # applying the overlay there would report a repair-required gate for
            # every such gateway.
            return result
        try:
            missing = await asyncio.to_thread(self._missing_agent_specs)
        except Exception:
            # Unreadable agents dir: report nothing rather than inventing a
            # missing spec and blocking a working install behind a repair card.
            logger.debug("Could not check Kiro Crew agent specs", exc_info=True)
            return result
        result["missing_agent_specs"] = list(missing)
        if missing:
            result["ready"] = False
            result["repair_required"] = True
        return result

    async def _repair_agent_specs(self) -> str:
        """Rewrite the managed agent specs. Returns the failure text, or ``""``.

        Reached ONLY from the ``POST /api/kiro-prerequisite/repair-specs`` handler,
        never from ``snapshot()``. That placement is load-bearing, not stylistic:
        the status route is an ``add_get``, and both dashboard barriers are
        method-scoped — ``csrf_middleware`` skips ``check_origin`` for
        ``{GET, HEAD, OPTIONS}`` and ``sel_audit_middleware`` logs only
        ``{POST, PUT, DELETE, PATCH}``. A write hung off the GET would therefore
        be cross-site triggerable (a ``SameSite=Lax`` cookie rides a top-level
        cross-site GET) and would leave no SEL record, while every sibling
        operation in this service — including the read-only probes — audits.

        Only rebuilds when the MAIN spec is absent, which keeps it out of a
        lost-update class: ``rebuild_agent_config`` regenerates the whole file and
        re-merges ``mcpServers`` under ``bridges._mcp_lock``, but NOT the
        ``tools``/``allowedTools`` half ``api_mcp_toggle`` writes separately — so
        rebuilding over an existing spec can drop a concurrent toggle's edit. With
        no file on disk there is nothing to lose (the toggle path itself bails
        with "Cannot read agent config, skipping sync").

        Failure is returned rather than raised: the caller reports it in the
        payload, which is the entire point — the boot path's swallowed exception is
        what made the original install undiagnosable. Sanitized like every other
        dashboard-facing string in this module, because the rebuild's call graph
        merges ``mcpServers[*].env``.
        """

        def _rebuild() -> None:
            from kiro_crew.agent import rebuild_agent_config  # circular import

            rebuild_agent_config()

        try:
            await asyncio.to_thread(_rebuild)
        except Exception as exc:
            logger.error("Agent spec repair from the readiness gate failed", exc_info=True)
            return _sanitize_detail(f"{type(exc).__name__}: {exc}")
        logger.info("Agent specs repaired from the readiness gate")
        return ""

    async def repair_agent_specs(self, caller: str = "") -> dict[str, Any]:
        """Repair the managed agent specs, then return the post-repair snapshot.

        The Check again button's repair arm, behind a POST so the write is
        origin-checked and audited (see :meth:`_repair_agent_specs`). Re-reporting
        alone could never help the install this exists to fix: the probe's two
        inputs (binary, ``whoami``) are both already true in that state.

        Returns a snapshot with ``agent_spec_repair_error`` set — empty on success.
        A no-op rebuild is reported as a failure rather than a success: the write
        can decline silently (``_decline_shared_agent_home`` returns early without
        raising), and reporting that as ``""`` would leave the gate showing a
        button that changes nothing on every press.
        """

        del caller  # the SEL record is written by the route's audit middleware
        # The missing-spec check and the rebuild must be ONE critical section. Read
        # outside the lock they are a TOCTOU: two concurrent repairs both observe
        # the spec missing, both rebuild, and the second one regenerates the file
        # the first just wrote. Re-reading inside the lock makes the loser a no-op.
        async with self._repair_lock:
            before = await self._agent_spec_overlay(self._snapshot_dict())
            missing_before = before.get("missing_agent_specs") or []
            # Read off the latched probe result because acceptance costs a
            # subprocess and the overlay above is deliberately stat-only.
            rejected_before = before.get("rejected_agent_specs") or []
            # A REJECTED spec is deliberately NOT rebuilt. The file exists, so a
            # regenerate would drop a concurrent api_mcp_toggle edit to the
            # tools/allowedTools half that rebuild_agent_config does not re-merge
            # (see _repair_agent_specs). Losing a user's tool grants to clear a
            # rejection is a worse outcome than the rejection, and the rebuild
            # cannot be made safe here: it already takes bridges._mcp_lock
            # internally, so wrapping this call in the same lock would deadlock.
            # Re-asking the binary is the honest action for the state, and it is
            # what the card's button offers.
            repairable = AGENT_FILENAME in missing_before
            if not repairable:
                if rejected_before:
                    # Not a no-op: acceptance is only re-answerable by the binary,
                    # and the stat-only overlay cannot ask it. force=True because
                    # the cached snapshot inside _PROBE_CACHE_SECS would just echo
                    # the rejection this is trying to re-test.
                    try:
                        await self._probe(force=True)
                    except Exception:  # noqa: BLE001 — stale state beats a 500
                        logger.warning("Re-probe of rejected agent specs failed", exc_info=True)
                    result = await self._agent_spec_overlay(self._snapshot_dict())
                    result["agent_spec_repair_error"] = ""
                    return result
                # Nothing to repair, only an auxiliary spec is missing (which the
                # main-spec gate deliberately excludes), or a concurrent repair
                # already wrote it. Report, do not write.
                before["agent_spec_repair_error"] = ""
                return before
            error = await self._repair_agent_specs()
            if not error and AGENT_FILENAME in rejected_before:
                # Acceptance is only re-answerable by the binary, and the overlay
                # cannot ask it. Re-probe so a rewrite that actually fixed the
                # rejection clears the card instead of leaving stale state up.
                # Affordable and in-policy here: this is the explicit-action half
                # of the probe's boot-and-explicit-action budget.
                try:
                    await self._probe(force=True)
                except Exception:  # noqa: BLE001 — a failed re-probe keeps stale state, not a 500
                    logger.warning("Re-probe after agent-spec repair failed", exc_info=True)
            result = await self._agent_spec_overlay(self._snapshot_dict())
            if not error and (result.get("missing_agent_specs") or []):
                error = (
                    "The rebuild reported success but the specs are still missing. "
                    "Run `kirocrew setup --agent-only --clean` on the gateway host."
                )
            result["agent_spec_repair_error"] = error
            return result

    def warm_up(self) -> asyncio.Task[None] | None:
        """Resolve readiness in the background shortly after gateway start.

        The probe spawns two ``kiro-cli`` subprocesses, so it yields
        ``_WARM_UP_DELAY_SECS`` first rather than running inline: racing those
        spawns against the concurrent app-backend spawns measurably lengthened and
        destabilized boot (~2.7s → 2.8-5.6s on one real home). Running it at all
        means the answer is usually already there when a session is first started.

        Returns the task so callers (and tests) can await it; startup itself does
        NOT await it, and failures are contained. A warm-up is strictly an
        optimization: the dashboard renders without waiting for it (readiness
        gates nothing in the UI), so it must never delay boot.
        """

        if self._warm_up_task is not None:
            return self._warm_up_task

        async def _warm() -> None:
            try:
                if self._warm_up_delay > 0:
                    await asyncio.sleep(self._warm_up_delay)
                await self._probe()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The lazy probe path stays authoritative and will retry, so a
                # failed warm-up must not fail gateway startup.
                logger.warning("Kiro readiness warm-up failed", exc_info=True)

        self._warm_up_task = asyncio.create_task(_warm())
        return self._warm_up_task

    async def snapshot(
        self,
        *,
        force: bool = False,
        coalesce: bool = False,
    ) -> dict[str, Any]:
        """Return the latched status, probing only on an explicit ``force``.

        Probing is boot-and-explicit-action only (see :meth:`session_ready`), so
        an ordinary poll reads latched state and spawns nothing. ``force=True``
        (the gate's Check again, and its blocking-state auto-poll) is the
        supported way to re-probe on demand.

        ``coalesce=True`` additionally floors the probe at
        :data:`_FORCED_PROBE_FLOOR_SECS`, and serves the latched answer without
        waiting when a probe is already in flight. It is for the MACHINE-driven
        force — the
        blocking gate's auto-poll, which every open tab runs independently — so N
        tabs collapse onto one probe per interval instead of multiplying the
        spawns. A human-driven force (Check again) passes ``coalesce=False`` and
        always probes: a button that silently returns a cached answer is a button
        that looks broken. The floor is likewise NOT applied in
        :meth:`verified_ready`, whose callers act irreversibly and carry their own
        freshness bound.
        """

        if force and coalesce and self._clock() - self._last_probe_at < _FORCED_PROBE_FLOOR_SECS:
            force = False
        if force and coalesce and self._has_probed and self._probe_lock.locked():
            # A probe is already in flight and a latched answer exists. The
            # machine poll must not queue behind ``_probe_lock`` here: the
            # identity probe can legitimately run for
            # _IDENTITY_PROBE_TIMEOUT_SECS (an IdP token refresh), and every
            # open tab polls every few seconds, so queueing would hang each
            # poll for the probe's whole remaining duration — a frozen gate,
            # exactly what the floor exists to prevent. Serving the latched
            # answer keeps the pane live; the in-flight probe refreshes it for
            # the next poll. A human Check again keeps queueing: it promised a
            # fresh answer, and it runs its own probe after the winner.
            force = False
        if force:
            # ``force=not coalesce`` is what actually coalesces a BURST. The floor
            # above is read outside ``_probe_lock``, so several tabs polling in the
            # same instant all see the same stale ``_last_probe_at``, all pass it,
            # and would each run a full probe in turn behind the lock — N tabs, N
            # probes, exactly what the floor exists to prevent. Handing the machine
            # poll ``force=False`` lets ``_probe``'s own cache recheck, which runs
            # INSIDE the lock after the winner refreshed the timestamp, drop the
            # queued callers. A human Check again keeps ``force=True`` and still
            # bypasses that cache, so the button never returns a stale answer.
            await self._probe(force=not coalesce)
        elif not self._has_probed:
            # Nothing has resolved yet (warm-up still pending or it failed).
            # One probe here is the boot probe, just arriving late.
            await self._probe()
        elif await self.identity_changed_since_probe():
            # The store now names a different account (or none) than the latch
            # describes. This is the one condition under which an ORDINARY poll
            # re-probes: without it the card keeps reporting the account the user
            # signed out of until they happen to press Check again, and reports
            # "not signed in" after a terminal sign-in for just as long. It stays
            # cheap because the trigger is a local read, not a spawn, and it
            # cannot loop -- the probe stamps the new identity, so the next poll
            # compares equal.
            await self._probe(force=True)
        result = await self._agent_spec_overlay(self._snapshot_dict())

        # The repair arm deliberately does NOT live here. This is an ``add_get``
        # route, and both dashboard barriers are method-scoped (csrf_middleware
        # skips check_origin for GET; sel_audit_middleware logs only
        # POST/PUT/DELETE/PATCH), so a write reached from a status read would be
        # cross-site triggerable and unaudited. It is a POST route instead:
        # ``repair_agent_specs`` / ``POST /api/kiro-prerequisite/repair-specs``.
        return result

    def mark_signed_out(self) -> None:
        """Latch readiness to signed-out after an observed ACP auth failure.

        The ACP attempt — not a probe — is what discovers a mid-session logout
        now that probing is boot-and-explicit-action only. Recording it here is
        what keeps the fail-closed gates (poll-driven spawn sites + destructive
        reruns) honest with no timer re-probe and no subprocess.

        Only ever *narrows* readiness (never grants it), and never touches
        ``initial_setup_complete``, so a returning user is not demoted to
        first-run setup. A running install/login operation owns the status, so
        this defers to it rather than racing its outcome.
        """

        if self._assume_ready:
            return
        if not self._status.ready and not self._status.authenticated:
            return
        self._status = replace(self._status, authenticated=False, ready=False)
        # Force the next explicit probe (Refresh / login) to actually run rather
        # than being served by the short cache off this synthetic transition.
        self._last_probe_at = 0.0
        logger.info("Kiro readiness latched to signed-out after an ACP auth failure")

    async def current_identity_fingerprint(self, *, allow_cached: bool = True) -> str:
        """Return the store's current identity fingerprint.

        Off-loop: a read-only SQLite open plus a couple of small queries.

        ``allow_cached`` decides whether a value read within
        :data:`_AUTH_FINGERPRINT_CACHE_SECS` may be reused. The cache exists for
        ONE caller -- the status surface, which N dashboard tabs poll every few
        seconds, so without it a poll storm becomes one SQLite read and one SEL
        audit event per tab per poll.

        Anything ACTING on the answer passes ``allow_cached=False``. A cached
        value is by definition up to a few seconds old, and a logout inside that
        window would otherwise be invisible to the turn that follows it -- the
        stale child would take the turn under the account just signed out. Turns
        and probes are human-paced, so reading fresh for them costs nothing worth
        having.

        What this replaces either way is the expensive signal: a ``whoami`` spawn,
        which is what the boot-only probe exists to keep off the send path.
        """

        now = time.monotonic()
        if (
            allow_cached
            and self._identity_cache_at
            and now - self._identity_cache_at < _AUTH_FINGERPRINT_CACHE_SECS
        ):
            return self._identity_cache

        def _read() -> str:
            # Both the relocation guard and the win32 path resolver stat the
            # filesystem, so the whole resolve-and-read runs in this worker
            # thread and stats stay off the event loop.
            if identity_store_is_relocated(self._platform, self._home, self._environ):
                # Do not read the default path: with the CLI pointed elsewhere, a
                # leftover database there would fingerprint an account nobody is
                # signed into, and a logout in the real store would change nothing
                # we can see. Absent is never reconciled, so this re-sweeps every
                # turn instead of trusting a stale file.
                if not self._relocation_logged:
                    # Once per service: this arm runs on every poll, and without
                    # the log an absent-because-relocated host is indistinguishable
                    # from a signed-out user -- the silence that made the identity
                    # probe's failures undiagnosable without reading source.
                    self._relocation_logged = True
                    logger.info(
                        "Kiro identity store is env-relocated on %s; reporting the "
                        "identity as absent instead of reading the fixed anchor",
                        self._platform,
                    )
                return _AUTH_FINGERPRINT_ABSENT
            return identity_fingerprint(
                kiro_identity_store_path(self._platform, self._home, self._environ)
            )

        try:
            fingerprint = await asyncio.to_thread(_read)
        except Exception:
            # An unreadable store reports "no identity", matching
            # identity_fingerprint's own contract, rather than "unchanged" --
            # guessing "unchanged" is what keeps a stale account alive.
            logger.warning("Kiro identity fingerprint could not be read", exc_info=True)
            fingerprint = _AUTH_FINGERPRINT_ABSENT
        self._identity_cache = fingerprint
        self._identity_cache_at = now
        return fingerprint

    async def identity_changed_since_probe(self) -> bool:
        """Whether the signed-in account differs from the one the LATCH describes.

        The status surface's consumer. Advancing this baseline is
        :meth:`_stamp_probe`'s business, so it must never be used to decide
        whether a running child is stale -- see
        :meth:`identity_changed_since_sessions` for why.

        False while ``assume_ready`` holds (a test or offline gateway asserts its
        own readiness and has no store to compare against) and false before the
        first probe, when there is no recorded identity to differ from.
        """

        if self._assume_ready or not self._has_probed:
            return False
        return await self.current_identity_fingerprint() != self._probe_identity

    async def identity_changed_since_sessions(self) -> tuple[bool, str]:
        """Whether running children predate the account the store now names.

        Returns ``(changed, live_fingerprint)``.

        This tracks a SEPARATE baseline from :meth:`identity_changed_since_probe`
        on purpose. One shared baseline has two consumers -- the status card and
        session retirement -- and whichever observes the change first advances it,
        so the other never sees it. In practice the dashboard polls status every
        few seconds while turns are minutes apart, so a single baseline means the
        poll re-probes, stamps the new identity, and the stale child is then never
        retired: the cheap half silently consumes the signal the consequential
        half exists to act on. A baseline per consumer removes the coupling.

        An UNSET baseline reports changed. It means we do not know which account
        the running children loaded, and "do not know" must not resolve to "they
        match": readiness is probed a few seconds AFTER boot while a session can be
        spawned eagerly before that, so a logout landing in the gap would otherwise
        be recorded as the starting point and the pre-logout child would keep
        answering as the previous account. Sweeping once when the baseline is unset
        costs one cold start per gateway lifetime -- the conversation is restored
        from disk -- and it is the only answer that cannot be silently wrong.

        The baseline is therefore advanced ONLY by
        :meth:`note_sessions_reconciled`, after a sweep that actually completed.
        """

        if self._assume_ready:
            return (False, _AUTH_FINGERPRINT_ABSENT)
        # Never cached: this answer decides whether a running child may take the
        # next turn, and a value a few seconds old would let a logout inside that
        # window pass unnoticed.
        live = await self.current_identity_fingerprint(allow_cached=False)
        if self._session_identity is None:
            return (True, live)
        return (live != self._session_identity, live)

    def note_sessions_reconciled(self, fingerprint: str) -> None:
        """Record that running children now match *fingerprint*.

        Advanced ONLY by the retirement path, so no other consumer can retire
        this baseline's signal on its behalf.
        """

        self._session_identity = fingerprint

    async def verified_ready(self, *, max_age_secs: float) -> bool:
        """Return readiness backed by a probe no older than *max_age_secs*.

        For callers that act IRREVERSIBLY on the answer — the destructive reruns
        (which rewrite persisted history) and the poll-driven ``kiro-cli`` spawn
        sites (which would open an interactive browser login) — the latch is not
        a sufficient authority in EITHER direction. It is written at boot and
        narrowed only when a chat turn observes an auth failure, so an external
        logout with no chat turn in between would leave it ``ready=True``
        indefinitely.

        Re-probing here is bounded: only these paths call it, never the message
        hot path, and ``_PROBE_CACHE_SECS`` collapses a burst of callers onto one
        probe. A running install/login operation owns the status, so defer to its
        current value rather than racing it.
        """

        if self._assume_ready:
            return bool(self._status.ready)
        if not self._has_probed or self._clock() - self._last_probe_at >= max_age_secs:
            try:
                await self._probe(force=True)
            except Exception:
                # A probe that cannot run is not evidence of readiness. Fail
                # closed: these callers must never act on a guess.
                logger.warning("Kiro verification probe failed; denying", exc_info=True)
                return False
        return bool(self._status.ready)

    async def session_ready(self) -> bool:
        """Return the latched readiness. Never probes; never blocks a turn.

        Readiness is resolved ONCE at gateway start (:meth:`warm_up`) and then
        refreshed only by an explicit user action — the dashboard's Refresh, or
        an install/login operation. There is deliberately no timer re-probe and
        no probe on the send path: a signed-out CLI is discovered by the ACP
        attempt itself, which raises ``AcpAuthRequired`` and surfaces an
        actionable sign-in error in the chat transcript.

        Because this value can therefore be arbitrarily stale, turn-starting
        callers treat it as ADVISORY and let the real ACP attempt be the
        authority (see ``dashboard/kiro_readiness.py``). Poll-driven
        ``kiro-cli`` spawn sites still gate on it — they have no turn to carry
        the failure, and an unauthenticated spawn opens a browser window on
        every poll.
        """

        return bool(self._status.ready)

    def _stamp_probe(self, identity: str) -> None:
        """Record that the latch was just written, and for which account.

        Every latch write in :meth:`_probe` goes through here so none can record
        a verdict without the identity it belongs to -- a latch stamped with no
        identity would read as "account changed" on the very next comparison and
        retire healthy sessions forever.

        Deliberately does NOT touch the retirement baseline. Seeding it here would
        look like it anchors that baseline to the identity the running children
        loaded, but the probe is delayed a few seconds after boot and a session can
        be spawned eagerly before it, so a logout in that gap would be seeded as the
        starting point and the pre-logout child would never be retired. That
        baseline is advanced only by :meth:`note_sessions_reconciled`, after a
        sweep -- and an unset one reports changed rather than assuming a match.
        """

        self._last_probe_at = self._clock()
        self._has_probed = True
        self._probe_identity = identity

    async def _probe(self, *, force: bool = False) -> PrerequisiteStatus:
        async with self._probe_lock:
            # Read the identity BEFORE running whoami, and stamp that value on
            # whatever verdict this probe reaches. The order matters: if the store
            # changes between this read and whoami, the latch records the OLDER
            # identity, so the next comparison sees a change and re-probes -- one
            # wasted probe. Reading afterwards would stamp the NEW identity onto a
            # verdict describing the OLD one, which hides the change instead.
            probe_identity = (
                _AUTH_FINGERPRINT_ABSENT
                if self._assume_ready
                else await self.current_identity_fingerprint(allow_cached=False)
            )
            if self._assume_ready:
                self._status = PrerequisiteStatus(
                    platform=_platform_label(self._platform),
                    installed=True,
                    authenticated=True,
                    ready=True,
                    initial_setup_complete=True,
                )
                self._stamp_probe(probe_identity)
                return self._status
            now = self._clock()
            if self._has_probed and not force and now - self._last_probe_at < _PROBE_CACHE_SECS:
                return self._status
            self._viable_binary = ""
            (
                self._probe_environment,
                candidates,
            ) = await asyncio.to_thread(
                _probe_filesystem_state,
                self._platform,
                self._home,
                self._environ,
            )
            # ACP resolves the first executable candidate. Probe that exact
            # candidate instead of skipping a broken entry and approving a
            # different binary than the session launcher will use.
            version_probe: ProcessResult | None = None
            for executable in candidates[:1]:
                result = await self._audited_probe(
                    "probe_version",
                    executable,
                    ["--version"],
                )
                version_probe = result
                if result.ok:
                    # Keep the discovered path AS RESOLVED (not realpath'd): a
                    # multiplexer launcher like ``~/.toolbox/bin/kiro-cli``
                    # dispatches on its argv[0] basename, so resolving the
                    # symlink to ``toolbox-exec`` would make whoami/login fail
                    # with "Command doesn't appear to be associated with any
                    # tool". This is the exact path ``--version`` just succeeded
                    # with and the one ACP launches.
                    self._viable_binary = executable
                    break

            if not self._viable_binary:
                # Was the probe refused BY THE SANDBOX, as opposed to failing for
                # any other reason? Verification runs the candidate inside the
                # sandbox (see _UNVERIFIED_SANDBOX_MODE), so on a host that
                # cannot build one a perfectly good, already-authenticated CLI
                # fails verification and must not be reported as missing.
                #
                # This keys on the typed failure the spawn actually raised, NOT
                # on whether the host has a backend. Host capability is not
                # evidence: _run_process skips the wrap on Windows, and the
                # allow_unsandboxed_exec opt-in bypasses it, so a broken CLI on
                # either would otherwise be blamed on the sandbox and lose the
                # repair actions that would genuinely help.
                sandbox_failure = version_probe.sandbox_failure if version_probe else None
                first_candidate = candidates[0] if candidates else ""
                # Off-loop: _is_runnable_executable realpath()s and stat()s the
                # candidate, and a stalled NFS/autofs mount would block those
                # syscalls indefinitely — on the event loop that freezes the whole
                # gateway, not just this probe.
                candidate_runnable = bool(first_candidate) and await asyncio.to_thread(
                    _is_runnable_executable, first_candidate, self._platform
                )
                if sandbox_failure is not None and candidate_runnable:
                    kind, detail, remedy = sandbox_failure
                    logger.warning(
                        "Kiro CLI at %s is present and executable but could not be "
                        "verified: the sandbox refused the probe (%s: %s)",
                        first_candidate,
                        kind,
                        detail,
                    )
                    self._status = PrerequisiteStatus(
                        platform=_platform_label(self._platform),
                        # Present and executable on disk. Verification is what
                        # failed, and it failed for an unrelated reason.
                        installed=True,
                        # Unknown, not false: whoami also runs through the probe
                        # path, so we cannot claim either way.
                        authenticated=False,
                        ready=False,
                        repair_required=False,
                        initial_setup_complete=self._initial_setup_complete,
                        sandbox_unavailable=True,
                        sandbox_failure_kind=kind,
                        sandbox_detail=detail,
                        sandbox_remedy=remedy,
                    )
                    self._stamp_probe(probe_identity)
                    return self._status
                if (
                    version_probe is not None
                    and version_probe.timed_out
                    and candidate_runnable
                ):
                    # A probe that never answered is not evidence of absence. The
                    # spawn was accepted and raised no typed failure, so the
                    # sandbox branch above cannot claim it, and falling through to
                    # the bare default below asserts installed=False about a binary
                    # this function just confirmed is present and executable.
                    #
                    # Reported as the SAME shape as a sandbox refusal, for the same
                    # reason: present on disk, verification is what failed. The
                    # timeout gets its own flag rather than reusing sandbox_* --
                    # nothing about the sandbox failed here, and a caller that keys
                    # a userns remedy off sandbox_unavailable must not be handed a
                    # slow filesystem to fix with an AppArmor profile.
                    #
                    # WARNING on the TRANSITION into the condition, DEBUG while it
                    # persists. This branch runs on EVERY probe, and on an affected
                    # host that is one probe per ~40s (the readiness gate's 30s
                    # staleness window plus this probe's own 10s timeout), so ~90
                    # lines/hour into the dashboard's 1000-entry log ring. A line that
                    # reports a slow host must not be the thing that evicts the rest of
                    # the evidence about it.
                    #
                    # The latched status IS the transition record, so this needs no
                    # timestamp and no re-warn floor: every probe rewrites
                    # ``self._status``, so a recovery re-arms the warning by itself and
                    # the next timeout speaks up. The dashboard gate needs the extra
                    # floor only because it has no object to hang state on and its
                    # clear path depends on a caller happening to observe the recovery.
                    if self._status.probe_timed_out:
                        logger.debug(
                            "Kiro CLI at %s still unverified: probe timed out again "
                            "after %ss (already reported)",
                            first_candidate,
                            _PROBE_TIMEOUT_SECS,
                        )
                    else:
                        logger.warning(
                            "Kiro CLI at %s is present and executable but could not be "
                            "verified: the probe timed out after %ss. The CLI may be "
                            "installed and signed in; this is not evidence it is "
                            "missing. Further timeouts log at DEBUG until it recovers.",
                            first_candidate,
                            _PROBE_TIMEOUT_SECS,
                        )
                    self._status = PrerequisiteStatus(
                        platform=_platform_label(self._platform),
                        installed=True,
                        # Unknown, not false: whoami runs through the same probe
                        # path, and on this host it is never even reached.
                        authenticated=False,
                        ready=False,
                        repair_required=False,
                        initial_setup_complete=self._initial_setup_complete,
                        probe_timed_out=True,
                    )
                    self._last_probe_at = self._clock()
                    self._has_probed = True
                    return self._status
                self._status = PrerequisiteStatus(
                    platform=_platform_label(self._platform),
                    initial_setup_complete=self._initial_setup_complete,
                )
                self._stamp_probe(probe_identity)
                return self._status

            # A viable binary answered ``--version``, so it can be signed into
            # (trust is "it runs"); ``whoami`` decides whether it is already
            # authenticated. No provenance gate: source/owner/path do not block
            # sign-in, so a runnable CLI never needs an unreachable "repair".
            # ``whoami`` decides whether the CLI is already signed in. Run it the
            # same way a real ACP session runs the CLI (see acp/runtime.py):
            # against the real environment/home, NOT a credential-minimal
            # rewritten HOME. A rewritten HOME breaks any CLI whose session or
            # tool registry lives in the real home — e.g. a multiplexer launcher
            # cannot even resolve itself without its real-home registry — so the
            # isolated probe reported such CLIs signed-out even though a real
            # session authenticates fine.
            whoami = await self._audited_identity_probe(
                self._viable_binary, isolate_home=False
            )
            if whoami.ok:
                await asyncio.to_thread(self._mark_setup_complete)
            # Acceptance is checked here, on the probe path, because it costs a
            # spawn per spec — the every-read overlay stays stat-only. Gated on a
            # successful identity probe so a broken or signed-out CLI is reported
            # as exactly that: asking a CLI that cannot authenticate whether it
            # likes our specs adds spawns and a second card for one fault, and its
            # answer would not be actionable until sign-in is fixed anyway.
            rejected: list[str] = []
            rejection_detail = ""
            acp_supported = True
            if whoami.ok:
                rejected, rejection_detail = await self._probe_spec_acceptance(
                    self._viable_binary
                )
                acp_supported = await self._probe_acp_support(self._viable_binary)
            self._status = PrerequisiteStatus(
                platform=_platform_label(self._platform),
                installed=True,
                authenticated=whoami.ok,
                # A required spec the CLI refuses fails every turn, exactly like
                # one that is absent, so it narrows readiness the same way. A CLI
                # without the ``acp`` subcommand cannot start ANY session, so it
                # narrows readiness too — but its remedy is an update, not a spec
                # rewrite, so it is tracked separately from ``repair_required``.
                ready=whoami.ok and not rejected and acp_supported,
                repair_required=bool(rejected),
                initial_setup_complete=self._initial_setup_complete,
                rejected_agent_specs=rejected,
                agent_spec_rejection_detail=rejection_detail,
                acp_supported=acp_supported,
            )
            self._stamp_probe(probe_identity)
            return self._status

    async def _probe_spec_acceptance(self, executable: str) -> tuple[list[str], str]:
        """Ask kiro-cli whether it accepts each required Kiro Crew spec on disk.

        Returns ``(rejected filenames, first reason)`` — both empty when every
        present spec is accepted.

        Complements :meth:`_agent_spec_overlay`, which answers whether the specs
        EXIST. A spec can be present and still be refused, and a refused spec is
        dropped from kiro-cli's agent table, so the product's own agent resolves
        to the default one with none of its MCP servers. Statting the file cannot
        see that; only the binary can answer it.

        Scoped to :data:`REQUIRED_KIRO_AGENT_FILES` because those are the specs
        whose rejection fails EVERY turn rather than disabling one feature, and to
        files that already exist because absence is ``missing_agent_specs``' job —
        reporting one fault under two names would put two cards on one problem.

        A spawn per spec, so it belongs to the probe's boot-and-explicit-action
        budget and must not be folded into the every-read overlay.
        """

        def _present() -> list[tuple[str, Path]]:
            return self._spec_lister()

        # Enumeration is delegated so path resolution and the ownership guard live
        # in one place (see present_required_agent_specs): an instance that may not
        # own these specs gets an empty list and spawns nothing.
        try:
            specs = await asyncio.to_thread(_present)
        except Exception:
            logger.debug("Could not enumerate Kiro Crew agent specs", exc_info=True)
            return [], ""
        rejected: list[str] = []
        detail = ""
        for filename, spec_path in specs:
            # Checked BEFORE spawning, because `agent validate` does not enforce
            # it: a spec whose mcpServers entry carries no launchable command
            # passes validate with no output at all, yet the server cannot start,
            # so the session runs without the tools the spec promises. Reading the
            # file is also cheaper than a subprocess, so a spec that fails here
            # costs no spawn.
            structural = await asyncio.to_thread(_unlaunchable_mcp_servers, spec_path)
            if structural:
                rejected.append(filename)
                if not detail:
                    # Sanitized like every other dashboard-facing string in this
                    # module: the text names the MCP server, and a server name is
                    # user-supplied, so it can carry a credential.
                    detail = _sanitize_detail(structural)
                continue
            result = await self._audited_probe(
                "probe_agent_spec",
                executable,
                ["agent", "validate", "--path", str(spec_path)],
            )
            if _SPEC_REJECTION_MARKER not in (result.output or ""):
                continue
            rejected.append(filename)
            if not detail:
                detail = _sanitize_detail((result.output or "").strip())
        return rejected, detail

    async def _probe_acp_support(self, executable: str) -> bool:
        """Ask kiro-cli whether it exposes the ``acp`` subcommand Kiro Crew uses.

        Returns ``True`` when the subcommand is present OR when the probe could
        not establish otherwise; ``False`` only on a CLEAN "unknown subcommand"
        rejection.

        Kiro Crew launches every session as ``kiro-cli acp ...`` (see
        ``acp.client`` / ``acp.runtime``). A CLI too old to have that subcommand
        runs fine and signs in fine, then fails at session-create with an opaque
        ``process exited (rc=None)`` — the same class of silent, hard-to-place
        failure the rest of this module exists to turn into an actionable card.
        ``acp --help`` is the read-only way to ask: a CLI that HAS the subcommand
        exits 0 and prints its help, one that lacks it exits nonzero with an
        "unknown subcommand" line (kiro-cli is a clap CLI).

        The verdict is deliberately conservative in ONE direction. A timeout, a
        sandbox refusal, or any nonzero exit whose text is not a recognized
        rejection is treated as SUPPORTED, so a probe that merely failed to run
        never blocks a working, up-to-date install behind an update card it does
        not need. The cost is that a genuinely-too-old CLI whose rejection wording
        is unrecognized would slip through here — but that install then fails at
        session-create with the pre-existing error, i.e. no worse than before this
        check existed, whereas a false positive would strand a healthy install.

        A spawn, so it belongs to the probe's boot-and-explicit-action budget and
        is gated on a successful ``whoami`` by the caller, matching the spec
        acceptance probe.
        """

        result = await self._audited_probe(
            "probe_acp_support",
            executable,
            [_ACP_SUBCOMMAND, "--help"],
        )
        if result.ok:
            return True
        # A probe that could not even run (sandbox refusal, timeout, spawn error)
        # is not evidence the subcommand is missing. Only a clean rejection counts.
        if result.timed_out or result.sandbox_failure is not None:
            return True
        haystack = (result.output or "").lower()
        return not any(marker in haystack for marker in _ACP_UNSUPPORTED_MARKERS)

    async def update_cli(self, caller: str = "") -> dict[str, Any]:
        """Run the CLI's own in-place self-update, then return a fresh snapshot.

        The Update button's action, behind an owner-gated POST so the spawn is
        origin-checked and audited. This is the ONE place this module runs a Kiro
        CLI subcommand that is not a read-only probe — justified because
        ``kiro-cli update`` is the CLI updating ITSELF in place (the same command
        the auto-update path in ``slack/gateway.py`` already runs unattended), not
        a remote installer script or a credential-writing flow, so it adds no new
        privileged surface. It is offered only to remedy a too-old CLI that lacks
        the ``acp`` subcommand.

        Returns a snapshot with ``cli_update_error`` set — empty on success. The
        error is returned rather than raised so the gate can render it in place,
        the same contract as ``repair_agent_specs``. Runs to completion within the
        request (bounded by :data:`_UPDATE_TIMEOUT_SECS`); a re-probe follows so a
        successful update clears the card instead of leaving stale state up.
        """

        del caller  # the SEL record is written by the route's audit middleware
        if self._assume_ready:
            # A test / offline gateway asserts its own readiness and has no real
            # CLI to update; running an update there is meaningless.
            result = await self._agent_spec_overlay(self._snapshot_dict())
            result["cli_update_error"] = ""
            return result
        # Resolve the binary the way the probe does, off-loop.
        probe_environment, candidates = await asyncio.to_thread(
            _probe_filesystem_state,
            self._platform,
            self._home,
            self._environ,
        )
        executable = candidates[0] if candidates else ""
        error = ""
        if not executable:
            error = "Kiro CLI could not be found to update."
        else:
            # The resolved binary is UNVERIFIED (candidates[0] is whatever sits
            # first on PATH; an agent that can plant ~/.local/bin/kiro-cli would
            # otherwise have it run against the real home). Run it under the
            # strict sandbox — the same posture verification uses for an
            # unverified candidate — so ~/.aws / ~/.ssh stay hidden even though
            # the owner clicked Update. Network reach for the self-update comes
            # from the proxy keys (which govern egress), NOT from the standard
            # sandbox's real-home exposure; the identity credential is
            # deliberately omitted because `update` fetches a binary, it does
            # not authenticate.
            update_environment = dict(probe_environment)
            update_environment.update(
                _allowlisted_env(self._environ, _IDENTITY_PROXY_ENV_KEYS)
            )
            await self._audit(
                action="update_cli",
                outcome="invoked",
                caller="gateway-setup",
                critical=True,
            )
            try:
                update = await self._run(
                    executable,
                    ["update"],
                    env=update_environment,
                    timeout_secs=_UPDATE_TIMEOUT_SECS,
                    sandbox_mode=_UNVERIFIED_SANDBOX_MODE,
                    # _hidden_probe_dirs (not _crew_hidden_dirs) so the unverified
                    # binary cannot read the Kiro identity token store either — the
                    # same isolation the read-only probe applies. `update` fetches a
                    # binary; it has no need for the on-disk identity credential.
                    extra_hidden_dirs=self._hidden_probe_dirs,
                )
            except asyncio.CancelledError:
                await self._set_terminal_audit(
                    "update_cli", "failed", "gateway-setup", "cancelled"
                )
                raise
            except Exception as exc:
                logger.warning("kiro-cli update failed to run", exc_info=True)
                await self._set_terminal_audit(
                    "update_cli", "failed", "gateway-setup", "update execution failed"
                )
                error = _sanitize_detail(f"{type(exc).__name__}: {exc}")
            else:
                if update.timed_out:
                    error = (
                        "kiro-cli update did not finish in time. Run "
                        f"`{KIRO_CLI_UPDATE_COMMAND}` on the gateway host directly."
                    )
                elif not update.ok:
                    error = _sanitize_detail(
                        (update.output or "").strip()
                        or f"kiro-cli update exited with code {update.returncode}"
                    )
                updated = update.ok and not error
                await self._set_terminal_audit(
                    "update_cli",
                    "completed" if updated else "failed",
                    "gateway-setup",
                    _terminal_audit_detail(update, updated),
                )
        if not error:
            # Re-probe so a successful update flips ``acp_supported`` / ``ready``
            # and the card clears. Explicit-action half of the probe budget.
            try:
                await self._probe(force=True)
            except Exception:  # noqa: BLE001 — stale state beats a 500
                logger.warning("Re-probe after kiro-cli update failed", exc_info=True)
        result = await self._agent_spec_overlay(self._snapshot_dict())
        if not error and not result.get("acp_supported", True):
            error = (
                "The update ran but this kiro-cli still has no `acp` command. "
                f"Update it manually with `{KIRO_CLI_UPDATE_COMMAND}` on the "
                "gateway host, or reinstall from the setup page."
            )
        result["cli_update_error"] = error
        return result

    async def _audited_probe(
        self,
        action: str,
        executable: str,
        args: list[str],
    ) -> ProcessResult:
        """Run one credential-free status probe with paired SEL lifecycle events."""

        await self._audit(
            action=action,
            outcome="invoked",
            caller="gateway-status",
            critical=True,
        )
        try:
            result = await self._run(
                executable,
                args,
                env=self._probe_environment,
                timeout_secs=_PROBE_TIMEOUT_SECS,
                sandbox_mode=_UNVERIFIED_SANDBOX_MODE,
                extra_hidden_dirs=self._hidden_probe_dirs,
            )
        except asyncio.CancelledError:
            await self._set_terminal_audit(action, "failed", "gateway-status", "cancelled")
            raise
        except Exception:
            # A probe that cannot even run means the candidate is not viable,
            # not that the gateway is broken. Degrade to a not-ok result so the
            # status endpoint stays a retryable "not ready" instead of a 500.
            logger.warning("Kiro %s probe failed to run", action, exc_info=True)
            await self._set_terminal_audit(
                action,
                "failed",
                "gateway-status",
                "probe execution failed",
            )
            return ProcessResult(ok=False, error="Kiro CLI probe could not run")
        await self._set_terminal_audit(
            action,
            "completed" if result.ok else "failed",
            "gateway-status",
            _terminal_audit_detail(result, result.ok),
        )
        return result

    async def _run_auth_command(
        self,
        executable: str,
        args: list[str],
        *,
        base_env: dict[str, str],
        timeout_secs: float,
        isolate_home: bool = True,
    ) -> ProcessResult:
        """Run one Kiro auth command against a sandboxed, trusted executable.

        The CLI is trusted because it runs (its install source, owner, and path
        do not gate sign-in); KiroCrew is not the authority on where Kiro CLI is
        installed. The user's installed binary is executed IN PLACE — never a
        private copy: Kiro CLI 2.15+ is a multi-call binary that exec's a sibling
        ``kiro-cli-chat`` resolved relative to its own path, so a copy into a
        flat staging dir fails with ENOENT.

        ``isolate_home=False`` runs against the user's real home (like an ACP
        session), so a CLI whose session/registry lives in the real home is
        detected and device login writes its own credential store where the CLI
        normally keeps it. ``isolate_home=True`` keeps a credential-minimal
        temporary HOME holding only Kiro identity files, for read-only probes
        that must never see the real ``~/.aws`` / ``~/.ssh``.
        """

        if not isolate_home:
            # Run the way a real ACP session runs the CLI (acp/runtime.py):
            # against the real environment/home under the standard OS sandbox. A
            # rewritten HOME breaks CLIs whose session or tool registry lives in
            # the real home (e.g. a toolbox multiplexer that resolves itself via
            # its real-home registry), so this is what actually detects them, and
            # it is where the CLI's own credential store belongs.
            #
            # SECURITY: this matches the accepted ACP launch posture, not a new
            # surface — ACP already runs the resolved kiro-cli with the full real
            # environment on every session (the standard sandbox intentionally
            # exposes AWS/SSH to it). Device sign-in writes kiro-cli's OWN
            # credential store in its own home; that write is the entire point of
            # delegating sign-in to the CLI, and it targets the same store an ACP
            # session already reads. KiroCrew stages nothing and publishes
            # nothing. Only Kiro Crew's own secret home is hidden, and the user's
            # binary runs IN PLACE, exactly as ACP runs it, so a multi-call CLI
            # can still reach its sibling subcommand executable.
            return await self._run(
                executable,
                args,
                env=base_env,
                timeout_secs=timeout_secs,
                sandbox_mode=_KIRO_AUTH_SANDBOX_MODE,
                extra_hidden_dirs=self._crew_hidden_dirs,
            )

        workspace = await asyncio.to_thread(
            _prepare_auth_workspace,
            self._platform,
            self._home,
            self._environ,
            base_env,
        )
        try:
            # The user's binary runs IN PLACE — never a copy staged under the
            # workspace. Kiro CLI 2.15+ dispatches subcommands by exec'ing a
            # sibling executable resolved relative to its own path, which a flat
            # copy destroys (ENOENT). Only the credential workspace is staged.
            return await self._run(
                executable,
                args,
                env=workspace.env,
                timeout_secs=timeout_secs,
                sandbox_mode=_KIRO_AUTH_SANDBOX_MODE,
                extra_hidden_dirs=self._hidden_probe_dirs,
                extra_visible_dirs=(str(workspace.root),),
            )
        finally:
            # Read-only probe home: nothing to publish, just remove it.
            await asyncio.to_thread(shutil.rmtree, str(workspace.root), ignore_errors=True)

    async def _audited_identity_probe(
        self, executable: str, *, isolate_home: bool = True
    ) -> ProcessResult:
        """Run an identity probe with paired SEL events.

        The readiness check calls this with ``isolate_home=False`` so ``whoami``
        runs against the real home (like an ACP session) and detects CLIs whose
        session or tool registry lives there. ``isolate_home=True`` keeps the
        credential-minimal temporary home for callers that need it.

        Either way the environment carries :data:`_IDENTITY_PROBE_ENV_KEYS`, so a
        host authenticated by Kiro CLI's own API-key variable is detected as
        signed in rather than reported "Not logged in".
        """

        action = "probe_identity"
        await self._audit(
            action=action,
            outcome="invoked",
            caller="gateway-status",
            critical=True,
        )
        base_env = _identity_probe_env(self._environ, self._probe_environment)
        if not base_env.get(CRED_KIRO_API_KEY):
            # Post-scrub Docker: the entrypoint moved the CLI's own credential
            # from the process environ into the data home's .env (mode 600), so
            # the environ allowlist above found nothing. Read it back from the
            # file — otherwise an API-key container reads as "Not logged in"
            # while ACP sessions (which get the same re-injection at spawn)
            # authenticate fine. Off-loop: file read.
            fallback_key = await asyncio.to_thread(
                read_env_file_credential, CRED_KIRO_API_KEY, self._data_home / ".env"
            )
            if fallback_key:
                base_env[CRED_KIRO_API_KEY] = fallback_key
        try:
            result = await self._run_auth_command(
                executable,
                ["whoami"],
                base_env=base_env,
                # The identity budget, not the shared probe ceiling: ``whoami``
                # may refresh an OIDC token against an organization IdP (see
                # _IDENTITY_PROBE_TIMEOUT_SECS). By the time this runs, the same
                # binary already answered ``--version`` inside the short budget,
                # so the extra headroom is never spent on a missing or hung
                # candidate.
                timeout_secs=_IDENTITY_PROBE_TIMEOUT_SECS,
                isolate_home=isolate_home,
            )
        except asyncio.CancelledError:
            await self._set_terminal_audit(action, "failed", "gateway-status", "cancelled")
            raise
        except Exception:
            # A whoami that cannot even run means "not signed in", not a broken
            # gateway. Degrade to a not-ok result so the status endpoint reports
            # authenticated=False (retryable) instead of surfacing a 500 that
            # flashes the full-screen "could not check Kiro CLI" error.
            logger.warning("Kiro identity probe failed to run", exc_info=True)
            await self._set_terminal_audit(
                action,
                "failed",
                "gateway-status",
                "probe execution failed",
            )
            return ProcessResult(ok=False, error="Kiro identity probe could not run")
        await self._set_terminal_audit(
            action,
            "completed" if result.ok else "failed",
            "gateway-status",
            _terminal_audit_detail(result, result.ok),
        )
        return result

    def _mark_setup_complete(self) -> None:
        if self._initial_setup_complete:
            return
        # restrict_to_owner=True locks the staged temp file down before any
        # content reaches it (0o600 on POSIX, an owner-only DACL on Windows), so
        # the published marker is owner-only from the instant the rename makes
        # it visible. The default restrict_on_error="raise" is what makes that
        # unconditional: a lockdown that fails aborts before the rename instead
        # of publishing the marker under the parent's inherited DACL.
        atomic_write(
            self._setup_marker,
            "complete\n",
            fsync=True,
            restrict_to_owner=True,
        )
        self._initial_setup_complete = True

    async def _set_terminal_audit(
        self,
        action: str,
        outcome: str,
        caller: str,
        error: str = "",
    ) -> None:
        try:
            await self._audit(
                action=action,
                outcome=outcome,
                caller=caller,
                error=error,
                critical=False,
            )
        except Exception:
            logger.warning("Could not write terminal Kiro setup audit event", exc_info=True)

    async def close(self) -> None:
        tasks: list[asyncio.Task[Any]] = []
        for task in (self._warm_up_task,):
            if task is not None and not task.done() and task not in tasks:
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
