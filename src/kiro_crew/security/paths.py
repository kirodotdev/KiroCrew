"""Keystone: the sensitive-path lists, the resolver, the gates.

This is the always-on floor. Every other tier can be narrowed on the argument
that the OS sandbox sees the same thing; these declarations are the record of
what must stay unreadable and unwritable to the agent no matter which sandbox is
in force, and the gates below are what roughly forty modules import by name.

The comments on the declarations carry more weight than the code does: each one
records why an entry sits on the read-plus-write floor rather than the write-only
tier, which is not recoverable from the list itself. Read them before adding,
moving or removing an entry.

Layered internally. Layer one is declarations, predicates, the bounded resolver
and the public path gates, and it reads nothing else in this package. Layer two is
the command-line orchestrator, :func:`is_sensitive_bash_command`: it composes the
egress tier and the rules catalog, both of which load-import layer one, so it
reaches them through call-time imports in the one body that needs them. The fence
between the two layers is a comment and this module's load-time import list, not
a second file.

The command-line gate does not match paths. Sensitive paths are enforced by the OS
sandbox on the agent's process tree and by :func:`is_sensitive_path` on every
resolved path the file tools open; a regex over the text of a shell command added
no protection on top of those and denied ordinary read-only commands, so the
orchestrator carries only the size ceiling, the IMDS detector and the
environment-credential detector.

The resolver is bounded on purpose. A path check runs synchronously on the event
loop against an agent-supplied token, so a stalled automount under that token
would wedge the gateway. Resolution is therefore off-loaded to a small dedicated
pool with a deadline, and a stall fails CLOSED for every path under the stalled
prefix until the mount answers again.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import platform
import re
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

from kiro_crew.agent_sdk import host_auth
from kiro_crew.executors import _MAX_PATH_RESOLVE_WORKERS, path_resolve_executor
from kiro_crew.identity_stores import (
    AUTH_SQLITE_DB,
    AUTH_SQLITE_SIDECAR_SUFFIXES,
    fenced_home_dirs,
)
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME

from .diagnostics import annotate_refusal, refusal_diagnostic

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# ── Sensitive Paths ──
# Directories and files that must never be read by the agent.
# Patterns are resolved relative to $HOME at check time.

#: Leaf specs below are authored with POSIX separators on every host; they are
#: split on this literal and re-joined with ``os.path.join`` so the same table
#: names the same files under Windows separators.
_LEAF_SEPARATOR = "/"


def _leaf_segments(spec: str) -> list[str]:
    """Path segments of a ``/``-authored leaf spec, for ``os.path.join``."""
    return spec.split(_LEAF_SEPARATOR)


_SENSITIVE_HOME_DIRS: list[str] = [
    # Gateway-owned Kiro auth staging. Owner-only filesystem mode does not
    # isolate another process running as the same UID, so every agent sandbox
    # and the shared read/write hook floor hide this fixed parent.
    ".kiro/crew-auth-staging",
    ".aws",
    ".ssh",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker/config.json",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # ACP adapter credential stores. Each adapter owns its own sign-in flow and
    # persists its own tokens; Kiro Crew never reads them, and only ever checks
    # that the file EXISTS so it can name the right sign-in command. An agent
    # that could ``fs_read`` one could impersonate the operator against that
    # vendor, so they are on the floor "precisely so nothing else does".
    #
    # DECLARED by each harness rather than spelled here
    # (``agent_sdk.host_auth.AGENT_AUTH_DECLARATIONS``), for the same reason the
    # identity-store splice below reads one canonical table: this fence, the
    # sandbox mask that compensates for it, and the sign-in advice the operator
    # is shown all have to name the same file, and the harness that shipped
    # selectable first shipped with its live token off this list because the
    # list was hand-maintained somewhere else. A harness declares what it
    # STORES; this module stays the one that decides what is fenced.
    #
    # Only the token leaf is declared. The sibling config files -- codex's
    # ``config.toml``, claude's ``settings*.json`` -- deliberately stay readable:
    # routing diagnosis needs them and they carry no credential.
    #
    # These are ``$HOME``-rooted defaults. A harness that honours a home override
    # declares the variable too, and every declared leaf is re-anchored under it
    # in ``_home_dir_targets_uncached`` so an override cannot move the token out
    # from under the gate.
    *host_auth.credential_leaves(),
    # (The Notes builtin's GitHub PAT lives under the crew data-home at
    # ``<prefix>/workspace/md-notebook/pat``; it is added below via
    # ``_CREW_SECRET_LEAVES`` so BOTH ``.kiro/crew`` and the legacy ``.kirocrew``
    # data-home are covered — ``config_dir()`` can resolve to either.)
    # Enterprise SSO cookie store. The public core ships no bundled SSO
    # integration, but the browser-auth layer already references this cookie
    # path (browser/auth.py), an edition CredentialPolicy redacts its session
    # token, and a companion IdentityProvider watches it for rotation. The cookie
    # is a live bearer credential: an agent that could fs_read it could
    # impersonate the user against every SSO-gated service. Classify the whole
    # directory so the cookie and its sidecars are covered. Generic and inert on
    # a host that does not have it — legitimate readers (the companion cookie
    # jar) open it directly + SEL-audited, not through this shared gate.
    ".midway",
    # kiro-cli / amazon-q auth stores hold the live SSO bearer token, read by
    # the dashboard credit pill via the audited kiro_usage_api._token_from_sqlite
    # helper. Classify the WHOLE data directories (not just data.sqlite3) so the
    # WAL/SHM/journal sidecars — which can hold the same credential bytes — are
    # covered too. Agent file tools must not read them through the shared gate.
    # The internal reader opens the DB read-only + SEL-audited (NOT via
    # is_sensitive_path), so it still works; the sandbox bind-mount list
    # (sandbox.py) is SEPARATE, so kiro-cli's own auth is unaffected.
    # The identity-store directories come from the single canonical table
    # (``identity_stores.IDENTITY_STORE_ROOTS``) so this fence and the five other
    # readers cannot drift apart. The splice emits all eight in table
    # order (``.local/share`` -> ``Library/Application Support`` ->
    # ``AppData/Local`` -> ``AppData/Roaming``, kiro-cli before amazon-q), and a
    # golden test freezes the final list against exactly that.
    #
    # Windows layouts: current kiro-cli writes the local, non-roaming app-data
    # directory (%LOCALAPPDATA% defaults to ~/AppData/Local); the Roaming entries
    # cover layouts that used %APPDATA% (defaults to ~/AppData/Roaming). These
    # matchers are home-anchored, so a profile redirected outside the home
    # directory is not covered -- the default location is what agent file tools
    # can reach by a fixed relative path.
    *fenced_home_dirs(),
]

# ── Kiro Crew's own data-home secrets & governance trust-root ──
# These leaves live directly under Kiro Crew's data home and must never be
# readable OR writable through the shared file gate (``is_sensitive_path``):
#
#   .env                          channel/API credentials (Slack/Discord/…)
#   browser-cookies.txt           reusable browser-auth session cookies …
#   playwright-storage-state.json … and the Playwright storage-state they become
#   sel_hmac.key                  Security Event Log HMAC key — signs the
#   security_events.jsonl         tamper-evident audit chain (``sel.py``);
#   trust                         the key now lives at ``trust/sel_hmac.key``
#                                 (owner-only dir OUTSIDE the log's directory);
#                                 the bare leaf covers pre-migration installs
#   app_admission.json            App Kit admission ceiling (apps/admission.py)
#   security_policy.json          governance ceiling (KEYSTONE, governance.py)
#   profiles                      per-surface governance profiles
#   admission_policy.json         signed-plugin admission trust root
#   denied_commands.json          denied-command opt-out ceiling — if an
#                                 auto-approved/YOLO agent shell could WRITE it,
#                                 it could set ``disable_all=true`` and defeat
#                                 the whole deny gate after a restart, so it must
#                                 be neither readable nor writable via any shell
#                                 form (operator edits it out-of-band via the
#                                 dashboard ``/api/security/…`` endpoints)
#   oauth_endpoints.json          operator OAuth consent-endpoint extension —
#                                 each entry widens the banner-only OAuth
#                                 entropy carve-out, so a writable file would
#                                 let the agent exempt an attacker host from
#                                 the exfiltration heuristics (operator edits
#                                 it out-of-band by hand)
#   token_signing.key             dashboard access/refresh token signing key
#   refresh_chains.json           refresh-token chain state
#   .local_secret                 internal MCP/cron/hook callback auth secret
#
# An agent that could fs_read any of these could forge auth tokens, impersonate
# internal callers, exfiltrate live sessions, or rewrite its OWN security
# ceiling; one that could fs_write them could neuter its governance/admission
# limits. Every legitimate reader/writer (token_secret.py, refresh_tokens.py,
# sel.py, apps/admission.py, governance.py, cli_commands.py, mcp_core.py, …)
# opens these directly (NOT via this gate), so real functionality is unaffected.
#
# Each leaf is expanded under EVERY known crew data-home prefix so the secret is
# gated identically whether it lives in the current home (``~/.kiro/crew``) or a
# pre-move legacy home (``~/.kirocrew``) that a user still has on disk. Keeping
# one leaf list means a new secret is added once and covered in both locations.
_CREW_HOME_PREFIXES: tuple[str, ...] = (".kiro/crew", ".kirocrew")
_CREW_SECRET_LEAVES: list[str] = [
    # Gateway diagnostics, for the reason the sandbox mask gives: the rows carry
    # frame labels, folded stacks and process detail, and the owner-gated read
    # routes redact them on the way out while the files themselves do not. A file
    # tool reading the directory would bypass that filter. Whole directory: the
    # day files rotate by name.
    "diag",
    ".env",
    # Owner-authored meetings edits are deliberately outside the meeting
    # directories agents write. They are returned verbatim to the owner and may
    # contain credential-shaped examples or private corrections, so an agent must
    # neither read nor overwrite them through file tools. The Meetings backend
    # opens this directory directly, so its save/overlay/revert flow is unaffected.
    "apps/meetings/data/edits",
    # The Notes builtin stores a GitHub Personal Access Token here so it can
    # push a vault. Owner-only mode (0600) does not isolate another process
    # running as the same UID, and the token is a live bearer credential for the
    # user's repositories, so it belongs behind the shared floor like every other
    # credential store. The app's own backend opens it directly rather than
    # through this gate, so it keeps working. It is a leaf here (not a flat
    # ``~/.kiro/crew`` entry) so it is generated for BOTH ``_CREW_HOME_PREFIXES``:
    # a user may still have a pre-move legacy ``.kirocrew`` home on disk holding a
    # live PAT, so it must be protected there too. A vault relocated with
    # ``MD_NOTEBOOK_HOME`` falls
    # outside a home-relative entry; the default path is what ships and what an
    # agent would find.
    "workspace/md-notebook/pat",
    # The WhatsApp channel's linked-device session store (whatsmeow's sqlite
    # keys). It IS the credential: anything that can read it can act as the
    # operator on WhatsApp, read every chat and send as them, with no second
    # factor and nothing on the phone to notice. Owner-only file modes do not
    # isolate another process running as the same UID, and a prompt-injected
    # agent's fs_read is exactly that process, so it belongs behind the shared
    # floor like every other credential store. Classified as the whole DIRECTORY
    # so the WAL and SHM sidecars, which hold the same key bytes, are covered
    # too. The channel's own client opens it directly rather than through this
    # gate, so pairing keeps working.
    "whatsapp",
    # The Notes builtin's vault registry. It is not a secret, but it stores each
    # vault's on-disk ``localPath``, which auto-sync trusts and runs ``git
    # add``/``commit``/``push`` against. A prompt-injected agent that could
    # rewrite this file would repoint a vault at an unrelated repository and have
    # the app commit and push work from it outside the hook controls, so the
    # agent must not be able to write it. The app's own backend opens it directly
    # rather than through this gate, so it keeps working.
    "workspace/md-notebook/vaults.json",
    # The Notes builtin's sync settings. ``autoSync`` here is the bit that
    # AUTHORIZES the background loop's unattended ``git push`` (using the
    # app's stored PAT), and ``autoSyncMins`` sets its cadence. A prompt-injected
    # agent that could write this file would flip on unattended pushing without
    # the operator's consent — the same escalation the ``vaults.json`` entry
    # above guards against, one step earlier. The user toggles it through the
    # HMAC-gated ``PUT /api/settings``; the app's own backend opens the file
    # directly rather than through this gate, so it keeps working.
    "workspace/md-notebook/settings.json",
    # The Notes builtin's write-staging directory. Every state writer above stages
    # its temp file in here before renaming onto its target, so during a write —
    # and after a crash between write and rename — a file in this directory holds
    # the same bytes as the leaves above, PAT included. Classified as the whole
    # DIRECTORY (like ``whatsapp``) so every temp name, present and future, is
    # covered. A TOP-LEVEL leaf, not one under ``workspace/md-notebook``, so an
    # agent-writable ancestor cannot be renamed out from under it. The app's own
    # backend opens it directly rather than through this gate, so writes keep
    # working.
    "md-notebook-staging",
    # Where the sandbox launcher stages the live-target pointer's absent-equivalent
    # stub before linking it into place. Classified as the whole DIRECTORY so the
    # in-flight temp is never a visible, linkable name: a second hard link to that
    # inode would be an unmasked path to the bytes the gateway executes. Only the
    # gateway process writes here.
    "live-target-staging",
    # The AWS Control builtin's app data directory. ``backup.json`` in here holds
    # ``nightly``, the bit that AUTHORIZES the app's startup loop to upload the
    # gateway's memory and workspace to S3 unattended, so a prompt-injected agent
    # that could write it would schedule an owner-billed export the owner never
    # asked for -- routing around the owner-only HTTP surface that is supposed to
    # be the only way to turn it on. Exactly the ``autoSync`` escalation above,
    # one app over.
    #
    # Classified as the whole DIRECTORY, not that one file, for the reason the
    # ``whatsapp`` entry above is: an atomic write goes through a temporary in the
    # same directory and is then renamed, so fencing only the final name leaves a
    # writable path to the same bytes. Its siblings (the cost cache, the library
    # ledger) have no legitimate file-tool reader either -- the app's own backend
    # opens every one of them directly rather than through this gate, so the app
    # keeps working and future state files are covered without a new entry.
    "apps/aws-control/data",
    # The same app's gateway-owned transfer staging. Not a secret store: it holds
    # object bytes in flight between the AWS CLI's write and the gateway's
    # read-back. It is fenced because the destination path is what a same-UID
    # agent would swap for a link to make the CLI -- which writes with the
    # gateway's reach -- land those bytes on a credential file. Every agent
    # sandbox masks the directory as well (``sandbox._CREW_HIDDEN_LEAVES``); the
    # one CLI spawn that must write into it is granted its per-call subdirectory
    # explicitly.
    "aws-control-staging",
    # Quarantine markers for auto-improvement clones whose rollback and retirement both
    # failed. A marker is the only durable record that a clone carrying a refused, unscanned
    # commit must not be reused, and an agent that could write or delete one could certify
    # its own poisoned clone. Masked at OS level too (`sandbox._CREW_HIDDEN_LEAVES`), since a
    # spawned shell's `open()` never routes through this gate.
    "quarantined-clones",
    "browser-cookies.txt",
    "playwright-storage-state.json",
    # The refused-inbound spool (messaging/inbound_spool.py). Not a secret: it is
    # an OUTBOUND SOURCE. Each entry names a conversation and carries text the
    # gateway posts on the next start, verbatim, in a restart notice to that
    # conversation -- so a file an agent could write is a way to send text of the
    # agent's choosing, as the gateway, into any conversation still authorized
    # for the principal it names. The egress recheck (may_send_to) narrows that
    # to authorized routes, which is not a boundary. Read matters too: an entry
    # holds the verbatim text of a message the operator sent, which is exactly
    # the private prompt content the rest of this floor exists to keep
    # unreadable.
    #
    # Classified as the whole DIRECTORY, for the reason the ``whatsapp`` and
    # ``apps/aws-control/data`` entries are: the spool is written by atomic
    # replace through a sibling temp name in the same directory, so fencing only
    # the final leaf would leave a writable path to the same bytes -- and the
    # lock file beside it is what serializes two concurrent refusals. The gateway
    # opens all of it directly rather than through this gate, so spooling and
    # the notice pass keep working.
    "inbound-spool",
    # The durable task queue (taskq/store.py): ``tasks/tasks.db`` plus its WAL
    # and journal siblings. Every row is another session's accepted work --
    # the task prompt, its parameters, its lease and generation -- and the
    # store is the scheduler's authority: an agent that could write it could
    # cancel or re-dispatch another session's task, or forge a claim. Whole
    # DIRECTORY (SQLite writes ``-wal`` / ``-journal`` / ``-shm`` siblings).
    # Every legitimate reader and writer is the GATEWAY process (the subagent
    # manager, the runner adapters, ``/api/tasks``), which opens the path
    # directly; the MCP tools reach the queue through ``/api/spawn``.
    "tasks",
    # Per-session work ledgers (session_ledger.py). Not credentials, but each
    # directory is one session's private work state, and the ledger's whole
    # authorization model is "a session reaches only its OWN ledger" (the HTTP
    # routes derive the target from the vetted caller identity). An agent's
    # auto-approved file tools would bypass that boundary sideways — any
    # session could read or corrupt any other session's ledger straight off
    # disk. Unlike the transcript files beside it, the ledger has no
    # legitimate file-tool reader: every legitimate access goes through the
    # backend module, which opens paths directly rather than through this
    # gate, so nothing breaks by fencing the whole subtree.
    "ledger",
    # The conductor work ledger (work_ledger.py) — same model, two parties: a
    # worker reaches only the ONE item it is bound to and a conductor only its
    # own directory, both derived from the vetted caller identity by the HTTP
    # routes. The worker agent carries the full default file toolset, so without
    # this entry those auto-approved tools reach every conductor's records
    # straight off disk, and a corrupted record reads as ABSENT to the store —
    # silent loss the conductor cannot see. No legitimate file-tool reader.
    "work-ledger",
    # The cross-process work root (``work_root``). Not credentials either: the
    # hazard is WRITE, and it is specific to this root's deterministic keys. A key
    # is an issue or pull-request number so a later run can compute it again, so a
    # name here is GUESSABLE in a way a random-suffixed scratch name is not. An
    # unmarked directory at a key is refused rather than adopted, but the marker
    # that lifts that refusal is a NAME IN THE CODE, so a writer inside this root
    # can plant the tree and the marker together and ``allocate_work`` then rejoins
    # it. Without this entry an agent's auto-approved file tools are such a writer:
    # they could furnish a key some job will use, and that job would take the
    # planted contents as its own prior state. The sandbox mask on the same leaf
    # stops a spawned subprocess; this entry stops the file tools, and the root
    # needs both because either alone leaves the other path open. The ``scratch``
    # root is deliberately NOT here: it is the agent's own sanctioned write area,
    # named to it by ``$KIROCREW_SCRATCH``. ``work_root`` opens these paths directly
    # rather than through this gate, so the sweep and every consumer keep working.
    "work",
    # Every append-only per-unit crew log, crew and session alike (crew_log/store.py).
    # Not credentials, but the design's whole premise is that the crew log is the
    # AUTHORITY and the context window only a cache: a conductor reads a unit's
    # history as fact instead of re-deriving it. An agent's auto-approved file
    # tools reaching this subtree would let it forge an entry attributed to the
    # gateway, or rewrite the history it is supposed to be reporting into, which
    # is the one thing an append-only record exists to prevent. The write-side
    # rules (type ownership, guest namespacing, seq under the lock) live in the
    # library, so they bind only callers who go through it; this entry is what
    # keeps a file tool from going around it, and the sandbox mask on the same
    # leaf is what keeps a spawned subprocess from going around BOTH. Named at the
    # shared ``crew-log`` root so every unit kind is fenced by one entry — session
    # crew logs included, which is why they do not live under the ``sessions``
    # transcript root. The store opens these paths directly rather than through
    # this gate, so nothing breaks.
    "crew-log",
    # The RETIRED root the same store used before it was renamed. Kept because this
    # rename ships no migration: a machine that ran the old build with the feature
    # flag on still has real entries under ``<home>/ledgers``, and dropping the leaf
    # would un-fence them from the agent's file tools on upgrade. Nothing writes here
    # any more, so the entry costs a retired name and refuses nothing legitimate --
    # the same reasoning the retired browser leaves above are kept under.
    "ledgers",
    # The optional Playwright extension token. It removes the browser-side approval
    # click for an attach, so a process that could read it could attach to the
    # operator's logged-in browser without them seeing a prompt. The gateway hands
    # it to the CLI through the environment, so nothing legitimate opens the file.
    "playwright-extension-token",
    # Gateway-executed browser launcher and its vendored package tree. Agent
    # subprocesses receive a READONLY sandbox view so their browser commands can
    # run it; file tools must not inspect or replace the executable the
    # unsandboxed gateway later uses for startup cleanup and owner launches.
    "playwright-cli",
    # Legacy SEL HMAC key location (pre-``trust/`` installs, and any stale file
    # a backup restore resurrects). Kept alongside the ``trust``
    # directory entry below so the key is gated at BOTH locations.
    "sel_hmac.key",
    # SEL trust-root directory: sel.py stores/migrates the audit chain's HMAC
    # signing key at ``trust/sel_hmac.key`` — OUTSIDE the log's directory, so
    # write access to the log dir does not imply re-signing power. The whole
    # dir is gated (like ``profiles``/``run``) so future trust-root material is
    # covered without a new entry. sel.py opens the key directly, not through
    # this gate.
    #
    # Spec Builder's decision record (``trust/spec-builder-decisions.json``) relies
    # on that whole-directory gating. The app refuses a second answer for a decision
    # it has recorded, so an agent able to write the file could erase an entry to
    # make a settled decision answerable again, or forge one to lock a decision the
    # user never answered. Gating the leaf alone was not enough: its parent under
    # ``workspace/`` was itself replaceable with one ``ln -s``, and the app opens the
    # path directly (as keystone writers must), so it would have followed the link.
    "trust",
    # Retained V1 member-memory binding records. The leaf name says "bindings",
    # but a binding FILE carries the RAW session key it binds: a record on disk
    # holds ``{"version": 1, "session_key": <raw>, "memory_store": ...}`` under
    # ``sessions/<digest>/`` and ``pids/``, so reading the directory hands over a
    # usable key rather than a digest of one. Memory V2 writes no such record and
    # migrates none, so an upgraded install keeps every one it already has and the
    # READ side needs this floor rather than write protection alone.
    #
    # ``sandbox._CREW_CHILD_WITHHELD_LEAVES`` classifies the leaf as one no child
    # may read; this entry is what makes that classification enforceable, because
    # ``agent_sdk.tool_gate.adapter_hidden_credential_dirs`` projects THIS floor
    # into an enforced adapter's OS mask rather than that list.
    #
    # The one legitimate reader, ``subagent_persistence.read_run_execution``,
    # opens the path directly in the gateway -- the keystone-reader pattern every
    # reader of a floor leaf uses -- so cold continuation of a retained V1 run is
    # unaffected. Crew's own sandbox keeps the directory OS-readable through
    # ``sandbox._CREW_READONLY_LEAVES``, which this entry does not touch: the
    # reader it fences is the AGENT'S OWN FILE TOOLS.
    "member-memory-bindings",
    "security_events.jsonl",
    # Rotated SEL segments. sel.py closes the live log at a size cap and renames
    # it into this directory, so a segment holds exactly the same audit records
    # the live file does and must be gated identically — a rotated log that the
    # agent could read (or rewrite, then let the chain re-anchor from) would make
    # rotation itself the way around the fence. Directory entry, so every
    # segment is covered without a per-name matcher. sel.py opens segments
    # directly, not through this gate.
    "security_events.d",
    "app_admission.json",
    "security_policy.json",
    "profiles",
    # The centrally-distributed ceiling's last-known-good cache
    # (``platform/policy_distribution.py``). Gated as a DIRECTORY, and the reason
    # is not just that it holds a copy of the policy: the sidecar metadata records
    # the SOURCE the copy came from, and the loader honours that source when
    # deciding whether the cache is this host's last-known-good. An agent able to
    # write here could publish itself a ceiling AND the provenance to make the next
    # boot adopt it — a full escape from ``security_policy.json`` being unwritable,
    # around the side. Read matters as much as write: the cache is a verbatim copy
    # of the policy the trust-root entry above exists to keep unreadable, so
    # leaving it open would hand over the exact deny patterns an evasion needs.
    # policy_distribution.py opens both files directly, not through this gate.
    "policy_cache",
    "admission_policy.json",
    "denied_commands.json",
    # The cron store. It holds access-control state, not just scheduling data:
    # ``session_key`` decides which session may manage a job through the MCP cron
    # tools and where the job's output is delivered, ``approval_mode`` is a
    # per-job auto-approval decision, and ``command``/``script`` decide what
    # gets executed on the host on a schedule. While the store sat outside the
    # protected leaves, an auto-approved shell could reassign ownership, flip a
    # job to auto-approve, or rewrite what a scheduled job runs with an ordinary
    # file edit — an open side door around the MCP tools' deliberate
    # cannot-write-``session_key`` rule and the ``self-protection-cron-adopt``
    # denied command, because those controls match command strings while the
    # state lives in the file. The gateway's own writers open the store
    # directly, not through this gate, so the cron service is unaffected; the
    # cost is that a human hand-edit through an agent shell is refused, the
    # same trade-off every other keystone leaf makes. The ``cron-history``
    # sidecar directory (per-job records plus the index) sits on the same floor:
    # it is a tamperable audit trail of those runs, and one directory rule
    # covers the records, the index, and the lock/temp files — the same
    # treatment ``webhooks`` and ``profiles`` already get.
    "crons.json",
    "cron-history",
    # The in-flight run markers (``cron_inflight``) belong on the same floor for
    # a sharper reason than the two above: the boot-time loop-stall breaker
    # TRUSTS them. One marker whose PID matches a cron-surface crash dump is
    # what makes the breaker park that job, so a marker an agent could write is
    # an unauthorized "pause this job" primitive that routes around both the MCP
    # cron tools and the owner-only HTTP surface, and a marker it could DELETE
    # disables the breaker for a crash loop that is about to recur. The evidence
    # an automatic state change rests on has to be at least as protected as the
    # state it changes, which is ``crons.json`` directly above. The service and
    # the doctor open the directory directly rather than through this gate, so
    # both keep working; nothing legitimate reads a marker through a file tool.
    "cron-running",
    # Saved workflow definitions are executable capabilities whose presence is
    # authorized only by an explicit dashboard action. Same-UID owner-only file
    # modes do not stop an agent file tool from planting or rewriting a valid
    # definition, so fence the whole directory, including atomic-write temp
    # files. The dashboard and workflow service open it directly and remain able
    # to create, list, update, and execute definitions.
    "workflow_library",
    # The crew appearance library: packs the user imported and a crew wears.
    # Only the gateway's owner-gated ``/api/appearances`` routes open it, and
    # they open it directly, so fencing it costs nothing in-process. Left off
    # this list, an agent's file tools could rewrite a manifest or erase the art
    # on any host: the sandbox bind-mask covers the Linux shell plane only, and
    # this list is what stops ``fs_write``/``fs_read`` on Windows and macOS.
    # Recovery is a re-import, but a prompt-injected agent corrupting user data
    # is the mainline threat these leaves exist for.
    "appearance-library",
    # The chat_tag authorization store. Grant rows decide which tags an agent
    # may self-apply, so agent file tools must neither read nor write them;
    # the OS-sandbox counterpart is ``sandbox._CREW_HIDDEN_LEAVES``. Only the
    # gateway opens the path.
    "tag-grants",
    # Crewmate teams (``crew_teams.py``): the owner's grouping of the roster. Not
    # a secret, but it decides which team view a crewmate's questions and work
    # roll up into, and a crewmate must not be able to move itself or a sibling.
    # Whole directory (``atomic_write`` temp sibling); the OS-sandbox counterpart
    # is ``sandbox._CREW_HIDDEN_LEAVES``. Opened only by the gateway and by the
    # operator's own ``kirocrew agent create`` / ``delete``, never by an agent tool.
    "crew-teams",
    # The operator's OAuth consent-endpoint extension
    # ({additional_authorization_endpoints: [{host, path}]}). Each entry widens
    # the banner-only OAuth entropy carve-out (_OAUTH_AUTHORIZATION_ENDPOINTS),
    # so this is a trust boundary of the same class as ``denied_commands.json``
    # directly above: an agent that could WRITE it could exempt an
    # attacker-controlled host from the exfiltration heuristics — widening its
    # own trust ceiling — and one that could READ it would learn which extra
    # hosts are exempt and aim there. Read+write blocked on both the tool path
    # (``is_sensitive_path``) and the shell forms. The only legitimate reader
    # (``_load_operator_oauth_endpoints`` in this module) opens the file
    # directly, not through this gate; the operator hand-edits it out-of-band
    # (there is deliberately no dashboard writer).
    "oauth_endpoints.json",
    # Per-session AgentCore Gateway inbound JWTs (directory name reserved
    # before the writer lands). Owner-only ``0600`` does not isolate another
    # process running as the same UID, so the directory belongs behind the
    # shared floor like every other credential store. Classified as the
    # whole DIRECTORY so atomic-write temps and every sidecar file are
    # covered.
    "agentcore-inbound",
    # Which checkout the gateway executes (Dev Fleet "Make live"). The pointer is
    # resolved during startup and exec'd into, so a writable one is arbitrary
    # code execution in the gateway's own identity — the agent must not be able
    # to author it, and must not be able to read it back to discover a target to
    # aim at either. The GATEWAY process writes it (Dev Fleet's in-gateway
    # cutover route, on the dashboard owner's request); the sandboxed Dev Fleet
    # backend does not touch the file at all and reads pointer state through
    # that route. The gateway's own startup reader opens it directly rather than
    # through this gate, so both keep working.
    "live_target.json",
    # Holds `backup/redaction.json`, the switch that decides whether a bundle
    # leaving this machine is redacted first. An agent that could write it would
    # turn redaction off and every later upload would carry the operator's
    # secrets verbatim; an agent that could read it learns whether the memory
    # store is currently being scrubbed. Flipping it is the attack and reading it
    # is reconnaissance, so this needs read AND write protection, not just write.
    #
    # The DIRECTORY is classified, not just the leaf inside it. Naming only the
    # leaf leaves the container writable, and a writable container is the same
    # hole one level up: replace `backup/` with a symlink and the protected leaf
    # now resolves somewhere unprotected, where the switch can be rewritten at
    # will. Restore's rollback copies live at `pre-restore-<ts>/`, not here, so
    # nothing legitimate is shut out, and the product's own reader opens the file
    # directly rather than through this gate.
    "backup",
    # The computer-use primary enable ({enabled, allowed_apps, extra_denied_apps}).
    # Same class of control as ``denied_commands.json`` directly above, and here
    # for the same reason: flipping ``enabled`` grants full desktop observation
    # plus keystroke/click synthesis into the operator's real applications — a
    # security ceiling, not a preference. Storing it in the agent-readable
    # ``config.json`` would leave it writable by any auto-approved agent shell,
    # so it lives here and gets read+write protection on the tool path
    # (``is_sensitive_path``) and read-only mounting by the OS sandbox for the
    # shell. The dashboard PUT handler is the only writer and it opens the path
    # directly, not through this gate, so the operator's Settings toggle still works.
    "computer_use.json",
    # Browser Mode's durable ENABLE gate. Same class of control as
    # ``computer_use.json`` directly above: while it is present the browse proxy
    # is registered and the ``browser_*`` tools are in the agent's tool list,
    # which lets the agent operate a real browser — and in attach mode that is
    # the operator's own running, logged-in browser. Presence alone is the
    # authorization, so a bare ``touch`` of this file would be a prompt-injected
    # self-grant of browser operation. It gets read+write keystone protection on
    # both the tool path (``is_sensitive_path``) and the shell forms (``touch``,
    # ``>``, ``tee``, extraction verbs). The dashboard PUT handler is the only
    # writer and opens the path directly, not through this gate, so the Settings
    # toggle still works. The sibling ``browser-engine`` leaf is protected too:
    # it selects the browser Playwright launches, so an agent-authored value
    # could steer the launch, and it must not diverge from the enable beside it.
    "browser-mode-enabled",
    "browser-engine",
    # Ops Mission Control's third-party provider tokens (PagerDuty / Datadog
    # API + application keys). These are live credentials against a user's
    # production incident tooling: a leaked one can acknowledge or resolve real
    # pages. They are here rather than in ``config.json`` for two concrete
    # reasons — an app's ``data/config.json`` is served over
    # ``/api/apps/<name>/config`` WITHOUT session auth, and ``config.json``
    # itself is writable by any auto-approved agent shell. The read+write
    # keystone floor is the only placement where the agent can neither read the
    # tokens nor overwrite them. The authenticated dashboard PUT handler is the
    # sole writer and opens the path directly, so Settings still works.
    "ops_mission_control_secrets.json",
    # Ops Mission Control's AUTONOMY CEILING: the app mode (observe/propose/act) and
    # the per-signal act-rules. This is the exact same class of control as
    # ``computer_use.json`` above — flipping ``mode`` to ``act`` plus adding a matching
    # rule is what authorizes a write against the user's production incident tooling —
    # and it was living in the agent-writable ``data/config.json``. A prompt-injected
    # agent could therefore mint the dashboard token, PUT ``mode=act`` with a rule
    # matching a signal, and unlock provider actions the operator never granted, which
    # defeats the app's central safety property (``effective = min(app_mode, rule_mode)``
    # is only a ceiling if the agent cannot raise it). Here for the same
    # reasons as the secrets leaf directly above — served unauthenticated over
    # ``/config`` and writable by any auto-approved shell in ``config.json`` — so it moves
    # to the read+write keystone floor. Dashboard PUT is the sole writer and opens the
    # path directly.
    "ops_mission_control_policy.json",
    # Recorded consent to call a PAID AWS service (Amazon Polly for TTS, Amazon
    # Transcribe for STT). Same class of control as ``computer_use.json`` above:
    # the record is what AUTHORIZES billable requests against a specific AWS
    # account, so an agent that could write it would consent on the operator's
    # behalf to spending the operator's money — and one that could write it
    # could also point the grant at an account of its choosing, which is the
    # unintended-account outcome the gate exists to prevent. Reading it is
    # fenced too: the file names the account id and caller ARN that a profile
    # resolves to, which is reconnaissance an agent should not get for free from
    # the shared gate. The authenticated dashboard ``/api/aws/consent`` handler is
    # the only writer and opens the path directly, not through this gate, so it
    # keeps working.
    "aws_service_consent.json",
    # Recorded consent to send conversation state to the external decision
    # provider (Jev). Same class of control as ``aws_service_consent.json``
    # above: the record is what AUTHORIZES message text and skill descriptions
    # to leave the machine, so an agent that could write it would consent on the
    # owner's behalf to its own egress. The authenticated, browser-only dashboard
    # ``/api/decisions/consent`` handler is the only writer and opens the path
    # directly, not through this gate.
    "decisions_consent.json",
    # Recorded consent to deliver a file whose contents the credential scanner
    # flagged. Same class of control as ``aws_service_consent.json`` above: the
    # record is what AUTHORIZES a flagged file past four independent content
    # gates, so an agent that could write it would consent on the owner's behalf
    # to shipping the owner's secrets. Reading it is fenced too -- the file says
    # which delivery destinations the owner has already blessed, which tells an
    # agent where a flagged file would land unrefused, and that is reconnaissance
    # it should not get for free from the shared gate. The authenticated,
    # owner-gated dashboard ``/api/file-delivery/consent`` handler is the ONLY
    # writer and opens the path directly, not through this gate, so it keeps
    # working; there is deliberately no CLI verb to fence.
    "file_delivery_consent.json",
    "ssh_auth_sock_consent.json",
    # The single-use step-up nonce that authorizes RECORDING a flagged-file
    # delivery grant. A whole DIRECTORY, not a leaf file, because arming writes a
    # sibling ``.tmp`` and renames it into place. It lives in its OWN top-level
    # leaf rather than under ``trust/`` on purpose: ``trust/`` is sandbox-VISIBLE
    # (it holds SEL append targets an in-sandbox MCP server writes), so a
    # prompt-injected agent could FORGE a nonce there with a runtime-constructed
    # shell path (the acknowledged evadable tier) and then drive the owner's
    # loopback browser to POST that chosen nonce -- recording a grant with no
    # human present. This leaf has NO in-sandbox reader (the gateway writes it on
    # arm, the host ``kirocrew file-delivery approve`` reads it), so it is also
    # bind-masked in ``sandbox._CREW_HIDDEN_LEAVES``; masking is what actually
    # closes the forge path, since the text/argv file gate alone does not stop a
    # runtime-constructed shell write.
    "file-delivery-consent-pending",
    "token_signing.key",
    "refresh_chains.json",
    ".local_secret",
    # Durable channel transport state: Teams' conversation -> serviceUrl and
    # identity -> conversation maps, and Telegram's getUpdates cursor. Two shapes of
    # the same control -- where a message GOES, and which messages are SEEN. Calling
    # getUpdates with an offset is also the ack for everything below it, so an agent
    # that could write that cursor would make the gateway skip every queued and
    # future message, durably, past the restart that would otherwise clear it.
    #
    # Same class of control as ``workspace/md-notebook/vaults.json`` above: neither
    # is a secret, both are PLUMBING. ``teams/transport.py`` resolves an explicit
    # ``user:<upn>`` send target through the identity map, so an agent that could
    # write it could point one operator's UPN at a different person's conversation
    # and have the next cron result, subagent notice or ``send_message`` delivered
    # there instead. The inbound path binds a ``serviceUrl`` to the JWT's own
    # ``serviceurl`` claim and ``connector_host_allowed`` re-checks it wherever the
    # Connector token is attached, but neither attestation survives PERSISTENCE,
    # and no host check can tell one legitimate conversation id from another.
    # Reading is fenced with writing because the file enumerates the operator's
    # UPNs and the conversations they use.
    #
    # A DIRECTORY entry, not the file leaf, and that is the load-bearing part: a
    # file leaf matches only its exact name, while ``atomic_write`` publishes
    # through a ``tempfile.mkstemp`` sibling (``tmpXXXXXXXX.tmp``) in the same
    # parent. With the store loose in the data-home root an agent watching that
    # directory could overwrite the temp file in the window before the rename and
    # have the rename publish its own routing. A directory entry covers every
    # child, random temp names included. (``trust``, ``profiles`` and
    # ``cron-history`` above are directories for the same reason among others.)
    # ``ServiceUrlStore`` and ``TelegramClient`` open their paths directly, not
    # through this gate, so
    # proactive routing across a restart is unaffected.
    "routing",
    # Inbound-webhook credential store directory. It holds the bearer HASHES and
    # the recoverable HMAC signing secrets for /api/hooks/agent, which is on the
    # dashboard-auth bypass list because it authenticates itself. An agent that
    # could WRITE this store could append a token hash it chose and then drive
    # arbitrary agent turns through that route from outside; one that could READ
    # it could sign requests as an existing integration. The store's own
    # reader/writer (webhooks.WebhookTokenStore) opens it directly, not through
    # this gate, so the feature is unaffected.
    #
    # The DIRECTORY is named, not the file, because the store is published with
    # mkstemp + os.replace: gating only ``tokens.json`` left the not-yet-renamed
    # ``*.tmp`` inode writable by a same-UID agent (0600 does not stop the same
    # user), and the rename would then publish agent-chosen content as the live
    # credential store. One directory rule covers the store, its lock file and
    # every temp file — the same treatment ``profiles`` and ``run`` already get.
    "webhooks",
    # Pinned installer provenance authorizes an executable to receive staged
    # Kiro identity credentials. Agent reads/writes must not be able to replace
    # this trust decision.
    ".kiro_cli_binary_trust.json",
    # MCP Apps spool (SEP-1865). Defense-in-depth: the per-render callback
    # capability (`callback_secret`) is delivered owner-WS-only and never
    # written to model-visible text, but the spool records also hold app HTML
    # and tool data, so the whole directory sits on the sensitive floor —
    # agent file tools cannot read it. Legitimate readers (gatewayd writer,
    # dashboard render/relay) open it directly in-process.
    "mcp-apps",
    # Runtime exec dir. ``run/`` holds paths the gateway executes OUTSIDE the
    # agent sandbox: the sandbox launcher scripts (``sandbox.py`` execs
    # ``python <home>/run/kirocrew_sandbox_*.py``) and the remote-instance
    # run-marker ``gateway-<port>.bin`` (``instances/run_marker.py``), whose
    # contents the SSH token-mint reads and ``exec``s on the remote host. A
    # prompt-injected / sandboxed agent that could WRITE into this dir could point
    # a marker (or a launcher) at an attacker-controlled binary and, on the next
    # routine token refresh, get it executed unsandboxed — a reachable sandbox
    # escape (owner + ``-x`` checks don't help; agent writes run as the same user).
    # Classify the whole dir read+write, like the other trust roots above. The
    # gateway's own writers open these paths directly and do NOT route through this
    # gate, so legitimate startup/spawn writes still work.
    "run",
    # Pi gate artifacts have asymmetric readers. The OS mask deliberately excludes
    # this directory from an enforced harness's credential mask so its child can exec
    # the launcher and read the sealed extension. This floor still keeps the agent's
    # own file tools out; the controls cover different readers rather than cancelling.
    "pi-gate",
    # Encrypted secret vault directory — denylists the entire subdirectory so
    # the key file, ciphertext store, lock, and atomic-write temp files are all
    # unreadable to the agent through any Kiro Crew-mediated channel.
    # The verb-independent sensitive-path backstop covers a scripted
    # ``python -c "open('~/.kiro/crew/.vault/...')"`` too.
    ".vault",
    # KAS-mode auth token store. In the KAS-embedded runtime Kiro Crew performs the
    # Kiro OIDC lifecycle itself (there is no kiro-cli), and persists the resulting
    # access/refresh tokens as ``0600`` files under this dir. They are live bearer
    # credentials for the model service, so — like every other credential store —
    # they sit behind the shared read+write floor: an auto-approved or sandboxed
    # agent must not be able to read the token back or overwrite it. The auth
    # module's own store opens these paths directly rather than through this gate,
    # so login/refresh keep working. Fence the whole ``kas`` dir (not just
    # ``kas/auth``): fencing only the leaf would let the agent rename ``kas`` and
    # then read the relocated token store from outside the fence.
    "kas",
    # The identity/auth SQLite store, named by the canonical filename constant
    # (``identity_stores.AUTH_SQLITE_DB``) rather than a fresh literal, so this fence
    # cannot drift from the readers that resolve the same store. It holds live bearer
    # tokens, so an agent that could read it could act as the user against the model
    # service, and one that could write it could forge the identity rows.
    #
    # The kiro-cli and amazon-q stores are fenced by DIRECTORY (``fenced_home_dirs()``
    # above), which covers each store's sidecars and temporaries for free. The crew
    # data home cannot be fenced the same way -- reading ``config.json`` and
    # ``sessions.db`` there is routine and intended -- so the store is named as a leaf
    # here, and the name is fenced BEFORE a writer for that location exists (the
    # treatment ``agentcore-inbound`` above gets): a fence that arrives with the
    # writer arrives one release after the first bytes it should have covered.
    #
    # The WAL/SHM/journal sidecars are spelled out for the reason the directory
    # entries do not have to be: a file leaf matches its exact name only, and a
    # sidecar carries the store's credential bytes -- ``kiro_cli`` documents the same
    # fact from the other side, that identity rows read as absent when the ``-wal``
    # sidecar is missing. (``.tmp``/``.lock`` publish artifacts in the same parent are
    # already covered by ``_KEYSTONE_ARTIFACT_SUFFIXES`` below.)
    #
    # Scoped to the crew data-home prefixes and NOT matched by basename:
    # ``data.sqlite3`` is a generic filename, so a basename rule would refuse an
    # unrelated application database anywhere under the home directory. No legitimate
    # reader is affected -- every identity-store reader (``kiro_usage_api``,
    # ``kiro_cli``, ``kiro_prerequisite``) resolves its path through
    # ``identity_stores`` and opens it directly, not through this gate.
    AUTH_SQLITE_DB,
    *(f"{AUTH_SQLITE_DB}{suffix}" for suffix in AUTH_SQLITE_SIDECAR_SUFFIXES),
    # Published crew webview RECORDS (agent_panel.py). Fenced because the record
    # is an OWNERSHIP claim the store reads back to refuse a colliding write, and
    # because it is the REDACTED copy of untrusted text: a direct write would
    # forge another crew's panel past both the ownership check and the redactors
    # in one step, and the drawer would render the result. The sandbox masks the
    # same leaf (sandbox._CREW_HIDDEN_LEAVES) so a runtime-constructed path inside
    # a sandboxed command cannot reach around this gate either.
    "crew-panels",
    # Managed memory uses bound tools. This directory guard keeps ordinary raw
    # file operations away from DB/WAL/SHM and manual context publication files;
    # glob-based project guidance skips it. It is a best-effort path guard, not
    # confidentiality against arbitrary code run by the same OS user. Memory
    # services open their captured store directly. Global V1 retains its existing
    # file access behavior. See docs/system-specs/modules/security.md.
    MEMORY_STORES_DIR_NAME,
]
_SENSITIVE_HOME_DIRS += [
    f"{prefix}/{leaf}" for prefix in _CREW_HOME_PREFIXES for leaf in _CREW_SECRET_LEAVES
]

# ── Publish artifacts of a keystone leaf ──
# Every leaf above is published through ``atomic_write``, which writes a
# ``tempfile.mkstemp(dir=path.parent, suffix=".tmp")`` sibling and renames it over the
# target; several stores also take a lock file beside the leaf they guard
# (``.policy.lock`` for the ops autonomy ceiling, ``ops_mission_control_secrets.json.lock``,
# ``.crons.lock``). Those siblings carry the SAME bytes as the leaf -- the temp holds the
# full payload for the whole write -- but a leaf entry matches its exact name only, so
# they sat outside the fence while the guarantee was stated as absolute.
#
# A DIRECTORY leaf never had this gap: its temps land INSIDE the fenced directory, where
# the ``startswith(target + os.sep)`` rule already covers them. That is exactly why
# ``webhooks``, ``routing``, ``.vault``, ``kas``, ``run``, ``cron-history`` and
# ``apps/aws-control/data`` are written as directories, and their comments say so. The gap
# is the leaves whose parent is NOT itself fenced -- in practice the crew data-home root,
# which cannot simply be fenced wholesale because reading ``config.json`` and
# ``sessions.db`` there is routine and intended (see ``_WRITE_PROTECTED_HOME_PATHS``).
#
# So the fence is DERIVED FROM the leaf declarations rather than restated per leaf: an
# artifact-shaped name sitting in the parent directory of any keystone leaf is protected.
# A leaf added later inherits the protection with no second entry to remember, which is
# the only version of this that stays true -- the reason the gap existed at all is that
# the exception was invisible at every call site.
#
# Derived from ``_CREW_SECRET_LEAVES``, deliberately NOT from ``_SENSITIVE_HOME_DIRS``:
# that list also carries ``.aws``, ``.ssh`` and the kiro-cli identity stores, whose parent
# is ``$HOME`` ITSELF, so deriving from it would fence ``~/*.tmp`` and ``~/*.lock`` across
# the user's entire home directory.
#
# Keyed on the artifact SHAPE, not on ``<leaf>.tmp``: the real mkstemp name is
# ``tmpXXXXXXXX.tmp`` and carries no leaf name at all, so a leaf-derived temp name would
# fence a spelling no writer produces. ``<leaf>.lock`` IS a real shape
# (``ops_mission_control_secrets.json.lock``), and the suffix rule covers both.
#
# Not included: ``deploy/pending-deploys.lock``. Its directory holds no keystone leaf, so
# there is no keystone payload beside it for the fence to protect.
_KEYSTONE_ARTIFACT_SUFFIXES: tuple[str, ...] = (".tmp", ".lock")
_KEYSTONE_ARTIFACT_PARENTS: list[str] = sorted(
    {
        # Every entry is ``<crew-prefix>/<leaf>`` so it always contains a separator,
        # making the rsplit safe: a bare leaf yields the crew home root, a path-shaped
        # leaf yields its own directory (``workspace/md-notebook``).
        f"{prefix}/{leaf}".rsplit("/", 1)[0]
        for prefix in _CREW_HOME_PREFIXES
        for leaf in _CREW_SECRET_LEAVES
    }
)

# ── Write-protected paths (block modification, allow reads) ──
# Runtime config files carry security-relevant resource ceilings (concurrent
# subagents, per-agent turn budget, warm-pool size). A prompt-injected agent
# with file-write access must not be able to rewrite these to inflate its own
# limits and drive host resource exhaustion (pentest — config-loader bound
# bypass, recommendation: block agent tools from modifying config files).
#
# They are DELIBERATELY NOT in ``_SENSITIVE_HOME_DIRS`` above: that list is the
# shared read+write gate, and reading config.json is routine and intended (the
# dashboard file viewer, ``cat``, and knowledge indexing all read it). We
# instead block only WRITES, at the agent file-edit tool gate
# (hooks.on_tool_call), via ``is_sensitive_write_path``. This is defense in
# depth on top of the loader's load-time clamp, which already neutralizes any
# inflated on-disk value no matter how it was written. The operator edits config
# out-of-band (dashboard config API / CLI), which do NOT route through this
# gate, so legitimate config changes still work.
# (The denied-command opt-out state does NOT live here — it is a security
# ceiling and lives on the read+write keystone floor in ``denied_commands.json``
# above, so no bash-level write matcher is needed for it. The computer-use primary
# enable is on that same floor, for the same reason.)
#
# SCOPE LIMIT worth stating where the matchers live: every path matcher in this
# module reasons about a PATH STRING. Computer use reaches state that has no path
# — a password field's ``AXValue``, a logged-in banking tab, an editor window
# already showing ``~/.aws/credentials`` as pixels and as accessibility text. No
# addition to either list here can see any of it. That is why
# ``computer_use/policy.py``'s bundle-id denylist (terminals, password managers,
# keychains) and its secure-subrole refusal are load-bearing security controls in
# their own right rather than conveniences, and why the always-on secure-field
# redaction has no policy key.
_WRITE_PROTECTED_HOME_PATHS: list[str] = [
    f"{prefix}/{leaf}"
    for prefix in _CREW_HOME_PREFIXES
    # config.json / config.local.json: security-relevant resource ceilings.
    # playwright-cli-config.json: the browse launch config
    # (browser_cli/launch.py). It holds no secret and the CLI must READ it on
    # every invocation, so it is write-protected rather than sensitive. But it is
    # an INPUT TO A SECURITY DECISION: the schema accepts
    # ``launchOptions.chromiumSandbox``, so an agent that could rewrite it would
    # turn the browser sandbox OFF for every later browse, and the change persists
    # until the next gateway start re-converges the file. Kiro Crew generates it
    # directly and does NOT route through this gate, so its own write still works.
    # subagents: the canonical run records a cold continuation restores app
    # ownership from. Gateway writers bypass this tool gate; an agent may read a
    # run's results but cannot turn an app-owned run into a personal run. Its
    # retained V1 companion ``member-memory-bindings`` belongs on the read+write
    # floor above instead, because a legacy binding record carries a raw session
    # key while a V2 run's results live in the run record here -- so the agent may
    # NOT read that leaf, and an entry on this tier as well would make
    # ``security_posture`` publish "Reads allowed" for a path the read gate
    # refuses.
    for leaf in (
        "config.json",
        "config.local.json",
        "playwright-cli-config.json",
        "subagents",
    )
] + [
    # Ops Mission Control's on-call schedule. WRITE-protected, not read+write
    # sensitive: it holds no secret and every teammate's instance must READ it to
    # answer "am I on call?", so classifying it as sensitive would break the
    # feature. But it is an INPUT TO AN AUTHORIZATION DECISION — an agent that
    # could rewrite it to name its own login would make
    # ``rotation.authorize_action`` -> ``_definitely_off_shift`` accept its own
    # forged shift and execute an off-shift production write against a teammate's
    # tooling.
    #
    # This is the last of five instances of one class on this app's off-shift
    # refusal (the others: the GitHub login, the strict-gating flag, the
    # provider-config field list, and ``providers.<id>.enabled``). The fix is
    # placement, not logic: the app READS the schedule exactly as before, and only
    # the agent's own file-edit tool is refused. `ledger_sync` writes it through
    # a direct `git checkout` on the merge path, not through this gate, so team
    # sync still converges.
    f"{prefix}/apps/ops-mission-control/data/rotation.yaml"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The cloud launcher's config. WRITE-protected for the same reason as
    # ``playwright-cli-config.json`` above and by the same placement-not-logic fix:
    # it holds no credential (its own module docstring is explicit that it stores a
    # profile NAME, and the Fargate block stores secret names and ARNs, never
    # values), and the gateway must READ it on every request to build the remote
    # provisioner list, so sealing it against reads would break the Set-up tab.
    #
    # But it is an INPUT TO A SECURITY DECISION. ``fargate.image`` chooses the
    # container image a launch runs, and the task's execution role delivers the
    # model credential into that container before it starts. An agent that could
    # rewrite this file could name a digest-pinned image of its own -- the digest
    # rule constrains the FORM of the reference, not who owns the registry -- and
    # leave every other field the owner wrote intact, so the owner's next launch
    # hands the credential to an image the owner never chose.
    #
    # Nothing in the product writes it: the launch path's own profile, region and tag
    # live in ``cloud.launch_state``, so this leaf has no product writer to exempt.
    f"{prefix}/cloud.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The cloud launcher's LAUNCH RECORD (``cloud/launch_state.py``): the profile, region
    # and tag the last launch decided. WRITE-protected on the same footing as
    # ``cloud.json`` above, and for a reason that is specific rather than inherited: the
    # tag in it is what ``kirocrew cloud destroy`` resolves when no ``--tag`` is given, so
    # an agent that could write this file could choose which of the owner's CloudFormation
    # stacks a ``destroy --yes`` deletes. Interactive ``destroy`` describes the instance
    # and asks first, but ``--yes`` is exactly the path that does not.
    #
    # Readable, like ``cloud.json``: every ``cloud`` subcommand resolves the tag from it,
    # and sealing it against reads would break them all. The gateway and the CLI write it
    # outside the agent's file-edit gate, so the launch path is unaffected.
    f"{prefix}/cloud_launch_state.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The Ops Mission Control incident INDEX, for the same reason as the schedule above and
    # with the same read/write asymmetry: every teammate's instance reads it constantly (it is
    # the claim ledger and the board), so classifying it sensitive would break the app, but it
    # is an INPUT TO AN AUTHORIZATION DECISION.
    #
    # ``/incident/action`` looks the incident up by id and hands ``incident.signal`` to
    # ``rotation.authorize_action``, whose ``AutonomyRule.matches`` keys on
    # ``signal.source``/``resource``/``labels``. An agent that can rewrite this file can pair a
    # resource an operator's rule authorizes (``resource="prod-db-1"`` matching
    # ``resource_glob="prod-*"``) with a DIFFERENT provider target in ``labels`` — so the gate
    # approves one signal while the sink mutates another, and the authorization describes a
    # signal that does not exist. That is the same defect already fixed on ``/incident/claim``
    # by resolving the signal server-side; this is the same forgery reached through the store
    # instead of the request body, which server-side resolution cannot help with because the
    # store IS the server's copy.
    #
    # The gateway's own writers (``store.claim``/``update_fields``, the reconcile SOP) open
    # this path directly and do not route through this gate, so the app keeps working; only
    # the agent's file-edit tool is refused.
    f"{prefix}/apps/ops-mission-control/data/incidents/index.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # Downloaded MODEL WEIGHTS (speech recognition and embeddings both land here).
    # WRITE-protected as a whole directory, not read+write sensitive: the weights hold
    # no secret, and the settings surface and `kirocrew doctor` both read the directory
    # to report what is installed.
    #
    # They are an INPUT TO A TRUST DECISION. Each store verifies its file against a
    # pinned sha256 and then hands the PATH to a native loader, so a writable directory
    # leaves a window between the digest and the open in which the bytes can be
    # swapped -- and no amount of re-hashing closes it, because the loader re-opens by
    # name. Removing the writability removes the window instead: the agent cannot
    # modify the file at all, so the verified bytes are the loaded bytes. A poisoned
    # model is persistent and invisible, and for speech it means the user's own words
    # reaching the agent as something they did not say.
    #
    # Kiro Crew's own downloaders write here directly and do not route through this
    # gate, so first-run fetches, re-downloads after a failed check and the embedding
    # model install all keep working; only the agent's file-edit tool is refused.
    f"{prefix}/models"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The decision log (``decisions/decisions-YYYYMMDD.jsonl``, written by
    # ``decisions.log``). Another instance of the input-to-an-authorization-decision
    # class, reached through the record rather than through the grant: the day-file
    # carries `kind="feedback"` rows, which are the OWNER's verdicts on what the skills
    # decision chose. An agent that can append there can put a verdict nobody gave into
    # the record, which is the one reading the feature exists to produce. The keystone next to it
    # (``decisions_consent.json``) is already read+write sensitive, and sealing the
    # grant while leaving the record writable would be half a control.
    #
    # WRITE-protected, not read+write sensitive: the rows are the machine's own
    # measurements and reading them is the point -- an owner or an agent asked to
    # explain a decision should be able to. There is no legitimate agent WRITE:
    # ``platform_log_append`` opens the file directly and does not route through this
    # gate, so the gateway keeps recording. The directory is separately mounted
    # read-only in the sandbox (``sandbox._CREW_READONLY_LEAVES``); that layer covers a
    # shell, and this one covers the file-edit tool, which is present on every host
    # whether or not the OS sandbox is.
    f"{prefix}/decisions"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The dashboard session-history store, fourth instance of the
    # input-to-an-authorization-decision class (rotation.yaml, the alias
    # ownership record, the OMC index). Each slot's persisted metadata carries
    # ``created_by`` — the creator attribution that chat_persistence restores
    # on gateway restart and that ``authorize_target`` then trusts as the
    # member ownership boundary. An agent that could rewrite a victim
    # transcript's metadata to name a member's caller key would, after one
    # restart, hand that member send/read/stop over the victim session. The
    # same file also carries the companion-artifact binding and the slot mode,
    # both re-validated on restore for exactly this reason.
    #
    # WRITE-protected, not read+write sensitive: transcripts are the user's
    # own conversations, and reading them (grep for an old error message, a
    # path, a decision) is routine and legitimate. There is NO legitimate
    # agent WRITE — the gateway persists turns through direct Python calls,
    # which do not route through this gate, so persistence keeps working. The
    # file-edit tool gate is the enforcement point; shell writes sit on the same
    # footing as config.json's.
    f"{prefix}/sessions"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The per-agent bookkeeping sidecar (agent_state.py, ``agent_model_state.json``
    # directly under the crew home). It is the same input-to-an-authorization-decision
    # class as rotation.yaml and the OMC index: besides model bookkeeping it records
    # FORK LINEAGE — ``forked_from`` / ``private_to`` mark a template as ONE crew's
    # private copy of a shared one (blueprint / copy-on-first-edit semantics). The
    # fork endpoint reads this lineage to decide whether a template is already the
    # crew's own copy; a prompt-injected agent that could write it would forge a
    # ``private_to`` entry naming a SHARED template, so the fork returns
    # ``already_private`` and the owner's next PATCH lands on the shared file instead
    # of forking a copy — silently mutating a template other crews depend on. Nothing
    # downstream neutralizes the forgery: the fork/PATCH path trusts the sidecar as
    # its own record..
    #
    # WRITE-protected, NOT read+write sensitive: it holds no secret and is READ
    # constantly (``list_agents`` enriches every row with fork info, model resolution
    # reads ``model_managed`` / ``cc_model``), so classifying it sensitive would break
    # those reads. Only the agent's own file-edit tool is refused; every internal
    # writer (agent_state._write via ``atomic_write``) opens the path directly and does
    # not route through this gate, so the dashboard fork/PATCH and the CLI model-state
    # updates keep working.
    f"{prefix}/agent_model_state.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The Connections tool-alias OWNERSHIP RECORD, third instance of the same class as the
    # two above and with the same read/write asymmetry. It holds no secret and the rebuild
    # reads it on every run, so classifying it sensitive would break the feature — but it is
    # an INPUT TO AN AUTHORIZATION DECISION, and by its own module's invariant 2 it is the
    # thing that AUTHORIZES DELETION: ``alias_record.load_claimed`` returns the pairs the
    # alias pass may strip from the agent spec, and nothing else grants that permission.
    #
    # An agent that can write this file can forge a ``committed`` record naming a
    # ``@slug/tool -> alias`` triple the user hand-wrote, together with the fingerprint of
    # the spec currently on disk (the spec is readable, so the fingerprint is computable).
    # The next rebuild then resolves the forgery as its own emission and deletes the user's
    # alias — laundering the edit through Kiro Crew's own trusted writer, which is what makes
    # it worse than editing the spec directly: the deletion is performed and persisted by the
    # legitimate owner of that file. The generation fingerprint cannot defend this, because a
    # forger reads the same spec it does.
    #
    # ``alias_record._write`` opens the path directly via ``atomic_write`` and does not route
    # through this gate, so both record writes still work; only the agent's own file-edit
    # tool is refused.
    f"{prefix}/connections-tool-aliases.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The operator-authored HARNESS DESCRIPTORS (``harnesses.json`` directly under
    # the crew home, beside config.json). It is the STRONGEST instance of the
    # write-protection class, on the same footing as ``app-sources`` below: the
    # protected file IS an execution grant rather than an input to a decision about
    # one. Each descriptor names an ``executable`` and an ``argv`` template the
    # gateway RESOLVES AND SPAWNS to serve a backend, so an agent that could write
    # this file could plant an attacker-chosen ``executable``/``argv`` and have Kiro
    # Crew's own trusted spawner run it — arbitrary code execution laundered through
    # the product's backend launcher, and re-armed on every listing. Nothing
    # downstream neutralizes it: unlike ``config.json``, whose inflated values the
    # loader clamps at load time, a descriptor's command is simply spawned; the
    # registry's validate pass costs a malformed row its listing but does nothing to
    # a WELL-FORMED forgery pointing at an attacker binary.
    #
    # WRITE-protected, deliberately NOT read+write sensitive: the file holds no
    # secret and is READ on every listing (the registry loads it to enumerate
    # backends, and Settings reads it to render the inventory), so classifying it
    # sensitive would break the feature. Only the agent's own file-edit tool is
    # refused; Kiro Crew's own writer opens the path directly via ``atomic_write``
    # and does not route through this gate, so operator edits keep working. The
    # shell side sits on the same footing as ``config.json``'s — a command-text
    # matcher matches no paths here (see ``is_sensitive_bash_command``); the OS
    # sandbox holds the shell fence, and this leaf is sealed READ-ONLY there
    # (``sandbox._CREW_READONLY_LEAVES`` plus the Linux absent-file pre-create in
    # ``_CREW_PRECREATE_READONLY_FILE_LEAVES``).
    f"{prefix}/harnesses.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The gateway-written ROUTING ATTESTATIONS for operator descriptors
    # (``backend-routing-attestations.json``, beside ``harnesses.json``). Same class
    # as the descriptor file, one step downstream: an attestation is what makes a
    # descriptor backend SELECTABLE (``acp.harness.routing_verification``), so an
    # agent that could write it would grant itself a harness the gateway never
    # verified. Only the gateway's own writer (the verify endpoint, via
    # ``atomic_write``) produces it; the agent's file-edit tool is refused, and the
    # OS sandbox seals it read-only the same way (``sandbox._CREW_READONLY_LEAVES``).
    f"{prefix}/backend-routing-attestations.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The gateway-written PER-CHAT BACKEND PINS (``chat-backend-pins.json``,
    # ``dashboard.backend_pins``): which harness process a chat's prompts reach
    # after a restart. The pin left the transcript metadata line precisely because
    # that file is within reach of the agent's own edit tool; an agent that could
    # write this document would retarget a chat to a provider its user never
    # picked. Only the gateway's slot routes and slot lifecycle write it.
    f"{prefix}/chat-backend-pins.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The settings-seed PROVENANCE RECORD (``acp.seed_provenance``), the alias
    # record's twin one seam over: it is what authorizes Kiro Crew to OVERWRITE and
    # then DELETE ``<work_dir>/.claude/settings.local.json``. The ACP client seeds
    # that file for a claude-agent-acp session and touches only the seed it owns;
    # ownership is this record plus the file on disk still hashing to the digest in
    # it. So an agent that can write this file can enter ``<path>: {size, sha256}``
    # for a settings file the USER hand-wrote — the file is readable, so both values
    # are computable — and the next session adopts it: the user's project settings
    # are overwritten with Crew's seed and unlinked on reset. As with the alias
    # record, the damage is done by Crew's own trusted writer, and the digest check
    # cannot defend it because the forger reads the same bytes it does.
    #
    # WRITE-protected, not read+write sensitive: the record holds no secret (it
    # names work dirs and digests), so the file-READ tools keep working and an
    # operator can still see why a seed was or was not adopted. There is no
    # legitimate agent WRITE at all — ``seed_provenance.record`` writes
    # the path directly through ``atomic_write``, which does not route through this
    # gate.
    f"{prefix}/settings_seeds.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The app-sources checkout root — the persistent tree every installed app
    # EXECUTES from (``apps.registry.app_source_dir``). This is a whole DIRECTORY
    # rather than a leaf, which the shared matcher already supports: it compares a
    # resolved path against the entry and its ``entry + os.sep`` prefix, so every
    # file under every checkout is covered without enumerating them.
    #
    # It is the strongest instance of the write-protection class, because the
    # protected file IS the executed code rather than an input to a decision about
    # it: an agent session with ordinary file-write tools could edit an installed
    # app's source, and that source then runs with the app's privileges on the
    # app's next launch. Nothing downstream neutralizes it — unlike ``config.json``,
    # whose inflated values the loader clamps at load time, a modified checkout is
    # simply run. Provenance does not catch it either: ``install_from_registry``
    # records ``_resolved_clone_commit`` (the tree's real ``HEAD``), and an agent
    # write dirties the worktree without moving ``HEAD``, so a modified tree still
    # reports the pinned SHA.
    #
    # Write-only, NOT ``_SENSITIVE_HOME_DIRS``, and the asymmetry is load-bearing:
    # app source carries no secret and is legitimately READ all the time — the
    # dashboard file viewer lists ``app-sources`` as a browsable root
    # (``apps.builtins.file_explorer.server``), knowledge indexing walks it, and
    # reading an installed app's code is how anyone debugs one. Classifying it
    # read+write sensitive would break those. Shell writes sit on the same footing
    # as ``config.json``'s, with the file-edit tool gate as the enforcement point.
    #
    # The gateway's own installer is unaffected: ``_clone_build_app`` clones,
    # builds and prunes through direct Python/subprocess calls, which are not
    # agent tool calls and never reach ``hooks.on_tool_call``.
    f"{prefix}/app-sources"
    for prefix in _CREW_HOME_PREFIXES
]

# ── kiro-cli agent-spec directory (~/.kiro/agents) ──
# The user-level directory kiro-cli reads its ``--agent <name>`` specs from
# (config.paths.kiro_agents_dir()). Each spec's ``mcpServers.<name>.command``
# is materialised by the MCP-gateway rewriter into a
# ``KIROCREW_MCP_TARGET_<SERVER>`` env value the gateway resolves and EXECS, and
# a stubbed server can be routed to a pooled backend that gatewayd spawns
# OUTSIDE the per-session sandbox, as the user. A prompt-injected agent that
# could WRITE a spec here — under any filename, so the whole DIRECTORY is fenced,
# not one leaf — would plant an attacker-chosen command that the gateway runs
# unsandboxed on the next start and re-arms on every restart. So the agent's
# file-edit tool must not be able to author or modify anything under it.
#
# WRITE-protection, NOT read+write sensitive: Kiro Crew and kiro-cli both
# legitimately READ specs (agent_discovery, session mtime scan, the dashboard MCP
# rows, kiro-cli's own ``--agent`` resolution), so this stays OFF
# ``_SENSITIVE_HOME_DIRS`` and reads are unaffected — only the write side is
# refused. Every INTERNAL writer (agent.rebuild_agent_config,
# apps.bridges._register_agents, the rewriter, the dashboard PUT handlers,
# connections/mint) opens these paths directly with ``os``/``Path`` and does NOT
# route through this gate, so managed-spec generation keeps working; only the
# agent's own file-edit tool hits it.
#
# Kept as a literal (mirroring ``.data-home-ready`` below) to avoid a
# config->security import cycle; a drift guard in the tests pins it to
# ``kiro_agents_dir()``'s tail. The default lives under the real home
# (``~/.kiro/agents``) and is anchored there like every other entry;
# ``KIRO_HOME`` (kiro-cli's own home override, which ``kiro_agents_dir()``
# honours) is re-anchored in ``_home_dir_targets_uncached`` so an instance that
# relocates its agents dir is covered the same way ``KIROCREW_HOME`` re-anchors
# the crew secrets.
_KIRO_AGENTS_DIR = ".kiro/agents"
_WRITE_PROTECTED_HOME_PATHS += [_KIRO_AGENTS_DIR]
_WRITE_PROTECTED_HOME_PATHS += [
    # Operator-authored panel templates (agent_panel.py). WRITE-protected rather
    # than read+write sensitive, and the asymmetry is the whole point: a crew's
    # webview is a human-authored TEMPLATE filled with crew-published DATA, and
    # that split is the containment story -- layout is reviewed, only data is
    # untrusted, so data can be escaped at one boundary. What must be refused is
    # an agent AUTHORING markup here: its auto-approved file tools could otherwise
    # drop a .html in and collapse the split, handing a hostile issue body a path
    # into a rendered document.
    #
    # The read must stay alive, which is why this entry is not on the floor above.
    # The file holds no secret: it is human-authored, versioned, reviewed content,
    # and it is the profile the write-only tier exists for -- routinely read, and
    # an input to a security decision. Fencing the READ would take the override
    # away from the operator who wrote it, since the same gate string reaches
    # ``fs_read``, the dashboard file viewer and knowledge indexing. Its sandbox
    # disposition already says the same thing from the other side: the launcher
    # seals the leaf READ-ONLY (``sandbox._CREW_READONLY_LEAVES``) rather than
    # masking it, so the two halves now agree.
    #
    # ``agent_panel.py`` loads the override directly and does not route through
    # this gate, so template rendering is unaffected; only the agent's own
    # file-edit tool is refused.
    f"{prefix}/panel-templates"
    for prefix in _CREW_HOME_PREFIXES
]

#: Longest command ``is_sensitive_bash_command`` will scan. Longer input is
#: REFUSED, not skipped and not scanned: both detectors the gate runs are linear
#: in the subject, so this bound is what turns "linear" into a hard wall-clock
#: ceiling for a gate that runs synchronously on the event loop under a 25 s
#: watchdog (``dashboard.loop_stall_exit_after_secs``). The same number bounds
#: each tool_input string in ``llm_helpers``; a legitimate command this long is a
#: heredoc writing a file, and the tool-input tier already refuses it, so the
#: tiers agree.
MAX_SCANNABLE_COMMAND_CHARS = 20 * 1024

#: Ceiling on a cron SCRIPT BODY the source-body detectors in ``mcp_cron`` will scan.
#: It is a different number from the command ceiling because the two subjects have
#: different legitimate sizes: 20 KiB of shell on one ``Bash`` call is a heredoc, while
#: 20 KiB of cron script is an ordinary script, and a size-keyed refusal there is
#: permanent (re-fired on every tick until someone edits the file). Still a hard
#: ceiling: the full-text detectors are linear, so this bounds their wall time on the
#: event loop. ``mcp_cron._MAX_SCRIPT_SCAN_BYTES`` aliases it so the reader admits
#: exactly what the detectors will scan.
MAX_SCANNABLE_SOURCE_BODY_CHARS = 256 * 1024


def _oversize_refusal(length: int, limit: int) -> str:
    """The pass-0 refusal, in ONE spelling.

    Both entry points refuse above their own ceiling and both must say so the same
    way, because an operator reading the reason is being told which number to compare
    their input against.
    """
    return (
        "Blocked: input is too large to security-scan "
        f"({length} chars > {limit} limit); refused rather than left unscanned"
    )


# ── Bounded symlink resolution for the sensitive-path gates ──
#
# ``os.path.realpath`` / ``Path.resolve`` ``lstat`` every component of the path
# they are handed.  The path gates hand them AGENT-SUPPLIED tokens -- including
# tokens that name nothing on this host at all, like the remote side of
# ``ssh host 'cd /home/user/ws && ...'`` -- and a component that lands on a
# stalled automount (macOS ``/home`` is an autofs map resolved through
# opendirectoryd; a dead NFS/SSHFS mount; a disconnected mapped drive) blocks in
# the kernel for as long as the mount does.  No exception is raised, so the
# ``except OSError`` around the call never fired: the call simply never
# returned.  Because :func:`is_sensitive_path` runs synchronously inside
# ``on_tool_call`` on the event loop, that was a loop
# wedge and the stall watchdog's dump-then-exit -- ten identical crash dumps on
# a corp macOS during a VPN transition, the loop parked in ``_joinrealpath`` for
# the full watchdog budget.  Widening the budget only moved the crash.
#
# So resolution runs on its own tiny pool and the caller waits a BOUNDED time.
# A timeout is NOT treated like the ``OSError`` fallback (lexical forms only):
# that would make the degraded state a lever -- stall one token under a wedged
# mount and, for the cooldown, a workspace symlink into a credential store
# would pass on its lexical spelling.  A path whose canonical form cannot be
# established is instead REFUSED (:class:`PathResolutionStalled`, fail-closed
# in every gate), the same posture the rest of this module takes when a proof
# is missing.  The cost is a false refusal of paths under a wedged mount for
# the cooldown window -- the ``ssh`` command above is refused for 30s during a
# VPN transition instead of killing the gateway -- and the refusal names why.
#
# A timeout also opens a short cooldown during which paths under the SAME
# prefix are refused without touching the filesystem: one tool call can
# carry many path tokens against the same wedged mount, each of which would
# otherwise pay the full timeout -- ten tokens at 2s would put the loop back
# past the watchdog.  The cooldown is scoped to the stalled prefix
# (:func:`_stall_prefix`), never process-wide, so a stall on ``/home/<user>``
# leaves ``/tmp`` and the workspace fully resolved.  It doubles on every
# repeat stall under the same prefix (up to the cap below) and a re-probe is
# only attempted while it leaves a worker free, because a timed-out worker is
# NOT reclaimed: a mount that stays dead would otherwise be handed a fresh
# worker every cooldown until every worker is pinned and every healthy path
# queues behind wedged futures -- the per-prefix isolation would hold only
# while free workers remained.
#
# The thread is NOT freed by the timeout (a started future cannot be cancelled);
# that is why this has its own pool -- see ``executors.path_resolve_executor``.
_PATH_RESOLVE_TIMEOUT_SECS = 2.0
# The ANCHOR REBUILD's own budget. One pool job there performs ~130 `realpath`
# calls to build ~200 targets, where a candidate resolution performs one or two,
# so a single budget sized for the candidate leaves the rebuild running ~130x
# closer to its ceiling -- measured: a cold rebuild is 130 `_realpath_or_none`
# calls, a warm one 4. Sizing this to the work actually done is what stops an
# ordinarily-slow rebuild from being mistaken for a wedged mount on a loaded host
# (4 xdist workers plus real-time antivirus on a 4-vCPU Windows runner is where it
# was first observed); raising `_PATH_RESOLVE_TIMEOUT_SECS` globally instead would
# relax the latency guarantee on the candidate path, which does not need it.
_PATH_RESOLVE_REBUILD_TIMEOUT_SECS = 8.0
# Fail-closed tightening: successful waits and stalls under DISTINCT prefixes all
# block the calling thread. Allow 12s total (one rebuild plus its maximum grace),
# leaving 13s of the 25s watchdog for heartbeat age and other tool-call work.
# Retain that spend until 25s after the LAST wait, not a fixed window boundary:
# otherwise two adjacent windows can spend twice the cap inside one watchdog gap.
# Background callers have their own allowance, never the event-loop thread's.
_PATH_RESOLVE_WAIT_CAP_SECS = 12.0
_PATH_RESOLVE_WAIT_WINDOW_SECS = 25.0
# A wait below this floor is resolver-pool round-trip overhead, not filesystem
# latency, so it is excluded from the cumulative spend above -- otherwise ordinary
# bulk work (a project-tree listing, a knowledge-indexing pass, a directory-wide
# path_contains_sensitive scan) accumulates thousands of sub-millisecond on-time
# waits and exhausts the allowance with zero mount evidence, trading the rare
# crash this bound removes for a reachable silent host-wide refusal instead.
# Measured on a 32-core host: 3000 calls against a healthy path cost 1.151s of
# accounted wait, 0.384ms/call -- 100ms is ~260x that overhead, so realistic pool
# jitter stays free, and 20x under the 2.0s default candidate budget, so it stays
# far below both a single missed-budget timeout and the slow-but-completing case
# this bound must still catch: a run of waits that each finish just under budget
# (13 at ~2s apiece is ~25s of loop block) all clear the floor and still count.
_PATH_RESOLVE_WAIT_FLOOR_SECS = 0.1
# How much longer a resolution that missed its budget is given to finish before the
# prefix is charged with a stall. A miss is not itself proof of a wedged mount -- a
# merely slow one completes -- and on any platform where the syscall probe below
# cannot discriminate, this is what separates the two, empirically rather than by
# syscall table. Paid at most once per prefix per cooldown, because the charge that
# follows a grace miss refuses later paths under the prefix without probing.
#
# Expressed as a FRACTION of the caller's budget, not a constant: the grace is "half
# again as long as this caller already agreed to wait", so a caller that deliberately
# chooses a tight budget keeps a tight worst case (the whole point of taking a budget
# per call) instead of inheriting a fixed multi-second tail. Capped so the generous
# rebuild budget cannot compound into the loop-stall watchdog this bound protects:
# 8s + 4s stays well inside 25s.
_PATH_RESOLVE_GRACE_FACTOR = 1.5
_PATH_RESOLVE_GRACE_MAX_SECS = 4.0
_PATH_RESOLVE_COOLDOWN_SECS = 30.0
_PATH_RESOLVE_COOLDOWN_MAX_SECS = 1800.0
# The load arm declines to charge the prefix, so it carries the event-loop bound the cooldown
# would otherwise supply: this many uncharged probes per prefix per window.
_PATH_RESOLVE_LOAD_WINDOW_SECS = 10.0
_PATH_RESOLVE_LOAD_MAX_PROBES = 3
# ``/proc/<tid>/syscall`` reports the syscall NUMBER, which is per-architecture. An unmapped
# architecture yields an empty set, which fails toward charging the prefix.
_FS_BLOCKING_SYSCALLS_BY_ARCH: dict[str, frozenset[int]] = {
    "x86_64": frozenset(
        {4, 5, 6, 89, 262, 267, 332}
    ),  # stat fstat lstat readlink newfstatat readlinkat statx
    "aarch64": frozenset({78, 79, 80, 291}),  # readlinkat newfstatat fstat statx
}
_FS_BLOCKING_SYSCALLS: frozenset[int] = _FS_BLOCKING_SYSCALLS_BY_ARCH.get(
    platform.machine(), frozenset()
)
# stall prefix -> (monotonic deadline until which paths under it are refused,
# consecutive stalls recorded under it -- drives the exponential backoff)
_path_resolve_degraded: dict[str, tuple[float, int]] = {}
_path_resolve_load_probes: dict[str, tuple[float, int]] = {}
# calling thread id -> (quiet-window end, accumulated seconds in result waits)
_path_resolve_thread_waits: dict[int, tuple[float, float]] = {}
# futures that timed out and still hold an mc-pathres worker; pruned as they finish
# (candidate spellings, root anchors and target rebuilds all land here)
_path_resolve_wedged: list[Future[Any]] = []
_path_resolve_lock = threading.Lock()
_path_resolve_clock: Callable[[], float] = time.monotonic  # tests advance this


def _resolved_spellings(expanded: str) -> set[str]:
    """Symlink-resolved spellings of *expanded*; runs on the ``mc-pathres`` pool."""
    out: set[str] = set()
    try:
        out.add(os.path.realpath(expanded))
    except (OSError, ValueError):
        pass
    try:
        # Guarded false-positive: this resolve() is INSIDE is_sensitive_path — the
        # sanitizer itself — building candidate forms to CHECK a path against the
        # sensitive denylist. It performs no read/write. CodeQL surfaces
        # py/path-injection here only because a new caller (artifact relocate)
        # reaches it with user input; the function's whole purpose is to vet that
        # input, so suppress the alert on the resolution step.
        out.add(str(Path(expanded).resolve()))  # lgtm[py/path-injection]
    except (OSError, ValueError, RuntimeError):
        pass
    return out


class PathResolutionStalled(RuntimeError):
    """Symlink resolution of an agent-supplied path did not complete in time.

    Raised by :func:`_resolved_forms_bounded` when the bounded ``realpath``
    times out, and for the cooldown that follows under the same path prefix.
    The sensitive-path gates treat it as FAIL-CLOSED: a path whose canonical
    form cannot be established is refused, never matched on its lexical
    spelling alone -- a lexical-only match would let a workspace symlink into a
    credential store pass while the mount it does not even live on is wedged.
    """

    def __init__(self, path: str, prefix: str) -> None:
        super().__init__(
            f"symlink resolution of {path!r} is unavailable (stalled mount under {prefix!r})"
        )
        self.path = path
        self.prefix = prefix


def _stall_prefix(expanded: str) -> str:
    """The path prefix a stall is charged to: the first two components, extended
    by one when the second names a container of home directories.

    A wedged mount stalls everything beneath its mount point, and mount points
    sit at depth one or two (``/home/<user>`` autofs, ``/Volumes/<share>``,
    ``/net/<host>``, ``C:\\Users\\<user>``), so two components is the narrowest key
    that still covers the whole stalled subtree.  Scoping the cooldown here is
    what keeps a stall on the REMOTE half of an ``ssh`` command from switching
    resolution off for the local workspace where a bypass symlink would live.

    **The DRIVE is split off first, and on Windows that is what makes the key two
    components rather than one.**  A POSIX absolute path starts with an empty
    component (``"/a/b"`` -> ``["", "a", "b"]``), which is why three are kept; a
    Windows path does not (``"C:\\Users\\bob"`` -> ``["C:", "Users", "bob"]``), so
    counting components without splitting the drive kept ``C:`` as one of the two
    and collapsed every user path to ``C:\\Users``.  That single key contains
    ``$HOME``, ``%TEMP%``, the workspace and the checkout, so one stall anywhere in
    the profile refused path resolution for essentially the whole host -- the exact
    opposite of the per-mount isolation this function exists to provide.

    **The key never ENDS on a container of homes**, a component spelled ``home``.
    A host whose homes sit one level deeper than ``/home`` reproduces the
    ``C:\\Users`` collapse on POSIX: two components of ``/local/home/<user>/ws/f``
    is ``/local/home``, which holds every user, every workspace and checkout, the
    data home and the credential stores, so one stall anywhere under it refused
    path resolution for the whole host (kirodotdev/KiroCrew#12386).  When the
    second component is such a container the key takes one more, so the boundary
    is the home directory itself -- ``/local/home/<user>`` -- exactly the key a
    ``/home/<user>`` layout already gets.  Every other key is unchanged, and the
    container alone (``/local/home``) stays its own key.

    The rule is lexical, like the rest of this function: it must not touch the
    filesystem, because it is consulted BEFORE deciding whether to probe at all
    and it names the prefix charged when the ``$HOME`` anchor itself stalls.  A
    mount-derived key (``os.path.ismount`` walked upward) would both ``lstat``
    the very mount that may be wedged and, on a single-filesystem host, reach
    ``/`` -- a process-wide key, worse than the collapse.  The price of a lexical
    key stands: two spellings of one tree (``/home/<user>`` and its
    ``/local/home/<user>`` target) remain two keys, each paying its own timeout.
    And when the container is itself ONE wedged mount (an NFS ``/export/home``),
    each user's tree pays its own timeout and pins its own worker -- the cost a
    ``/home/<user>`` autofs layout already carries per user, bounded the same
    way, by the pool size -- in exchange for the isolation this key exists for.

    A UNC share root is returned whole: ``\\\\server\\share`` IS the mount point,
    and ``splitdrive`` already reports it as the drive, so no component of the
    remainder belongs in the key.
    """
    normalized = os.path.normpath(expanded)
    drive, rest = os.path.splitdrive(normalized)
    if drive[:1] in ("\\", "/") and drive[1:2] in ("\\", "/"):
        return drive
    parts = rest.split(os.sep)
    keep = 3 if parts and parts[0] == "" else 2  # leading "" for an absolute path
    # A component spelled ``home`` is a CONTAINER of home directories, never a home:
    # ``/home/<user>`` is the common layout, and the layouts that put the homes one
    # level deeper all keep the word -- ``/local/home/<user>`` on a cloud desktop
    # (where ``/home/<user>`` is a symlink into it), ``/usr/home`` on FreeBSD,
    # ``/var/home`` on Fedora Silverblue, ``/export/home`` on Solaris and NFS
    # servers.  ``Users`` needs no entry: it only ever sits at depth one
    # (``/Users/<user>``, ``C:\Users\<user>``), where the key already ends on the
    # user.  Matched exactly: the container is spelled by the OS or the
    # administrator, not by the agent.
    if len(parts) > keep and parts[keep - 1] == "home":
        keep += 1  # the container holds every user: key on the home beneath it
    return (drive + os.sep.join(parts[:keep])) or normalized


def _wedged_workers() -> int:
    """How many ``mc-pathres`` workers are still pinned by a timed-out resolution.

    A future that timed out is not cancelled -- its thread stays in the kernel
    until the mount answers -- so it is kept here and forgotten once it finally
    completes.  The count gates re-probes: a mount that stays dead (hard NFS,
    not the transient VPN case) must not be handed a fresh worker every cooldown
    until none is left and every healthy path queues behind wedged futures.
    """
    with _path_resolve_lock:
        _path_resolve_wedged[:] = [f for f in _path_resolve_wedged if not f.done()]
        return len(_path_resolve_wedged)


def _worker_blocked_in_filesystem(tid: int | None) -> bool:
    """True when thread ``tid`` is blocked inside a syscall a path resolution can block in.

    Separates the two causes of a started-but-unfinished resolution, which the budget alone
    cannot distinguish: a worker stuck on a wedged mount versus one that started and was then
    starved of the CPU. Both consume almost no CPU, so a CPU-time comparison cannot tell them
    apart, and neither can the ``/proc`` state field: measured on a 48-core host, a thread doing
    ordinary ``lstat`` work and a thread doing nothing but burn CPU BOTH alternate between ``R``
    and ``S`` from one sample to the next, because a Python thread spends most of its wall time
    waiting on the GIL rather than inside a syscall.

    What does separate them is WHICH syscall the thread is in. A thread genuinely blocked in a
    kernel wait reports that syscall on every sample; one merely contending reports ``futex`` or
    ``running``. And these syscalls complete in microseconds on a healthy filesystem, so
    sampling one at all is itself evidence that it is not completing.

    UNVERIFIED, and deliberately not claimed: that a thread stuck in one of these syscalls on a
    real wedged NFS, FUSE or CIFS mount reports it stably. No wedged mount could be produced
    where this was measured: an unprivileged ``fusermount3`` mount returns EPERM and
    ``unshare(CLONE_NEWUSER)`` returns EPERM, so neither FUSE nor a user-namespace NFS mount is
    available there.

    What IS confirmed is the sampling this rests on, including for the state class a wedged FUSE
    or CIFS mount actually waits in. An ``openat`` on a FIFO with no writer blocks
    INTERRUPTIBLY while operating on a real filesystem path, and measured on a 48-core x86_64
    host it reported state ``S`` with syscall 257 on 15 of 15 samples and no other value -- so
    an in-``S`` filesystem wait samples exactly as stably as the uninterruptible waits (a pipe
    ``read`` and a ``clock_nanosleep``, each 12 of 12). See
    ``test_an_in_s_filesystem_wait_is_sampled_stably_and_reads_as_blocked``.

    The table covers ``readlink`` as well as the stat family because CPython's
    ``posixpath.realpath`` calls ``os.lstat`` AND ``os.readlink`` per component: a mount that
    answers the lstat from cache and hangs the readlink would otherwise read as not blocked,
    take the load arm, and pay an uncharged full-budget probe per token rather than opening one
    cooldown.

    Fails toward the EXISTING behaviour -- an unreadable ``/proc``, a thread that has already
    exited, an architecture whose syscall numbers are not mapped -- by returning True, so the
    caller still charges the prefix rather than silently withholding an escalation the gate
    would otherwise make.
    """
    if tid is None or not _FS_BLOCKING_SYSCALLS:
        return True
    try:
        with open(f"/proc/self/task/{tid}/syscall", "rb") as fh:
            head = fh.read().split()
    except OSError:
        return True
    if not head:
        return True
    sampled = head[0]
    if sampled == b"running":
        blocked = False
    else:
        try:
            blocked = int(sampled) in _FS_BLOCKING_SYSCALLS
        except ValueError:
            blocked = True
    logger.debug(
        "resolver worker tid=%s syscall=%s blocked_in_filesystem=%s",
        tid,
        sampled.decode("ascii", "replace"),
        blocked,
    )
    return blocked


def _load_arm_budget_spent(prefix: str) -> bool:
    """Record a load-arm probe under *prefix*; True once the window's allowance is gone.

    The per-prefix cooldown has a second job besides remembering a dead mount: it BOUNDS how
    much event-loop time one call can spend probing. A single tool call can carry many path
    tokens, and ten tokens each paying the full budget puts the event loop back past the
    watchdog this whole bound exists to protect. Declining to charge the prefix removes that
    bound, so the load arm has to carry it: the first ``_PATH_RESOLVE_LOAD_MAX_PROBES`` probes
    in a window are free, and the next one charges the prefix normally.

    The count is cleared ONLY by the window expiring, never by a successful resolution. It
    measures event-loop time already spent, which a later success cannot refund: clearing it on
    success let an alternating success / CPU-starved-timeout run under one prefix pay the full
    budget on every timeout while never crossing the allowance.
    """
    now = _path_resolve_clock()
    with _path_resolve_lock:
        if len(_path_resolve_load_probes) > 64:
            _path_resolve_load_probes.clear()
        window_end, probes = _path_resolve_load_probes.get(prefix, (0.0, 0))
        if now >= window_end:
            window_end, probes = now + _PATH_RESOLVE_LOAD_WINDOW_SECS, 0
        probes += 1
        _path_resolve_load_probes[prefix] = (window_end, probes)
        return probes > _PATH_RESOLVE_LOAD_MAX_PROBES


def _mark_stalled(prefix: str, budget: float) -> None:
    """Record an OBSERVED stall under *prefix*: back off exponentially on repeats.

    Only a resolution that actually RAN and timed out is recorded.  A refusal
    issued because every worker was already pinned (nothing is submitted), or
    because a submitted resolution never left the queue (its future cancelled
    on timeout), says nothing about the filesystem and must not charge the
    refused prefix -- often the local workspace -- a backoff it never earned,
    or a transient dual-mount outage would keep refusing healthy paths for the
    accrued window after the mounts recover.  The log line deliberately omits
    the path: the token is agent-supplied and is what the gates exist to keep
    out of clear-text logs.
    """
    now = _path_resolve_clock()
    with _path_resolve_lock:
        if len(_path_resolve_degraded) > 64:
            _path_resolve_degraded.clear()
        _, stalls = _path_resolve_degraded.get(prefix, (0.0, 0))
        stalls += 1
        cooldown = min(
            _PATH_RESOLVE_COOLDOWN_SECS * (2 ** (stalls - 1)),
            _PATH_RESOLVE_COOLDOWN_MAX_SECS,
        )
        _path_resolve_degraded[prefix] = (now + cooldown, stalls)
    logger.warning(
        "sensitive-path symlink resolution did not complete in %.1fs (stalled "
        "mount?); refusing paths under the stalled prefix for the next %.0fs "
        "(stall #%d, %d resolver worker(s) pinned)",
        budget,
        cooldown,
        stalls,
        len(_path_resolve_wedged),
    )


_UNC_PREFIX_RE = re.compile(r"^[\\/]{2}[^\\/]")
_ON_WINDOWS = os.name == "nt"


def _is_unc_path(expanded: str) -> bool:
    """``\\\\server\\share\\...`` in either separator spelling.

    On Windows ``os.path.realpath`` on a UNC path opens it
    (``GetFinalPathNameByHandle``), which is a network round-trip to the named
    host -- a dead or slow host stalls the caller for the SMB timeout, and a
    UNC token in an agent's command is the ordinary way to name a share, not a
    symlink-bypass vector: the fence's targets are local drive spellings that a
    UNC realpath never produces (``\\\\?\\UNC\\...``).  So a UNC token is matched
    lexically and never probed, the same stance the mapped-drive fence below
    takes for a foreign drive letter.
    """
    return bool(_UNC_PREFIX_RE.match(expanded))


_ResolvedT = TypeVar("_ResolvedT")


def _run_resolution_bounded(
    expanded: str, worker: Callable[[str], _ResolvedT], *, budget: float | None = None
) -> _ResolvedT | None:
    """Run *worker(expanded)* on the ``mc-pathres`` pool within the resolve budget.

    The shared core under :func:`_resolved_forms_bounded` (the agent-supplied
    CANDIDATE), :func:`_resolved_root_key` and :func:`_rebuild_targets_bounded`
    (the TARGET anchors: ``$HOME``, the
    override roots and the keystone leaves).  Both kinds of resolution stat the
    same filesystem from the event loop, so they share one pool, one budget and
    one per-prefix cooldown: a stall observed while anchoring ``$HOME`` refuses
    candidate resolution under that prefix for the same window, and a stall on
    a candidate keeps the anchors from re-probing the same wedged mount every
    time the target cache expires.

    Returns the worker's value, or ``None`` when resolution FAILED -- the pool
    refused work at interpreter exit, or faulted.  A resolution that does not
    COMPLETE is different and raises
    :class:`PathResolutionStalled` instead, both on the timing-out call and,
    without touching the filesystem, for every later call under the same
    :func:`_stall_prefix` until the cooldown lapses.  Repeated stalls under one
    prefix double the cooldown up to ``_PATH_RESOLVE_COOLDOWN_MAX_SECS``, and a
    prefix with a stall history is only re-probed while that leaves at least one
    worker free for everything else -- so a permanently dead mount is probed
    rarely and can never pin the whole pool.  Never blocks the caller for longer
    than *budget* plus its grace -- the bounded second wait a missed budget earns
    before the prefix is charged, itself a capped fraction of *budget*. Both waits
    also consume the calling thread's cumulative allowance -- excluding a wait
    below the floor, resolver-pool round-trip overhead rather than filesystem
    latency; exhaustion refuses without submitting work or charging a prefix until
    the quiet window expires.
    Charging the prefix on a timeout ALSO requires that both the budget and the
    grace were granted in full, uncapped by the allowance: a wait clamped short by
    the allowance says nothing about the mount, so it refuses this call alone,
    the same conclusion the saturated-pool, never-ran and load arms reach.

    A stall is charged only to a resolution that RAN.  A future that times out
    still QUEUED (the pool saturated by concurrent callers, e.g. simultaneous
    cron fires) is cancelled and refuses this call alone: queue wait is
    evidence about load, not about the mount, so it opens no cooldown and pins
    no worker in :func:`_wedged_workers`.  A future claimed by a freeing worker
    in the very instant the deadline fires is abandoned by handshake -- the
    worker returns without entering the resolution -- so the never-ran
    classification is binding, not a race.

    The UNC shortcut is NOT here: skipping a ``\\\\server\\share`` token is a
    stance about agent-supplied CANDIDATES (:func:`_resolved_forms_bounded`),
    whose fence targets a UNC realpath never produces.  The anchors are the
    fence itself, and a UNC home with a junction inside ``KIROCREW_HOME`` must
    still be canonicalised or a canonical-spelling request would miss the
    governance file (found in review); the bound makes that probe safe.

    *budget* sizes the wait to the work the caller submits: the anchor REBUILD is
    one job performing ~130 ``realpath`` calls and passes
    ``_PATH_RESOLVE_REBUILD_TIMEOUT_SECS``, while a candidate resolution keeps the
    default. One budget for both put the rebuild ~130x closer to its ceiling than
    the path whose latency the default exists to guarantee.
    """
    if budget is None:
        budget = _PATH_RESOLVE_TIMEOUT_SECS
    requested_budget = budget
    now = _path_resolve_clock()
    prefix = _stall_prefix(expanded)
    with _path_resolve_lock:
        history = _path_resolve_degraded.get(prefix)
    if history is not None and now < history[0]:
        raise PathResolutionStalled(expanded, prefix)
    wedged = _wedged_workers()
    if wedged >= _MAX_PATH_RESOLVE_WORKERS or (
        history is not None and wedged >= _MAX_PATH_RESOLVE_WORKERS - 1
    ):
        # Every worker is pinned, or this re-probe of a known-stalled prefix
        # would pin the last free one.  Queueing behind a wedged future can only
        # time out, so refuse now.  Nothing was submitted, so nothing is charged
        # to the prefix: the next call re-evaluates the gate for free.
        logger.debug(
            "sensitive-path symlink resolution refused without probing: %d of %d "
            "resolver worker(s) pinned by earlier stalls",
            wedged,
            _MAX_PATH_RESOLVE_WORKERS,
        )
        raise PathResolutionStalled(expanded, prefix)
    caller_tid = threading.get_ident()
    with _path_resolve_lock:
        if len(_path_resolve_thread_waits) > 64:
            # Clearing live entries would let thread churn refund the loop's spend.
            expired = [tid for tid, (end, _) in _path_resolve_thread_waits.items() if now >= end]
            for expired_tid in expired:
                del _path_resolve_thread_waits[expired_tid]
        window_end, seconds_spent = _path_resolve_thread_waits.get(caller_tid, (0.0, 0.0))
        if now >= window_end:
            seconds_spent = 0.0
    remaining = _PATH_RESOLVE_WAIT_CAP_SECS - seconds_spent
    if remaining <= 0:
        # No work ran, so this refusal says nothing about the prefix's mount.
        logger.debug(
            "sensitive-path symlink resolution refused without probing: calling thread's "
            "cumulative wait allowance exhausted; prefix not charged"
        )
        raise PathResolutionStalled(expanded, prefix)
    granted_budget = min(budget, remaining)
    try:
        started = threading.Event()
        abandoned = threading.Event()
        handoff = threading.Lock()
        worker_tid: list[int] = []

        @functools.wraps(worker)
        def _tracked(arg: str) -> _ResolvedT | None:
            worker_tid.append(threading.get_native_id())
            with handoff:
                if abandoned.is_set():
                    # The caller classified this future as never-run at its
                    # deadline: return without touching the filesystem, so a
                    # late claim can neither probe a wedged mount nor pin a
                    # worker _wedged_workers() is not tracking.
                    return None
                started.set()
            return worker(arg)

        future = path_resolve_executor().submit(_tracked, expanded)
    except RuntimeError:
        # Pool already shut down (interpreter exit).  Lexical forms only.
        return None

    def _wait_for_result(timeout: float) -> _ResolvedT | None:
        nonlocal seconds_spent
        wait_start = _path_resolve_clock()
        try:
            return future.result(timeout=timeout)
        finally:
            wait_end = _path_resolve_clock()
            elapsed = max(0.0, wait_end - wait_start)
            if elapsed >= _PATH_RESOLVE_WAIT_FLOOR_SECS:
                seconds_spent += elapsed
                with _path_resolve_lock:
                    _path_resolve_thread_waits[caller_tid] = (
                        wait_end + _PATH_RESOLVE_WAIT_WINDOW_SECS,
                        seconds_spent,
                    )

    try:
        value = _wait_for_result(granted_budget)
    except FutureTimeoutError:
        if future.cancel():
            # Never claimed by a worker: the pool was saturated and the
            # resolution never started -- evidence about load, not the mount.
            logger.debug(
                "sensitive-path symlink resolution refused: the resolver pool was "
                "saturated and the resolution never started; prefix not charged"
            )
            raise PathResolutionStalled(expanded, prefix) from None
        with handoff:
            ran = started.is_set()
            if not ran:
                abandoned.set()
        if not ran:
            # Claimed by a freeing worker in the instant the deadline fired,
            # before entering the resolution.  The handshake makes the
            # classification binding: the worker sees ``abandoned`` and returns
            # without probing, so it pins nothing and there is nothing to
            # charge -- the same conclusion as the queued arm above.
            logger.debug(
                "sensitive-path symlink resolution refused: the resolver pool was "
                "saturated and the resolution never started; prefix not charged"
            )
            raise PathResolutionStalled(expanded, prefix) from None
        with _path_resolve_lock:
            _path_resolve_wedged.append(future)
        tid = worker_tid[0] if worker_tid else None
        if not _worker_blocked_in_filesystem(tid) and not _load_arm_budget_spent(prefix):
            logger.debug(
                "sensitive-path resolution timed out under load; prefix not charged (tid=%s)",
                tid,
            )
            # The worker RAN but never got the CPU: the same conclusion as the queued
            # arm above, reached one step later.  The future stays tracked as wedged
            # (it does hold a worker until it finishes), but the prefix is NOT charged,
            # so ordinary contention refuses THIS resolution instead of opening a
            # cooldown across every path under the prefix.
            raise PathResolutionStalled(expanded, prefix) from None
        # A missed budget is not yet proof of a wedged mount, and charging the prefix
        # is the expensive conclusion: it refuses EVERY path under that prefix for the
        # cooldown, so one transient miss becomes a cascade of refusals across
        # unrelated paths. Give the resolution a bounded GRACE to finish first. This is
        # the only discriminator available wherever the syscall probe above cannot
        # answer -- an architecture absent from `_FS_BLOCKING_SYSCALLS_BY_ARCH`, which
        # is every Windows host (`platform.machine()` is "AMD64") and Apple silicon --
        # because there it returns True for a merely slow resolution as readily as for
        # a dead mount, and the prefix was charged either way.
        #
        # Costs nothing on a genuinely wedged mount beyond delaying the cooldown by
        # the grace, and is paid at most ONCE per prefix per cooldown: the charge
        # below refuses later paths under the prefix without probing at all. The
        # future stays tracked as wedged while this waits, so a second token cannot
        # pin the last worker meanwhile, and it self-prunes from that list if it does
        # complete (`_wedged_workers` drops finished futures).
        entitled_grace = min(
            requested_budget * _PATH_RESOLVE_GRACE_FACTOR, _PATH_RESOLVE_GRACE_MAX_SECS
        )
        granted_grace = min(entitled_grace, _PATH_RESOLVE_WAIT_CAP_SECS - seconds_spent)
        # A one-element list, not an Optional: the sentinel has to distinguish "the
        # future completed" from "it completed as None", and the worker's own return
        # is Optional since the never-ran handshake above makes it return None.  That
        # arm raises before reaching here, so a None here can only come from the
        # worker itself and is passed through exactly like the on-time path does.
        late: list[_ResolvedT | None] = []
        try:
            if granted_grace > 0:
                late.append(_wait_for_result(granted_grace))
        except FutureTimeoutError:
            pass
        except Exception:
            logger.debug("sensitive-path symlink resolution failed", exc_info=True)
            return None
        if late:
            logger.debug(
                "sensitive-path resolution completed within %.1fs past its %.1fs "
                "budget, so the prefix is NOT charged (tid=%s)",
                granted_grace,
                requested_budget,
                tid,
            )
            if history is not None:
                with _path_resolve_lock:
                    _path_resolve_degraded.pop(prefix, None)
            return late[0]
        if granted_budget < requested_budget or granted_grace < entitled_grace:
            # The wait ended early because the calling thread's cumulative allowance
            # ran out, not because the resolution itself proved anything about the
            # mount -- the same conclusion the saturated-pool, never-ran and load
            # arms above reach by a different route. Charging here would let the
            # allowance clamp reopen exactly the blast radius this bound removes:
            # on a host where the syscall probe cannot discriminate (every Windows
            # host, and Apple silicon), the grace is the ONLY signal, and a
            # truncated grace answers nothing either way. Refuse this call alone.
            logger.debug(
                "sensitive-path resolution timed out with its budget or grace clamped "
                "by the calling thread's cumulative wait allowance; prefix not "
                "charged (tid=%s)",
                tid,
            )
            raise PathResolutionStalled(expanded, prefix) from None
        logger.debug(
            "sensitive-path resolution timed out blocked in the filesystem (tid=%s)",
            tid,
        )
        _mark_stalled(prefix, requested_budget)
        raise PathResolutionStalled(expanded, prefix) from None
    except Exception:
        # The worker's own exceptions are already swallowed inside the worker;
        # anything else here is a pool fault, and the gate's contract is to keep
        # the lexical forms rather than fail the tool call.
        logger.debug("sensitive-path symlink resolution failed", exc_info=True)
        return None
    if history is not None:
        # The mount answered again: forget the stall history so the next stall
        # starts from the base cooldown rather than an inherited backoff.
        with _path_resolve_lock:
            _path_resolve_degraded.pop(prefix, None)
    return value


def _resolved_forms_bounded(expanded: str) -> set[str]:
    """Return the symlink-resolved spellings of *expanded*, or an empty set.

    Empty means resolution FAILED (see :func:`_run_resolution_bounded`) or was
    deliberately not attempted -- a UNC path on Windows, see
    :func:`_is_unc_path`: the caller keeps the lexical forms, exactly as before
    the bound existed.  A resolution that does not COMPLETE raises
    :class:`PathResolutionStalled` through here, and every gate turns that into
    a refusal.  Tests swap :func:`_resolved_spellings` at module level for a
    blocking stub and advance ``_path_resolve_clock``.
    """
    if _ON_WINDOWS and _is_unc_path(expanded):
        return set()
    forms = _run_resolution_bounded(expanded, _resolved_spellings)
    return set() if forms is None else forms


def _realpath_or_none(path: str) -> str | None:
    """``os.path.realpath`` for a target anchor; runs on the ``mc-pathres`` pool."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _candidate_forms(
    path_str: str, base_dir: str | None = None, *, pre_resolved: bool = False
) -> set[str]:
    """Expand *path_str* into every candidate form the sensitive-path gates match.

    Symlink-resolved forms defeat a link bypass; the lexical forms are the
    fail-safe fallback when resolution cannot complete (over-matching a
    sensitive-looking path is the safe direction). ``base_dir`` anchors a
    relative input against the caller's known working directory. Shared by
    :func:`_path_in_home_dirs` (is the path INSIDE a protected location?) and
    :func:`path_contains_sensitive` (does the path CONTAIN one?) so the
    symlink/anchoring hardening cannot drift between the two directions.

    *pre_resolved* says the caller ALREADY holds the canonical spelling -- the
    output of ``os.path.realpath`` computed on its own thread -- so no
    resolution is submitted to the ``mc-pathres`` pool: the candidates are the
    input and its ``normpath``, which is exactly what :func:`_resolved_spellings`
    returns for a path that has no link left to follow. Reserved for
    :func:`is_sensitive_resolved_path`; see there for why a caller may claim it.
    """
    # Expand ~ and $HOME
    expanded = os.path.expanduser(os.path.expandvars(path_str))

    # Anchor a relative input against the supplied workspace dir so it resolves
    # to the real file rather than the gateway's CWD.  Absolutize base_dir
    # itself first — if a caller passes a relative base_dir, os.path.join would
    # re-anchor against the process CWD (the very thing the parameter exists to
    # avoid), giving zero protection when CWD is unrelated to the workspace.
    if base_dir and not os.path.isabs(expanded):
        expanded = os.path.join(os.path.abspath(base_dir), expanded)

    # Build the candidate forms.  Symlink-resolved forms defeat a link bypass;
    # the lexical forms are the fail-safe fallback when resolution FAILS
    # (over-matching a sensitive-looking path is the safe direction).
    # Resolution is BOUNDED -- see _resolved_forms_bounded: an unbounded lstat on
    # a stalled automount would wedge the event loop from inside on_tool_call.
    # A resolution that does not COMPLETE raises PathResolutionStalled through
    # here, and every gate turns that into a refusal: no lexical-only matching
    # of a path whose canonical form is unknown.
    candidates: set[str] = set() if pre_resolved else _resolved_forms_bounded(expanded)
    candidates.add(os.path.normpath(expanded))
    candidates.add(expanded)
    return candidates


class _BuiltTargets(set[str]):
    """The target set, plus whether building it traversed a symlink.

    A plain ``set`` wherever it is consumed -- membership, iteration, equality
    and the casefold contract are all unchanged -- carrying one extra fact that
    only the CACHE needs and no matcher does: did any path this build actually
    RESOLVED come back spelled differently from the path it was asked about.

    WHICH paths those are is the entire scope of the flag, so they are named here
    rather than left to be read as "every target". The build resolves five classes
    of path and no others: ``$HOME``, ``KIROCREW_OS_HOME``, each entry of the
    CALLER'S ``home_dirs`` list that carries a crew prefix, re-anchored under
    ``KIROCREW_HOME``, the agents dir under ``KIRO_HOME``, and each harness
    credential leaf re-anchored under its own home override. Every
    other member of the set comes from ``_anchor`` or
    ``_anchor_both_separators``, neither of which touches the filesystem, so the
    bulk of the set cannot report a traversal at all.

    An ordinary symlinked dotfile under ``$HOME`` can set this flag, and a
    dotfile-managed home is therefore a case to look for. The trigger is the union
    this build already resolves: a crew-prefixed entry of the caller's
    ``home_dirs`` under ``KIROCREW_HOME``, the agents dir under ``KIRO_HOME``, or a
    DECLARED credential leaf under whichever harness variable relocates it. So a
    symlinked ``sessions`` or ``models`` leaf under a ``KIROCREW_HOME`` that sits
    below ``$HOME`` pins the write tier, and a symlinked ``goose`` directory under
    whatever ``XDG_CONFIG_HOME`` resolves to pins the tier handed that leaf. Read
    that second one literally: the leaf resolved is
    ``$XDG_CONFIG_HOME/goose/secrets.yaml``, so it is the exported root that has to
    contain the symlink. A host whose ``XDG_CONFIG_HOME`` points somewhere other
    than ``~/.config`` can symlink ``~/.config/goose`` all it likes and no build
    will resolve it. What CANNOT set it is a dotfile in none of those classes,
    because the bulk targets are never resolved. The roots themselves cannot
    either: ``_resolve_root_anchors`` canonicalises every root first, so the
    symlink has to sit BELOW the resolved root -- which is why relocating the
    exported config root alone does not trip it while a symlinked ``goose``
    directory under it does.

    The LEAF-BEARING override roots are ``KIROCREW_HOME``, ``KIRO_HOME``, and every
    variable in ``host_auth.home_override_env_vars()``. That table is the
    enumeration; naming the variables or their count here would be a second place
    to forget the next harness, which is the documentation defect this docstring
    exists to remove. ``_OVERRIDE_ROOT_ENVS`` carries one more root,
    ``KIROCREW_OS_HOME``, which is resolved as a root but bears no leaf class of
    its own -- its entries are joined lexically by ``_anchor_both_separators``.

    The crew-prefix class is the caller's list, not a fixed set of leaves. The
    crew-prefix arm loops over the ``home_dirs`` it was handed and resolves every
    entry carrying a prefix, and the three callers hand it three different lists:
    the read gate passes ``_SENSITIVE_HOME_DIRS``, ``is_sensitive_write_path``
    passes ``_SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS``, and
    ``_is_keystone_publish_artifact`` passes ``_KEYSTONE_ARTIFACT_PARENTS``. So no
    one class of leaf describes the resolved population: on the write tier a
    symlinked write-protected crew leaf that holds no secret sets the flag too,
    and the three builds can disagree about it on one host.

    That fact is what bounds the deeper-leaf staleness the target cache always
    carried. A resolved path that comes back different came through a symlink at
    or below a resolved root, and THAT is the entry a repoint can move out from
    under a cached set. When nothing resolved differently there is no
    resolution-derived entry to go stale, so the long adaptive expiry is safe;
    when one did, the expiry is pinned to the floor. See
    :func:`_home_targets_ttl`.

    The roots cannot trip it. Every root reaching the builder is already
    canonical (:func:`_resolve_root_anchors` resolved them), so re-resolving one
    returns the same string -- which matters on a host where ``$HOME`` is itself
    a symlink, because flagging that would pin the floor on every cloud desktop
    and the adaptive expiry would never apply anywhere.

    Carried as an ATTRIBUTE rather than a second return value because
    ``_home_dir_targets_uncached`` has several callers and three test doubles. A
    widened signature would break each of them, whereas a double that returns a
    plain ``set`` simply lacks the attribute and reads as the class default
    below -- ``True``, the fail-safe answer, which selects the floor.
    """

    #: Fail-safe: an unknown build is treated as having traversed a symlink, so a
    #: caller that cannot prove otherwise gets the short expiry.
    resolution_differed: bool = True


def _home_dir_targets_uncached(
    home_dirs: list[str],
    roots: _ResolvedRoots | None = None,
) -> set[str]:
    """Anchor the ``$HOME``-relative *home_dirs* entries into absolute, casefolded
    on-disk targets.

    Every per-anchor resolved form comes from :func:`_realpath_or_none`, looked
    up at call time so a test can stand in a recording or wedged resolver at
    module level.  It touches the filesystem, so in production this function
    runs on the ``mc-pathres`` pool via :func:`_home_dir_targets` (see there
    for why); only direct callers and tests run it inline.

    *roots* optionally supplies the already-resolved :class:`_ResolvedRoots`
    already resolved by the caller. The TTL cache in :func:`_home_dir_targets` MUST pass
    it: resolving the roots here as well would read the filesystem a second
    time, and a root symlink repointed between the two reads would file this
    set under a key naming the OTHER root — caching one root's targets against
    another root's key, which fails OPEN. ``None`` (direct callers and tests)
    resolves them here as before.

    Anchors against BOTH the logical home and its realpath.  On macOS the
    per-user temp/home prefix can itself be reached via OS symlinks (``/var`` →
    ``/private/var``); folding both roots in means a resolved candidate under
    either spelling is still matched.

    ``home_dirs`` entries are authored with POSIX "/" separators, and some are
    multi-segment now (e.g. ".kiro/crew/security_policy.json"). Split on "/"
    and re-join with ``os.path.join`` so the target uses the running OS's
    separator — otherwise on Windows the target keeps a literal "/" in the
    leaf while the candidate forms (realpath/normpath) are all-backslash, they
    never compare equal, and the keystone would silently stop gating its own
    secrets. On POSIX a single-segment entry splits to a 1-element list, so
    this is a no-op there.
    """
    # Both supported Crew home prefixes map to the same override leaves.
    # Resolve each identical spelling once within this build; never carry these
    # answers across builds or cache keys, so root and leaf freshness is unchanged.
    resolved_paths: dict[str, str | None] = {}

    def resolve_target(path: str) -> str | None:
        if path not in resolved_paths:
            resolved_paths[path] = _realpath_or_none(path)
        return resolved_paths[path]

    resolved = roots if roots is not None else _resolved_root_key()
    home = resolved.home
    crew_home = resolved.crew_home
    kiro_home_override = resolved.kiro_home
    logical_home = resolved.logical_home
    os_home = resolved.os_home

    def _anchor(root: str, d: str) -> str:
        return os.path.join(root, *_leaf_segments(d)).casefold()

    def _anchor_both_separators(root: str, d: str) -> set[str]:
        """*d* under *root*, spelled with BOTH separators.

        ``_anchor`` joins with the RUNNING OS's separator, which is right for a
        candidate that reached the matcher as a native path. It is not enough for
        a pod root: this anchors a root that arrives from an ENV VARIABLE rather
        than from ``Path.home()``, so the operator's own spelling reaches the set
        and a Windows pod home would otherwise be all-backslash while a candidate
        normalised to forward slashes never compared equal -- the gate silently
        stops covering its own targets on that platform.

        Emitting both spellings is strictly WIDENING -- no target is removed, and a
        path is fenced under either spelling on either platform -- which is the
        right direction for a gate whose documented stance is that a *maybe*
        answers yes. Cheaper and more honest than teaching every candidate path to
        re-derive the separator it should have used.
        """
        parts = _leaf_segments(d)
        return {
            os.path.join(root, *parts).casefold(),
            "/".join([root.rstrip("/\\"), *parts]).casefold(),
            "\\".join([root.rstrip("/\\"), *parts]).casefold(),
        }

    sensitive_targets: set[str] = {_anchor(home, d) for d in home_dirs}
    # ``KIROCREW_OS_HOME`` is an ALTERNATE ``$HOME`` (see _resolved_root_key):
    # a pod-spawned kiro-cli child runs with it as its literal HOME, so its
    # credential store and the pod-minted OAuth grants live under this root. Every
    # entry re-anchors here -- the variable relocates the whole home, not just
    # ``.aws`` -- so a secret cannot be moved out from under its own gate.
    #
    # Resolved through ``_realpath_or_none`` for the same reason ``home`` is: it
    # touches the filesystem (and opens the directory on Windows), which is why the
    # whole rebuild runs off the loop, and ``None`` degrades to the lexical anchors
    # already added above rather than raising.
    if os_home:
        for d in home_dirs:
            sensitive_targets |= _anchor_both_separators(os_home, d)
        os_home_real = resolve_target(os_home) or os_home
        if os_home_real.casefold() != os_home.casefold():
            for d in home_dirs:
                sensitive_targets |= _anchor_both_separators(os_home_real, d)
    # ``home`` arrives RESOLVED from the cache key, so this is normally a no-op;
    # it still opens the directory on Windows, which is why the whole rebuild
    # runs off the loop.  None degrades to the lexical anchors already in the set.
    home_real = resolve_target(home) or home
    if home_real.casefold() != home.casefold():
        sensitive_targets |= {_anchor(home_real, d) for d in home_dirs}
    # ``home`` arrives RESOLVED (the cache is keyed on the resolved roots), so
    # the realpath above is normally a no-op and the LOGICAL spelling of a
    # symlinked ``$HOME`` was never anchored -- a gap masked as long as every
    # candidate was itself resolved.  Candidate resolution is now bounded and
    # degrades to the lexical spelling, so anchor the logical home explicitly:
    # ``~/.ssh/id_rsa`` spelled through ``/home/x`` must match even when
    # ``/home/x -> /local/home/x`` could not be followed in time.
    if logical_home.casefold() != home.casefold():
        sensitive_targets |= {_anchor(logical_home, d) for d in home_dirs}
    # When KIROCREW_HOME points to a non-default path, the keystone secrets
    # (token_signing.key, refresh_chains.json, .local_secret, sel_hmac.key,
    # security_policy.json etc.) live directly under it — NOT under either of
    # the default crew home prefixes (~/.kiro/crew, ~/.kirocrew). Without this
    # expansion any "<crew-prefix>/X" entry in the home_dirs list would miss
    # the real file location, letting the agent read/write its own signing key
    # or governance ceiling via the custom KIROCREW_HOME. Strip whichever crew
    # prefix an entry carries and re-anchor the leaf under the env-override
    # root ADDITIONALLY (the ~/-rooted default forms stay, so every location is
    # always covered).
    if crew_home:
        kiro_home = crew_home
        for d in home_dirs:
            for _prefix in _CREW_HOME_PREFIXES:
                # Compare with POSIX separators (home_dirs entries are authored
                # that way) so this matches regardless of the running os.sep.
                if d == _prefix or d.startswith(_prefix + "/"):
                    leaf = d[len(_prefix) :].lstrip("/")
                    full = os.path.join(kiro_home, *_leaf_segments(leaf)) if leaf else kiro_home
                    sensitive_targets.add(full.casefold())
                    # Also add the resolved form in case the env value itself has
                    # symlinks (matches the home/home_real duality above).
                    full_real = resolve_target(full)
                    if full_real is not None:
                        sensitive_targets.add(full_real.casefold())
                    break
    # The agents dir (``~/.kiro/agents``) follows ``KIRO_HOME`` — kiro-cli's own
    # home override, honoured by ``kiro_agents_dir()``. When it is set, the specs
    # the gateway execs live at ``<KIRO_HOME>/agents``, NOT under the real home,
    # so the ``.kiro/agents`` entry anchored above misses them and an agent write
    # there would bypass the gate. Re-anchor the leaf under the override, mirroring
    # the ``KIROCREW_HOME`` expansion directly above (the ~/-rooted default form
    # stays, so both locations are always covered). Only added when the agents dir
    # is actually in *home_dirs* — it is on the write-only tier
    # (``_WRITE_PROTECTED_HOME_PATHS``) and NOT in ``_SENSITIVE_HOME_DIRS``, so
    # this must not leak an agents target into the read gate. No validity check on
    # the override: an unsafe ``KIRO_HOME`` falls back to ``~/.kiro`` in
    # ``kiro_home()`` (already covered by the default form), so an extra target
    # under a bogus value is harmless and fail-safe.
    if kiro_home_override and _KIRO_AGENTS_DIR in home_dirs:
        agents_full = os.path.join(kiro_home_override, "agents")
        sensitive_targets.add(agents_full.casefold())
        agents_real = resolve_target(agents_full)
        if agents_real is not None:
            sensitive_targets.add(agents_real.casefold())
    # An ACP adapter's OAuth token follows that adapter's own home override, so
    # the ``$HOME``-rooted entry anchored above covers only the documented
    # default. Re-anchor the token leaf under each override the adapter honours
    # (the default form stays, so every location is always covered). Guarded on
    # membership in *home_dirs* for the same reason as the agents dir above: a
    # write-tier build must not gain a read-tier target.
    _adapter_roots = dict(resolved.adapter_roots)
    for _leaf, _root_envs, _under_root in _OVERRIDE_ANCHORED_LEAVES:
        if _leaf not in home_dirs:
            continue
        for _env in _root_envs:
            _root = _adapter_roots.get(_env)
            if not _root:
                continue
            _full = os.path.join(_root, *_leaf_segments(_under_root))
            sensitive_targets.add(_full.casefold())
            _full_real = resolve_target(_full)
            if _full_real is not None:
                sensitive_targets.add(_full_real.casefold())
    # Did any path this build resolved come through a symlink? Read off the memo
    # this build already filled -- the five classes named on ``_BuiltTargets``, so
    # one entry on a host with no home override set and about eighty with every one
    # set -- which costs one pass over that dict and no extra filesystem work: the
    # resolutions themselves are the expense and they have already happened. The
    # bulk of the set never appears here, because ``_anchor`` and
    # ``_anchor_both_separators`` build it without resolving anything. Normalised
    # on both sides so a separator or case
    # difference cannot read as a symlink; a false positive here is merely the
    # short expiry, a false negative would be the stale window this bounds.
    differed = any(
        value is not None
        and os.path.normcase(os.path.normpath(value)) != os.path.normcase(os.path.normpath(key))
        for key, value in resolved_paths.items()
    )
    built = _BuiltTargets(sensitive_targets)
    built.resolution_differed = differed
    return built


# How long a built target set stays reusable. ``_home_dir_targets_uncached``
# rebuilds a ~75-entry set on EVERY ``is_sensitive_path`` call and measured at
# 1.14ms of that call's 1.25ms (91%) on a dev desktop, because it realpath()s
# ``$HOME`` and each KIROCREW_HOME-anchored leaf. Callers hit it per FILE — one
# skills-tree walk made thousands of identical calls and took 4.2s, of which
# 3.5s was this rebuild.
#
# Deliberately TTL-bounded rather than a plain ``lru_cache``: part of the set is
# derived from FILESYSTEM state, so an unbounded cache would keep matching a
# stale target if a symlink were repointed after the cache warmed — a gate that
# fails OPEN. ``_HOME_TARGETS_TTL_MAX_SECS`` bounds that window.
#
# The key is built from the RESOLVED roots (``Path.home().resolve()`` and the
# resolved ``KIROCREW_HOME``), NOT from the raw env vars, because those two
# values are exactly what the builder anchors its targets on. Keying on the raw
# ``$HOME`` string is wrong twice over:
#   1. Repointing a symlink AT ``$HOME`` leaves ``$HOME`` unchanged while every
#      target moves, so the gate returns False for a credential path the
#      uncached code blocks (a real, reproduced bypass — see the regression
#      test ``test_repointed_home_symlink_is_not_served_from_cache``).
#   2. ``Path.home()`` reads ``USERPROFILE`` on Windows and never ``HOME``, so on
#      that platform the key omits the one variable that decides the anchor.
# Resolving the roots costs ~2 realpath calls (~0.06ms) against the ~1.14ms
# rebuild it replaces, so the win survives. Those calls -- and the rebuild
# itself -- run on the ``mc-pathres`` pool under the resolve budget, one thread
# hop each (``_resolved_root_key`` resolves all six roots in one job,
# ``_rebuild_targets_bounded`` resolves every leaf in one job), because
# an inline ``realpath($HOME)`` on a loaded Windows desktop blocked past the
# loop-stall watchdog; see ``_rebuild_targets_bounded``.
#
# Residual, accepted: a symlink swapped DEEPER inside the crew home (an
# individual keystone leaf, or an intermediate directory on the way to one) can
# still be served stale for up to the TTL. Detecting that needs the per-leaf
# realpath calls that ARE the expense — measured 45 realpath calls per build,
# 94% of its 1.39ms — so there is no cheap way to keep the cache and revalidate
# them.
#
# The TTL is therefore sized as small as it can be while still doing its job.
# The FLOOR is 0.1s, NOT a "few seconds", because a skills walk issues thousands
# of calls in a burst and one build serves the whole burst either way. Measured
# cold-walk cost against a FIXED constant, on an idle interpreter:
#     5.0s -> 0.95s    1.0s -> 0.93s    0.1s -> 0.95s    0.0s -> 4.66s
# So 0.1s keeps the entire win while cutting the stale window 50x versus 5.0s.
# Only 0.0 (no cache) closes the window completely, and that reverts to the 4.7s
# scan whose GIL-held cost wedges the event loop -- the defect this exists to fix.
_HOME_TARGETS_TTL_SECS = 0.1

# ---------------------------------------------------------------------------
# Why the expiry is not that floor alone.
#
# 0.1s was chosen against an IDLE interpreter, where one rebuild costs ~2ms, so
# the cache spends ~2% of the wall clock rebuilding. But the rebuild is ~130
# ``os.path.realpath`` calls and each syscall releases and re-acquires the GIL,
# so its cost is set by CONTENTION, not by the disk. Measured on this repo's own
# anchors -- 64-core Linux, local xfs, one mount, ``sys.getswitchinterval()``
# 5ms -- with N sibling threads running pure Python:
#     0 -> 2ms    1 -> 581ms    2 -> 399ms    4 -> 3859ms    8 -> 7456ms
# A fixed 0.1s expiry does not move with that, so the share of the wall clock
# spent rebuilding rises with load until the rebuild misses
# ``_PATH_RESOLVE_REBUILD_TIMEOUT_SECS`` and the gate refuses ordinary project
# files. That is the reported defect, and the filesystem is healthy throughout.
#
# So the expiry tracks the MEASURED cost of the build it is expiring:
#
#     ttl = clamp(last rebuild seconds * _HOME_TARGETS_TTL_COST_RATIO,
#                 _HOME_TARGETS_TTL_SECS, _HOME_TARGETS_TTL_MAX_SECS)
#
# The ratio is the reciprocal of the share of the wall clock the gate may spend
# rebuilding, so that share is held at ``1 / ratio`` at EVERY load level rather
# than only at the one the constant was picked on. The rebuild's own duration is
# the load signal, so no separate load metric is read and nothing has to be
# configured per host.
#
# WHAT THIS DOES NOT FIX, stated rather than implied: an expiry controls how
# OFTEN the rebuild is paid, never what ONE costs. Past roughly four contending
# threads a single COLD rebuild already exceeds its own budget, and there every
# expiry refuses alike -- measured, see ``scripts/measure_path_gate_ttl.py``.
# Only a resolver outside this interpreter's GIL closes that case. This law's
# job is narrower: it stops re-paying the contended cost every 100ms.
#
# THE TRADE, stated plainly: a longer expiry lengthens the window in which a
# symlink swapped DEEPER inside the crew home (a keystone leaf, or an
# intermediate directory on the way to one) is still answered from the stale
# set -- the residual named above, now bounded by ``_HOME_TARGETS_TTL_MAX_SECS``
# rather than by 0.1s. It does NOT widen the two reproduced bypasses, because
# both repoint an ANCHOR and every anchor is part of the cache KEY: a repointed
# ``$HOME`` or ``KIROCREW_HOME`` re-keys and misses the cache at any expiry,
# which ``test_repointed_home_symlink_is_not_served_from_cache_at_the_longest_expiry``
# pins at the maximum expiry as well as at the floor.
#
# Both inputs are NAMED constants, each pinned by a test, and each an OPERATOR
# KNOB read once at import and fail-soft to its reviewed default:
# ``KIROCREW_PATH_GATE_TTL_COST_RATIO`` and ``KIROCREW_PATH_GATE_TTL_MAX_SECS``.
# So a host can trade the two the other way, or revert to one fixed 0.1s expiry
# with a ratio of 0, without a release and without patching this file.
# ---------------------------------------------------------------------------

#: Environment overrides for the two inputs below, and the bounds each accepts.
#: Read ONCE at import, like the resolver pool size: these size a cache that is
#: already live by the first gate call, so a mid-run change would leave entries
#: written under one policy being judged by another.
_TTL_COST_RATIO_ENV = "KIROCREW_PATH_GATE_TTL_COST_RATIO"
_TTL_COST_RATIO_DEFAULT = 50.0
#: 0 is the REVERT: zero times any cost clamps to the floor for every input, so
#: ``KIROCREW_PATH_GATE_TTL_COST_RATIO=0`` pins one fixed 0.1s expiry at every
#: load level. That is deliberately the only spelling of the revert -- a separate
#: private boolean said the same thing while being unreachable from outside the
#: package, which made "operator lever" untrue. Above 1000 the clamp makes every
#: load level select the ceiling anyway, so a larger value expresses nothing this
#: cannot.
_TTL_COST_RATIO_MIN = 0.0
_TTL_COST_RATIO_MAX = 1000.0
_TTL_MAX_SECS_ENV = "KIROCREW_PATH_GATE_TTL_MAX_SECS"
_TTL_MAX_SECS_DEFAULT = 30.0
#: The ceiling an operator may raise the ceiling TO. The measurement justifies
#: nothing above about a minute -- that is the longest expiry the default ratio
#: asks for at the heaviest load the gate can still serve, and past that load no
#: expiry helps at all -- so 300s leaves generous headroom while refusing a value
#: that could only widen the stale window. The floor is the shipped floor: a
#: ceiling below it would invert the clamp.
_TTL_MAX_SECS_MIN = 0.1
_TTL_MAX_SECS_MAX = 300.0


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a float operator knob, falling back to *default*.

    Fail-soft TO THE DEFAULT in every failure mode -- absent, unparseable, out of
    range -- which is the conservative direction for both callers: the shipped
    ratio and the shipped ceiling are the reviewed values, so a bad override can
    only leave the reviewed behaviour in place and never widen the stale window
    the ceiling bounds.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using the default of %s", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "%s=%s is outside %s..%s; using the default of %s",
            name,
            value,
            minimum,
            maximum,
            default,
        )
        return default
    return value


#: Reciprocal of the share of the wall clock the gate may spend rebuilding
#: anchors. 50 holds that share at 2%, and it is the value at which an idle
#: interpreter selects the same 0.1s EXPIRY it selects today (2ms * 50 = the floor).
#: Only that duration is unchanged: an entry's total age is the expiry plus the build
#: that measured it, which for a 2ms build is the same to within those 2ms. So the
#: default is observationally identical on an unloaded host, by arithmetic rather
#: than by measurement.
#: Chosen as the SMALLEST ratio that refused nothing and paid the fewest
#: rebuilds at every load level the gate can still serve -- the same rule that
#: chose the floor, applied to the ratio. Larger ratios paid no fewer rebuilds
#: and only lengthened the stale window.
_HOME_TARGETS_TTL_COST_RATIO = _env_float(
    _TTL_COST_RATIO_ENV, _TTL_COST_RATIO_DEFAULT, _TTL_COST_RATIO_MIN, _TTL_COST_RATIO_MAX
)

#: Hard ceiling on the expiry, and therefore THE WORST-CASE STALE WINDOW for the
#: deeper-leaf residual described above: 30 seconds, and never longer by any
#: path through this module. Deliberately NOT derived from the ratio: at high
#: contention the ratio asks for minutes (a 7.5s rebuild wants 375s), and this
#: gives up the 2% share there rather than give up the freshness bound. Sized to
#: cover what the load levels the gate can still SERVE actually ask for -- a 0.4s
#: rebuild under two contending threads asks for 20s -- so the clamp binds only
#: where a single cold rebuild is already at its own budget and no expiry helps.
_HOME_TARGETS_TTL_MAX_SECS = _env_float(
    _TTL_MAX_SECS_ENV, _TTL_MAX_SECS_DEFAULT, _TTL_MAX_SECS_MIN, _TTL_MAX_SECS_MAX
)


def _home_targets_ttl(rebuild_secs: float, *, resolution_differed: bool = True) -> float:
    """How long a target set that took *rebuild_secs* to build stays reusable.

    See the block above the constants for why this is a function of the build's
    own cost rather than a constant.  *rebuild_secs* is the WALL time the cache
    fill took, pool queue wait included: the caller blocked for all of it, and a
    saturated pool is exactly a state in which refreshing less often is correct,
    so the queue is part of the cost being amortised rather than noise to
    subtract.

    *resolution_differed* is what keeps the long expiry off the one class of
    install it could hurt.  True means one of the paths the build RESOLVED came
    back spelled differently -- in practice a symlinked leaf under a home-override
    root, which is the whole population :class:`_BuiltTargets` enumerates -- and
    that resolution-derived entry is exactly what a repoint can move out from
    under a cached set, the deeper-leaf residual.  There the expiry is
    pinned to the floor, so that window stays at 0.1s and does not grow.  False
    means nothing resolved differently, so the set holds nothing a repoint can
    stale WITHOUT first creating a symlink inside the crew home, which is a write
    the write gate refuses; the adaptive expiry applies.  It defaults to True so
    every caller that cannot prove otherwise gets the short expiry.

    A STALLED rebuild never reaches here -- it raises and no entry is written --
    so a dead mount cannot inflate the expiry.  A zero or negative duration (a
    coarse clock, or a frozen one in tests) yields the floor, and so does a
    ratio of zero, which is how an operator pins one fixed 0.1s expiry at every
    load level.
    """
    if resolution_differed:
        return _HOME_TARGETS_TTL_SECS
    scaled = rebuild_secs * _HOME_TARGETS_TTL_COST_RATIO
    return min(max(scaled, _HOME_TARGETS_TTL_SECS), _HOME_TARGETS_TTL_MAX_SECS)


def _home_targets_tier(home_dirs: list[str]) -> str:
    """Name the gate whose ``home_dirs`` list this is, for the expiry-pin line.

    A label for a reader, never a decision: :func:`_report_expiry_pin` keys its
    state on the list itself, so an unrecognised list still gets its own slot and
    the right dedup while being named generically here. That is what keeps this
    from being a second place a new gate must be registered for correctness --
    ``test_every_gate_list_handed_to_the_builder_has_a_tier_name`` scans the call
    sites and fails when one arrives without a name, so the generic answer is a
    floor rather than somewhere to stop.

    Compared against the live lists rather than a table frozen at import, because
    the lists are assembled by ``+=`` across this module and a test may extend one.
    """
    if home_dirs == _SENSITIVE_HOME_DIRS:
        return "read"
    if home_dirs == _SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS:
        return "write"
    if home_dirs == _KEYSTONE_ARTIFACT_PARENTS:
        return "keystone-artifact"
    return f"unnamed {len(home_dirs)}-entry"


#: Last state :func:`_report_expiry_pin` reported FOR EACH ``home_dirs`` list, so the
#: line is emitted once per tier per TRANSITION rather than once per rebuild. Keyed on
#: the list itself rather than on one shared slot because the gates hand the builder
#: different lists whose answers can disagree on one host -- see
#: :func:`_report_expiry_pin`. The roots are deliberately NOT part of this key: they key
#: the target CACHE, but a tier reporting the same answer under new roots has not
#: transitioned and must not re-log. Bounded by the number of gate lists, a handful of
#: module constants, so it needs no eviction. A dict rather than a module global because
#: it is written from inside a function.
_home_targets_pin_state: dict[tuple[str, ...], bool] = {}


def _report_expiry_pin(home_dirs: list[str], pinned: bool) -> None:
    """Say once per tier, on each transition, which expiry that build selects.

    Without this the availability half self-disables in silence. On an install
    whose RESOLVED leaf under a home-override root is a symlink -- a crew-prefixed
    entry of the caller's ``home_dirs`` under ``KIROCREW_HOME``, the agent-spec dir
    under ``KIRO_HOME``, or a declared harness credential leaf under any variable in
    ``host_auth.home_override_env_vars()`` --
    the build that was handed that leaf reports a traversal, ITS expiry is
    therefore the floor, and both knobs read as no-ops for that tier to whoever
    tunes them. The refusals then come back with nothing in the log to say why the
    fix did not apply to this host.

    A dotfile-managed home IS a case to look for. The trigger is narrower than
    "any symlinked dotfile" but not exotic: the symlinked path has to fall in one
    of the three classes above -- a crew-prefixed entry of the caller's
    ``home_dirs`` under ``KIROCREW_HOME``, the agent-spec dir under ``KIRO_HOME``,
    or a declared harness credential leaf -- and it has to sit BELOW the resolved
    root, since ``_resolve_root_anchors`` canonicalises each root first. A
    symlinked ``sessions`` leaf under a ``KIROCREW_HOME`` below ``$HOME`` is that
    shape, and so is a ``goose`` directory symlinked into a store underneath
    whatever ``XDG_CONFIG_HOME`` resolves to -- the leaf resolved is
    ``$XDG_CONFIG_HOME/goose/secrets.yaml``, so the symlink has to be under the
    exported root and not under ``~/.config`` unless that is the same directory. A
    dotfile in none of those classes cannot report
    a traversal, because the bulk targets are never resolved.

    The dedup is per ``home_dirs`` list, because that list is what decides the
    answer. The gates hand the builder three different ones -- the read gate passes
    ``_SENSITIVE_HOME_DIRS``, :func:`is_sensitive_write_path` passes that plus
    ``_WRITE_PROTECTED_HOME_PATHS``, and :func:`_is_keystone_publish_artifact`
    passes ``_KEYSTONE_ARTIFACT_PARENTS`` -- while a symlinked write-protected crew
    leaf that holds no secret (``models``, ``sessions``, ``app-sources``) sits in
    the write gate's list alone. So one host can have the write build resolve that
    leaf and pin the floor while the keystone-artifact build reports no traversal
    and selects the cost-tracking expiry. Under one shared slot each of those builds
    flips the key the other just set, so the line alternates between two opposite
    messages for as long as the disagreement lasts and neither survives long enough
    to read as a state. A per-list slot lets each build transition its OWN state: a
    steady host says it once per tier rather than once per rebuild, and a tier that
    flips says it again.

    Both messages name the tier for the same reason. A reader diagnosing a refusal
    needs to know WHICH gate is pinned, and on a host whose builds disagree a line
    naming none of them cannot answer that -- the "cost-tracking expiry in force"
    half would read as the state of the whole gate while another tier is pinned to
    the floor, which is the opposite of the truth for whoever is tuning the knobs.
    """
    key = tuple(home_dirs)
    if _home_targets_pin_state.get(key) is pinned:
        return
    _home_targets_pin_state[key] = pinned
    tier = _home_targets_tier(home_dirs)
    if pinned:
        logger.info(
            "sensitive-path anchor cache (%s tier): expiry pinned to the %.1fs floor "
            "because a path under a home-override root resolved through a symlink, so "
            "the cost-tracking expiry and both of its knobs do not apply to this tier "
            "while that holds",
            tier,
            _HOME_TARGETS_TTL_SECS,
        )
    else:
        logger.info(
            "sensitive-path anchor cache (%s tier): cost-tracking expiry in force "
            "(ratio %s, cap %.1fs)",
            tier,
            _HOME_TARGETS_TTL_COST_RATIO,
            _HOME_TARGETS_TTL_MAX_SECS,
        )


# key -> (expiry_monotonic, targets)
_home_targets_cache: dict[tuple[object, ...], tuple[float, set[str]]] = {}
# Only off-loop bulk readers acquire this lock. Event-loop gates retain their
# bounded resolver and must never wait for an inline filesystem rebuild.
_home_targets_inline_lock = threading.Lock()


class _ResolvedRoots(NamedTuple):
    """The roots the sensitive-target set is anchored on, AND its cache key.

    Those two jobs are the same object on purpose: every field is part of the
    key, so an override that would move a target invalidates the cached set
    instead of serving targets anchored on the previous value. Keying on fewer
    fields than the builder anchors on is the fail-OPEN shape the resolved-home
    key already exists to prevent.

    A new adapter with its own credential home adds NOTHING here: its override
    variables arrive in ``adapter_roots``, projected from its own declaration in
    ``agent_sdk.host_auth``. That is what keeps this tuple fixed-width as harnesses
    are added, and what keeps the anchors and the key derived from one table rather
    than from a per-adapter field each caller has to remember to pair up.
    """

    home: str
    crew_home: str | None
    kiro_home: str | None
    #: Each declared ``$HOME``-override variable and the root it resolves to, in
    #: declaration order, ``None`` when the variable is unset.
    #:
    #: A TUPLE of pairs rather than a dict because this NamedTuple is also the
    #: cache key: it has to hash, and it has to change exactly when an override
    #: that would move a target changes. A dict would make the key unhashable,
    #: and the fallback -- keying on fewer fields than the builder anchors on --
    #: is the fail-OPEN shape the resolved-home key exists to prevent.
    adapter_roots: tuple[tuple[str, str | None], ...]
    logical_home: str
    # ``KIROCREW_OS_HOME`` is an ALTERNATE WHOLE ``$HOME``, not one adapter's
    # credential leaf: ``pod.runtime.build_pod_env`` sets it and
    # ``acp.client._apply_pod_home_remap`` makes it the literal ``HOME`` of a
    # pod-spawned kiro-cli child, so that child's whole credential store -- the
    # runtime identity store ``pod.runtime._seed_pod_os_home`` snapshots in, and
    # the MCP OAuth grants that child MINTS under ``.aws/sso/cache`` -- lives
    # under this root. No host SSO cache contents are copied in; that staging was
    # removed. It is therefore anchored by re-anchoring EVERY ``home_dirs`` entry
    # in ``_home_dir_targets_uncached``, rather than through
    # ``_OVERRIDE_ANCHORED_LEAVES``, which maps one leaf to the roots that move
    # it. Without it the relocated tree sits at a path no matcher
    # covers, so an agent inside a pod could read the operator's identity token
    # at the pod-path spelling while the identical bytes at ``~/.aws`` are
    # refused.
    os_home: str | None


#: Sensitive leaf -> the ``$HOME``-override VARIABLES that move it, and the
#: spelling it takes under each of them.
#:
#: PROJECTED from the harness declarations, not enumerated: the pairing has to
#: name the same leaf the list above fences and the same variable the resolver
#: below reads, and it was a third hand-maintained copy of both. A leaf absent
#: from the *home_dirs* list being built is skipped, so a write-tier build never
#: leaks a read-tier target.
#:
#: Read once at import, like the leaf list itself: the declarations are static
#: data, and re-projecting per gate call would put a table walk on the hot path.
_OVERRIDE_ANCHORED_LEAVES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    host_auth.override_anchored_leaves()
)


def _resolved_env_root(name: str) -> str | None:
    """Resolve an environment home override, or ``None`` when it is unset.

    Falls back to the unresolved absolute form on OSError/ValueError the same way
    the builder does. No validity check: an unsafe override falls back to its
    default inside the owning helper, and that default is already covered by the
    ``$HOME``-rooted entry, so an extra target under a bogus value is harmless
    and fail-safe.

    The value is read VERBATIM -- deliberately not stripped. Whitespace is a legal
    POSIX path character, and the owning resolvers take the variable raw
    (``_valid_override_home`` does ``Path(os.environ.get("KIROCREW_HOME"))``, and
    ``config_dir`` then ``mkdir``s whatever that names). Stripping here would
    anchor the target set on ``<root>`` while the process actually runs out of
    ``"<root> "``, leaving the real ``.env``, signing keys and governance files
    outside the floor this set defines. Emptiness is the only test, so an unset
    or empty override still resolves to ``None``.
    """
    expanded = _expanded_env_root(name)
    if expanded is None:
        return None
    # Runs on the ``mc-pathres`` pool via _resolve_root_anchors -- never call it
    # from the event loop directly; go through _resolved_root_key.  A failure
    # keeps the lexical form, exactly as the OSError arm did.  ``Path.resolve()``
    # is ``os.path.realpath`` underneath, so the resolved spelling is unchanged.
    return _realpath_or_none(expanded) or _lexical_root(expanded)


def _expanded_env_root(name: str) -> str | None:
    """The ``~``-expanded value of an environment home override, or ``None``."""
    raw = os.environ.get(name, "")
    if not raw:
        return None
    return os.path.expanduser(raw)


def _lexical_root(expanded: str) -> str:
    """Absolute, normalized spelling of *expanded* WITHOUT touching the filesystem.

    Deliberately not ``os.path.abspath``: on Windows that calls
    ``GetFullPathName``, which strips a trailing space or dot -- and the
    verbatim contract above says the anchor must keep it, because the owning
    resolvers run out of exactly the spelling the variable carries.
    """
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    return os.path.normpath(expanded)


#: The HOST's own override roots :func:`_resolve_root_anchors` resolves, in field
#: order. Each relocates a whole tree this core owns or honours itself.
#:
#: A harness's own credential home is NOT here: those arrive from
#: :func:`host_auth.home_override_env_vars` into ``adapter_roots``, so adding a
#: harness edits neither this tuple, nor ``_ResolvedRoots``, nor either of the two
#: loops that anchor on them.
_OVERRIDE_ROOT_ENVS: tuple[tuple[str, str], ...] = (
    ("crew_home", "KIROCREW_HOME"),
    ("kiro_home", "KIRO_HOME"),
    ("os_home", "KIROCREW_OS_HOME"),
)


def _resolve_root_anchors(logical_home: str) -> _ResolvedRoots:
    """Resolve every root the target set anchors on; runs on the ``mc-pathres`` pool.

    One worker call resolves ``$HOME`` and all six override roots together,
    so :func:`_resolved_root_key` -- which runs once per ``is_sensitive_path``
    call, on the event loop -- pays a single thread hop rather than seven.  The
    stall bookkeeping is charged to the logical home's prefix: that is the
    mount every root ordinarily lives under, and it is the one the crash dumps
    named.
    """
    home = _realpath_or_none(logical_home) or logical_home
    overrides = {field: _resolved_env_root(env) for field, env in _OVERRIDE_ROOT_ENVS}
    # Resolved in the SAME worker call as the host's own roots, for the reason
    # above: one thread hop for every anchor, rather than one more per harness.
    adapter_roots = tuple(
        (env, _resolved_env_root(env)) for env in host_auth.home_override_env_vars()
    )
    return _ResolvedRoots(
        home=home, logical_home=logical_home, adapter_roots=adapter_roots, **overrides
    )


def _resolved_root_key() -> _ResolvedRoots:
    """Return the roots the target set is anchored on.

    Mirrors how :func:`_home_dir_targets_uncached` derives its anchors, so the
    cache key changes exactly when the anchors would. Falls back to the
    unresolved form on OSError/ValueError the same way the builder does.

    ``kiro_home`` is the resolved ``KIRO_HOME`` override (kiro-cli's own home
    override, honoured by ``kiro_agents_dir()``), or ``None`` when unset — it
    re-anchors the ``~/.kiro/agents`` write-protection, so a changed ``KIRO_HOME``
    must invalidate the cache. No validity check here (an unsafe value falls back
    to ``~/.kiro`` in ``kiro_home()``, already covered by the default form); it is
    resolved only so a symlinked override keys and anchors identically.

    ``adapter_roots`` does the same for every declared harness credential home in
    ``_OVERRIDE_ANCHORED_LEAVES``.

    ``logical_home`` is ``Path.home()`` UNRESOLVED.  It is a separate anchor, not
    a duplicate: on a host where ``$HOME`` is itself a symlink (``/home/x`` ->
    ``/local/home/x`` on cloud desktops) the resolved home spells every target
    one way while an agent-supplied ``~/.ssh/id_rsa`` spells it the other.  The
    resolved CANDIDATE normally bridges that -- but candidate resolution is
    bounded (:func:`_resolved_forms_bounded`) and degrades to the lexical
    spelling, which must still hit a target or the gate fails OPEN on exactly
    the hosts where ``$HOME`` is a link.  Keyed here so an env change that
    moves the logical spelling invalidates the cache like any other anchor.

    ``os_home`` is the resolved ``KIROCREW_OS_HOME`` override, or ``None`` when
    unset. It is an ALTERNATE ``$HOME``: ``pod.runtime.build_pod_env`` sets it
    and ``acp.client._apply_pod_home_remap`` makes it the literal ``HOME`` of a
    pod-spawned kiro-cli child, so kiro-cli's own ``$HOME``-derived credential
    store — the runtime identity store ``pod.runtime._seed_pod_os_home`` mirrors
    in, and the MCP OAuth grants that pod's own child MINTS under
    ``.aws/sso/cache`` — lives under this root rather than under the real home.
    No host SSO cache contents are copied in; that staging was removed. Anchoring
    it here is what fences the relocated tree, and it is the ONLY layer that can:
    the pod child's mount namespace must keep that tree readable AND writable
    because kiro-cli writes its grants there and no env lever relocates them. So
    the two audiences are split — the harness process reaches the tree, while an
    agent TOOL call naming any path under it is refused in-band here. Every
    ``home_dirs`` entry is re-anchored under it, not merely ``.aws``, because
    the variable relocates the whole home: the crew-home leaves, ``.ssh`` and
    every other fenced entry move with it. Same reasoning as the
    ``KIROCREW_HOME`` expansion below — an override must not move a secret out
    from under its own gate.
    """
    logical_home = str(Path.home())
    # Bounded (see _rebuild_targets_bounded): this runs on the event loop once per
    # is_sensitive_path call, and an inline resolve of a slow-to-stat $HOME is
    # exactly the stall the watchdog dumps caught.  All seven roots resolve in
    # ONE pool hop (_resolve_root_anchors).
    #
    # INVARIANT: the gate only ever compares against anchors resolved FRESH,
    # canonically, within the budget.  Anything else -- a stall, an open
    # cooldown, a pinned or faulted pool -- raises, and every gate turns that
    # into a refusal, exactly as it does for a stalled candidate.  Three
    # weaker fallbacks were each found open in review: lexical spellings (a
    # symlinked override root on another mount loses its canonical target), a
    # UNC skip (same, via a junction in a UNC home), and serving the previous
    # canonical resolution (a symlink repointed during the stall moves the
    # credential out from under the stale anchor).  Refusing for the cooldown
    # is the one outcome none of those reach.
    try:
        roots = _run_resolution_bounded(logical_home, _resolve_root_anchors)
    except PathResolutionStalled:
        roots = None
    if roots is None:
        raise PathResolutionStalled(logical_home, _stall_prefix(logical_home))
    return roots


def _home_dir_targets(home_dirs: list[str], *, inline: bool = False) -> set[str]:
    """TTL-cached :func:`_home_dir_targets_uncached`.

    Keyed on the *home_dirs* list plus the RESOLVED home and crew-home roots
    (see the note above the constant for why the raw env vars are not enough).

    *inline* resolves the anchors and rebuilds the set on the CALLING thread
    instead of through the bounded ``mc-pathres`` hop -- the same stance
    :func:`sandbox_credential_targets` takes, and for the same reason: the
    bound exists to keep the EVENT LOOP responsive, and a caller that is
    already on a worker thread gains nothing from it while its submissions
    queue ahead of the loop's own. Reserved for :func:`is_sensitive_resolved_path`,
    whose contract is exactly such a caller. The freshness invariant is kept:
    the roots are still resolved on every call and key the cache, so a repointed
    root still invalidates; what changes is only WHERE the ``realpath`` runs. A
    wedged mount blocks the calling thread here rather than raising a stall --
    which is what the same thread's own ``os.walk`` on that mount does anyway.

    ponytail: the returned set is the cached instance, not a copy — both
    callers only iterate it. A future caller that MUTATES the result would
    poison the cache for every other caller; copy here if that ever happens.
    """
    # Resolve the roots ONCE and use the same tuple for both the key and the
    # build. Resolving separately lets a root symlink repointed between the two
    # reads file one root's targets under the other root's key — a fail-OPEN
    # TOCTOU, pinned by the regression test
    # test_roots_are_resolved_once_for_key_and_build.
    if inline:
        roots = _resolve_root_anchors(str(Path.home()))
        cached = _home_targets_cache.get((tuple(home_dirs),) + roots)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        with _home_targets_inline_lock:
            # Resolve after acquiring: a queued worker must not reuse anchors
            # captured before another worker's potentially slow rebuild.
            roots = _resolve_root_anchors(str(Path.home()))
            return _cached_home_dir_targets(home_dirs, roots, inline=True)
    return _cached_home_dir_targets(home_dirs, _resolved_root_key(), inline=False)


def _cached_home_dir_targets(
    home_dirs: list[str], roots: _ResolvedRoots, *, inline: bool
) -> set[str]:
    """Share the target cache while coalescing inline builders at the caller."""
    key = (tuple(home_dirs),) + roots
    now = time.monotonic()
    cached = _home_targets_cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    if inline:
        targets = _home_dir_targets_uncached(home_dirs, roots)
    else:
        targets = _rebuild_targets_bounded(home_dirs, roots)
    # Read the clock AGAIN, and start the expiry here rather than at ``now``.
    # Two reasons, neither of them visible while a rebuild costs 2ms. The interval
    # between the two reads is the measured cost the expiry is a multiple of.
    # And an expiry that started BEFORE the build would already have elapsed by
    # the time a contended build returned -- a 2s build under a 0.1s expiry
    # hands back an entry that is expired on arrival, so the next call rebuilds
    # again and the cache stops being a cache exactly when it matters most.
    built_at = time.monotonic()
    # Read the flag off the built set, defaulting to the fail-safe answer: a test
    # double or any future builder that returns a plain ``set`` carries no
    # attribute and gets the floor rather than the long expiry.
    differed = bool(getattr(targets, "resolution_differed", True))
    _report_expiry_pin(home_dirs, differed)
    ttl = _home_targets_ttl(max(0.0, built_at - now), resolution_differed=differed)
    # Bound the dict: the key space is tiny (two constant home_dirs lists ×
    # roots), but a test or embedder that churns KIROCREW_HOME must not grow it
    # without limit.
    if len(_home_targets_cache) > 32:
        _home_targets_cache.clear()
    _home_targets_cache[key] = (built_at + ttl, targets)
    return targets


def _rebuild_targets_bounded(home_dirs: list[str], roots: _ResolvedRoots) -> set[str]:
    """Rebuild the target set on the ``mc-pathres`` pool.

    The anchors -- ``$HOME``, the ``KIROCREW_HOME`` / ``KIRO_HOME`` / adapter
    override roots and the ~40 keystone leaves under them -- are the paths the
    sensitive-target set is built FROM, as opposed to the agent-supplied
    candidate checked AGAINST it.  They are deliberately NOT ``realpath``'d
    inline on the event loop every time the target cache expires: on a Windows
    desktop under heavy disk load (a full test run plus several subagents, all
    being scanned by real-time antivirus) ``realpath($HOME)`` blocks past the
    25s loop-stall watchdog from inside ``on_tool_call``, and the gateway exits
    with every in-flight turn -- the same crash the bounded candidate
    resolution already prevents for the OTHER half of the check.

    The whole rebuild is ONE pool job, not one per leaf: a single bash command
    can drive ~200 rebuilds (``test_chained_cd_expansions_do_not_blow_up_the_gate``),
    and 40 thread hops per rebuild is what turns a 9s gate into a 15s one.  The
    stall bookkeeping is charged to ``roots.home``'s prefix, the mount every
    anchor ordinarily lives under and the one the crash dumps named.

    It carries its OWN budget (``_PATH_RESOLVE_REBUILD_TIMEOUT_SECS``) because that
    single job does ~130 ``realpath`` calls where a candidate resolution does one or
    two: sharing the candidate's budget sized the wait to the wrong work and let an
    ordinarily-slow rebuild on a loaded host read as a stalled mount.

    A rebuild that does not complete canonically within the budget RAISES, and
    every gate turns that into a refusal -- the same invariant as
    :func:`_resolved_root_key` (see the comment there for the three weaker
    fallbacks review found open: lexical spellings, a UNC skip, and serving the
    previous canonical set).  A pool fault is treated exactly like a stall, and
    a UNC home is probed here too (bounded): the candidate-side UNC shortcut is
    about tokens, not about the fence.  The stall is recorded, so the rebuild
    does not re-probe the wedged mount on every expiry -- it refuses without touching
    the filesystem until the cooldown lapses.
    """
    try:
        targets = _run_resolution_bounded(
            roots.home,
            lambda _home: _home_dir_targets_uncached(home_dirs, roots),
            budget=_PATH_RESOLVE_REBUILD_TIMEOUT_SECS,
        )
    except PathResolutionStalled:
        targets = None
    if targets is None:
        raise PathResolutionStalled(roots.home, _stall_prefix(roots.home))
    return targets


def _path_in_home_dirs(
    path_str: str,
    home_dirs: list[str],
    base_dir: str | None = None,
    *,
    strict: bool = False,
    pre_resolved: bool = False,
) -> bool:
    """Return True if *path_str* resolves under any of *home_dirs* (``$HOME``-relative).

    Shared matching core for :func:`is_sensitive_path` (read+write gate,
    ``_SENSITIVE_HOME_DIRS``) and :func:`is_sensitive_write_path` (write-only
    gate, the read+write set PLUS ``_WRITE_PROTECTED_HOME_PATHS``). Keeping one
    implementation means the symlink/casefold hardening below cannot drift
    between the two gates.

    ── Symlink robustness (pentest AWS-345 / AWS-62) ──
    A workspace symlink pointing at ``~/.aws/credentials`` (absolute OR relative
    ``../../.aws/credentials`` traversal) must NOT be readable through the link.
    We therefore check MULTIPLE candidate forms of the input and return True if
    ANY of them lands in a matched location:

      1. the fully symlink-RESOLVED canonical target (``realpath`` /
         ``Path.resolve`` — follows every symlink in the chain, including
         intermediate directories and the final component).  This is what
         defeats the symlink bypass: the resolved target of the link is
         ``~/.aws/credentials`` even though the link's own name is benign.
      2. the LEXICALLY-normalized path (no symlink following) and the raw
         expanded string — so a path that *textually* names a matched dir is
         still caught when resolution fails (dangling link, permission error).

    ``base_dir`` anchors a *relative* input against the caller's known working
    directory (e.g. the agent's workspace cwd) so a relative title like
    ``sub/cfg.ini`` resolves against the real directory rather than whatever CWD
    the gateway process happens to have.  Absolute inputs are unaffected;
    ``base_dir=None`` preserves the historical CWD-relative behavior.
    ``pre_resolved`` is :func:`_candidate_forms`'s flag of the same name, and
    such a caller is by contract on its own worker thread, so the anchors are
    resolved inline as well (``_home_dir_targets(inline=True)``): the whole
    check then performs no ``mc-pathres`` submission.
    """
    if not path_str:
        return False

    try:
        candidates = _candidate_forms(path_str, base_dir, pre_resolved=pre_resolved)
        # The anchors are bounded the same way (see _rebuild_targets_bounded):
        # a stall with no prior canonical resolution to serve refuses too.
        sensitive_targets = _home_dir_targets(home_dirs, inline=pre_resolved)
    except PathResolutionStalled:
        # Canonical form unavailable (wedged mount under the path): refuse.  A
        # lexical-only match here would pass a workspace symlink into a
        # credential store for the length of the stall.  A *strict* caller
        # (``sensitive_path_refusal``) wants to REPORT that as what it is rather
        # than as a match, so it gets the exception; the refusal is the same.
        if strict:
            raise
        return True

    # Case-fold both sides for the membership test.  On a case-insensitive
    # filesystem (macOS APFS/HFS+ default — a supported platform) the OS opens
    # ``~/.kirocrew/Security_Policy.json`` and ``~/.kirocrew/security_policy.json``
    # as the SAME file, so a byte-exact comparison would let the agent write its
    # own governance ceiling via an alternate-case path. Folding is strictly more
    # protective (it can only ever over-match an alternate-case variant of an
    # already-sensitive path, which is itself suspicious), so it is safe on
    # case-sensitive Linux too — matching the IGNORECASE bash-read matcher.
    for cand in candidates:
        cand_cf = cand.casefold()
        for sensitive_path in sensitive_targets:
            if cand_cf == sensitive_path or cand_cf.startswith(sensitive_path + os.sep):
                return True
    return False


def _is_keystone_publish_artifact(
    path_str: str,
    base_dir: str | None = None,
    *,
    strict: bool = False,
    pre_resolved: bool = False,
) -> bool:
    """Return True if *path_str* is the atomic-write temp or lock beside a keystone leaf.

    Closes the gap between a keystone leaf's FINAL name, which
    :data:`_SENSITIVE_HOME_DIRS` fences, and the intermediate inodes its publish
    actually goes through -- see :data:`_KEYSTONE_ARTIFACT_PARENTS` for why the rule is
    derived from the leaf list instead of restated per leaf.

    Two properties are load-bearing:

    - It reuses :func:`_candidate_forms` and :func:`_home_dir_targets`, so the
      symlink-resolution, casefolding and ``KIROCREW_HOME`` re-anchoring cannot drift
      from the main gate. A relocated crew home is covered because a
      ``<crew-prefix>``-rooted entry hits the prefix-stripping arm in
      :func:`_home_dir_targets_uncached`; a symlink aimed at a live temp is covered
      because the resolved form is one of the candidates.
    - The parent is compared for EQUALITY, not by prefix. An artifact is a direct child
      of the leaf's own directory, and a prefix test would sweep every descendant of the
      crew home whose name happens to end in ``.tmp`` -- far wider than this needs, in a
      directory that must stay readable.
    """
    if not path_str:
        return False
    try:
        artifact_parents = _home_dir_targets(_KEYSTONE_ARTIFACT_PARENTS, inline=pre_resolved)
        candidates = _candidate_forms(path_str, base_dir, pre_resolved=pre_resolved)
    except PathResolutionStalled:
        if strict:
            raise
        return True  # fail closed: see _path_in_home_dirs
    for cand in candidates:
        cand_cf = cand.casefold()
        # Suffixes are authored lowercase and the candidate is casefolded, so this is
        # the same case-insensitive comparison the rest of the gate makes -- on
        # macOS/Windows ``FOO.TMP`` and ``foo.tmp`` are the same file.
        if not cand_cf.endswith(_KEYSTONE_ARTIFACT_SUFFIXES):
            continue
        if os.path.dirname(cand_cf) in artifact_parents:
            return True
    return False


# Credential dot-dirs denied as a path COMPONENT anywhere in an app-picked local
# folder. This broadens the `is_sensitive_path()` floor below, which resolves its
# entries relative to $HOME and pins `.kube`/`.docker` to single leaf files
# (`config`, `config.json`): membership here denies these directory names at any
# depth and covers those two dirs whole. `path_contains_sensitive()` supplies the
# complementary ancestor/root protection. Owned here so every consumer
# (design_critique's local-target guard, design_tweak's project-folder guard)
# screens against the same set — a credential directory added for one app is
# automatically denied by the others.
DENIED_ROOT_PARTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})


def is_sensitive_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path points to a read+write-sensitive location.

    Used across every file-access surface (hooks.on_tool_call, validate_file_path,
    artifacts, dashboard file I/O, knowledge indexing) to block BOTH reads and
    writes of credential files and the governance trust-root
    (:data:`_SENSITIVE_HOME_DIRS`). See :func:`_path_in_home_dirs` for the
    symlink/casefold matching contract.

    Also covers a protected leaf's publish artifacts
    (:func:`_is_keystone_publish_artifact`): the temp an ``atomic_write`` renames over
    the leaf holds the leaf's full payload, so READ is blocked alongside write -- a
    write-only fence there would still disclose ``.env`` or ``token_signing.key`` to a
    reader that wins the race.

    The decision is :func:`sensitive_path_refusal`'s -- this is its boolean
    spelling, so the two cannot diverge. A stall is refused there (a string) and is
    therefore ``True`` here: the callers that only hold this boolean keep refusing
    fail-closed; what they lose is the distinct WORDING, which is the gate
    consumers' business.
    """
    return sensitive_path_refusal(path_str, base_dir) is not None


def is_sensitive_resolved_path(resolved: str) -> bool:
    """:func:`is_sensitive_path` for a path the caller has ALREADY canonicalised.

    Same decision and same targets, with NO ``mc-pathres`` submission on either
    half: the candidate is matched lexically, and the anchors (``$HOME``, the
    override roots, the keystone leaves) are resolved inline on the calling
    thread (:func:`_home_dir_targets` with ``inline=True``), fresh on every call
    and keying the same TTL cache the bounded path uses. *resolved* MUST be the
    output of ``os.path.realpath`` (or ``Path.resolve``) that the caller computed
    on its OWN worker thread: the only thing the bounded resolution would add for
    such an input is the same string back, since a canonical path has no link
    left to follow. Handing this an unresolved spelling is a link bypass, and
    calling it from the event loop forfeits the bound the pool exists to give
    that loop -- so it is for exactly one shape of caller: a bulk WALK on a
    worker thread that already resolves every entry to detect symlink loops and
    prove containment, and only then asks whether the entry is fenced.

    Why a separate entry point rather than "just call the pool anyway": the pool
    is sized for the event loop (two workers by default --
    ``executors._MAX_PATH_RESOLVE_WORKERS`` -- so a wedged mount can pin at most
    that many threads), and it is FIFO. A walk over a thousand skill directories, each
    submitting a resolution the walk had already performed plus an anchor
    resolution per call, fills that queue from worker threads while the loop's
    own latency-critical resolutions wait behind it -- not for a slow disk, for
    the queue -- and the accumulated waits cross the loop-stall watchdog. The
    scanner's realpath is unbounded either way (it runs off the loop, and
    ``os.walk`` on the same mount is unbounded too), so the pool bought that
    caller nothing and cost the loop its budget.

    A wedged mount therefore does not surface here as a refusal: it blocks the
    calling thread inside ``realpath``, exactly as that thread's own walk of the
    same mount would. Nothing is admitted while it blocks.
    """
    return _path_in_home_dirs(resolved, _SENSITIVE_HOME_DIRS, pre_resolved=True) or (
        resolved.casefold().endswith(_KEYSTONE_ARTIFACT_SUFFIXES)
        and _is_keystone_publish_artifact(resolved, pre_resolved=True)
    )


def is_sensitive_canonical_path(resolved: str) -> bool:
    """The sensitive-path verdict for a path the CALLER already canonicalised.

    The one entry point for a reader that computed ``os.path.realpath`` or
    ``Path.resolve`` itself and would otherwise hand the result to
    :func:`is_sensitive_path`, which resolves it again on the ``mc-pathres``
    pool and fails closed when the pool misses its budget. Which gate answers
    depends on the calling thread, and that is decided here rather than by the
    caller's say-so:

    * Off the event loop, :func:`is_sensitive_resolved_path` answers inline: no
      pool submission, so a saturated pool cannot refuse a healthy file.
    * On the event loop, :func:`is_sensitive_path` answers, bounded: the inline
      anchor ``realpath`` the pre-resolved gate performs is the blocking call
      the pool exists to keep off the loop, and the anchors (the override
      roots) need not share the caller's mount.

    The decision is the same gate list either way; only the submission path
    differs. The canonical-spelling half of the contract stays the caller's:
    hand this the resolved spelling, never the raw one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return is_sensitive_resolved_path(resolved)
    return is_sensitive_path(resolved)


#: The fixed opening of an unverifiable-path refusal. Consumers tell a stall from a
#: match with :func:`is_unverifiable_path_refusal`, a prefix test, and never by
#: searching the text: both refusals embed the caller-chosen path, and a prefix is
#: the one place that path cannot reach -- a substring test would let a path
#: spelled to contain the phrase pass itself off as a stall.
UNVERIFIABLE_PATH_PREFIX = (
    "Blocked: the path could not be verified against the sensitive-path list within "
    "the resolver budget"
)


def is_unverifiable_path_refusal(reason: str) -> bool:
    """True when *reason* is the stall refusal :func:`sensitive_path_refusal` produces.

    Structural, by the fixed prefix that precedes any caller-influenced text. The
    match refusal opens ``Blocked: access to sensitive path:`` instead, so no path
    spelling can move one refusal into the other's class.
    """
    return reason.startswith(UNVERIFIABLE_PATH_PREFIX)


def sensitive_path_refusal(path_str: str, base_dir: str | None = None) -> str | None:
    """The path tier of the tool gate: the refusal for *path_str*, or ``None``.

    Reason-or-``None`` like the other tiers (``is_sensitive_bash_command``,
    ``audit_bash_exfiltration``, ``is_denied``), so ``hooks.on_tool_call`` applies
    it the same way.

    The ONE decision: :func:`is_sensitive_path` is ``refusal is not None``. A path
    whose canonical form (or whose anchors) could not be established within the
    resolve budget is refused exactly as a match is, fail-closed, and this function
    is never a way to let one through -- but the two refusals get different WORDS.
    A match is ``Blocked: access to sensitive path: <path>``. A stall opens with
    :data:`UNVERIFIABLE_PATH_PREFIX`, says the path is NOT a match, and quotes the
    path LAST: a stall reported as a match leads the agent reading it to conclude,
    reasonably and wrongly, that an ordinary project file holds a credential, that
    the session has been locked down, or that a different spelling might pass, and
    each of those costs a wasted round where "could not verify within budget, retry
    shortly" costs one wait.
    """
    try:
        matched = _path_in_home_dirs(
            path_str, _SENSITIVE_HOME_DIRS, base_dir, strict=True
        ) or _is_keystone_publish_artifact(path_str, base_dir, strict=True)
    except PathResolutionStalled:
        return (
            f"{UNVERIFIABLE_PATH_PREFIX} (symlink resolution did not complete in time), "
            "so it is refused fail-closed. This is NOT a match: the path is not known to "
            "be sensitive. Retry the same call after a short wait; do not re-spell it. "
            f"Path: {path_str!r}"
        )
    if matched:
        return f"Blocked: access to sensitive path: {path_str}"
    return None


def path_contains_sensitive(dir_str: str, base_dir: str | None = None) -> bool:
    """Return True if a read+write-sensitive location lies UNDER *dir_str*.

    The REVERSE direction of :func:`is_sensitive_path`: that gate answers "is
    this path inside a protected location?", this one answers "does this
    directory CONTAIN one?". A bulk operation rooted at *dir_str* — e.g. the
    Notes builtin's ``git add -A`` over an attached vault — sweeps every file
    below the root, so a root that is an ANCESTOR of a credential store (the
    home directory itself, or a parent of ``~/.ssh``) would stage and push the
    credentials wholesale even though the root is not itself a sensitive path.

    List-based, no filesystem walk: the known sensitive roots
    (:data:`_SENSITIVE_HOME_DIRS`, including the crew data-home secret leaves
    and any ``KIROCREW_HOME`` re-anchoring) are prefix-compared against the
    directory's candidate forms, so the check is O(sensitive entries) even when
    *dir_str* is a huge tree. Shares :func:`_candidate_forms` and
    :func:`_home_dir_targets` with :func:`_path_in_home_dirs` so the
    symlink/casefold hardening cannot drift between the two directions.
    """
    if not dir_str:
        return False
    try:
        sensitive_targets = _home_dir_targets(_SENSITIVE_HOME_DIRS)
        candidates = _candidate_forms(dir_str, base_dir)
    except PathResolutionStalled:
        return True  # fail closed: see _path_in_home_dirs
    for cand in candidates:
        # Normalize away a trailing separator so `/home/u/` and `/home/u`
        # produce the same prefix (a bare `/` or `C:\` root rstrips to ""/"C:",
        # whose prefix form still matches everything under it — correct: every
        # sensitive path is inside the filesystem root).
        cand_cf = cand.casefold().rstrip(os.sep)
        prefix = cand_cf + os.sep
        for target in sensitive_targets:
            # Equality (the dir IS the sensitive path) is is_sensitive_path's
            # job, but including it here fails safe for callers using only this
            # gate.
            if target == cand_cf or target.startswith(prefix):
                return True
    return False


def is_sensitive_write_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path must not be MODIFIED by an agent tool.

    Superset of :func:`is_sensitive_path`: everything that is read+write blocked
    PLUS the write-only-protected runtime config files
    (:data:`_WRITE_PROTECTED_HOME_PATHS`), which stay readable but must not be
    written by the agent. Enforced at the file-edit tool gate
    (``hooks.on_tool_call`` on the ACP ``edit`` kind) — see
    :data:`_WRITE_PROTECTED_HOME_PATHS` for the rationale.

    The publish-artifact clause is repeated from :func:`is_sensitive_path` rather than
    left to be inherited, because this gate is documented as a SUPERSET of it: omitting
    it here would leave a keystone temp writable through the edit gate while the
    read+write gate refused it, the same one-path-only hole the pairing notes above warn
    about.
    """
    return _path_in_home_dirs(
        path_str, _SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS, base_dir
    ) or _is_keystone_publish_artifact(path_str, base_dir)


def sensitive_home_dirs() -> tuple[str, ...]:
    """Public, read-only view of the read+write-blocked home-relative paths.

    Lets the security-posture surface (``security_posture.py``) enumerate what
    :func:`is_sensitive_path` actually blocks without coupling to the private
    ``_SENSITIVE_HOME_DIRS`` name — the same rationale as
    :func:`get_credential_patterns`. Returned as a tuple so a caller cannot
    mutate the live blocklist.
    """
    return tuple(_SENSITIVE_HOME_DIRS)


def write_protected_home_paths() -> tuple[str, ...]:
    """Public, read-only view of the write-only-protected home-relative paths.

    Companion to :func:`sensitive_home_dirs` — these stay readable but must not
    be written by an agent tool.
    """
    return tuple(_WRITE_PROTECTED_HOME_PATHS)


def crew_home_prefixes() -> tuple[str, ...]:
    """Public view of the known crew data-home prefixes.

    Used to classify a sensitive path as a Kiro Crew trust root vs. a third-party
    credential store when describing the posture.
    """
    return tuple(_CREW_HOME_PREFIXES)


def sandbox_credential_targets(exclude_leaves: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Absolute, on-disk-case credential targets for an OS sandbox deny list.

    Applies the SAME anchoring as :func:`_home_dir_targets_uncached` -- the
    ``$HOME`` projection of every :data:`_SENSITIVE_HOME_DIRS` leaf, plus each
    env-override re-anchor -- so a caller building a sandbox mask inherits the
    read gate's anchor rules instead of re-deriving them. That is the whole point
    of this living here: a mask that projected leaves under ``Path.home()`` only
    would silently miss a credential the operator relocated with
    ``KIROCREW_HOME``, ``CLAUDE_CONFIG_DIR`` or ``CLAUDE_HOME``, which is exactly
    the drift a hand-maintained list already produced once.

    Unlike the read gate's target set the paths are NOT casefolded: that set
    exists to COMPARE against candidate paths, while these are handed to a
    sandbox backend to deny on disk, and a casefolded path denies nothing on a
    case-sensitive filesystem.

    *exclude_leaves* drops a ``$HOME``-relative leaf (and its override
    re-anchors) from the result -- for an adapter whose own OAuth token it must
    still be able to read in order to authenticate. Excluding a leaf here only
    removes it from THIS mask; the read gate still fences it for the agent's own
    file tools, so the two controls keep covering different readers.

    Returns logical paths. The launcher ``os.path.abspath``es what it is handed,
    and the macOS profile emits both a ``subpath`` and a ``literal`` deny for each
    entry, so both a directory and a plain file leaf are valid entries.
    """
    excluded = set(exclude_leaves)
    leaves = [d for d in _SENSITIVE_HOME_DIRS if d not in excluded]
    # Resolved INLINE, not through _resolved_root_key: that one is bounded for
    # the event loop and RAISES on a stall, and this runs off the loop already
    # (``_sandbox_preflight`` wraps it in ``asyncio.to_thread``). A sandbox mask
    # must be canonical whatever the disk is doing, so the worker that resolves
    # the roots for the gate is called here directly and waits; the spawn side
    # bounds that wait (``_run_preflight_bounded``, 60 s) and refuses the
    # adapter on expiry rather than starting it unmasked (found in review).
    resolved = _resolve_root_anchors(str(Path.home()))
    # BOTH home spellings, reusing the two anchors the read gate already keys on.
    # On a host whose home is itself a symlink (``/home/u`` -> ``/local/home/u``) the
    # resolved and logical spellings differ, and the read gate can absorb that because
    # it realpaths a candidate BEFORE comparing. A sandbox deny list gets no such
    # normalisation -- it denies the paths it is handed -- so denying only the resolved
    # form would leave every credential reachable through the symlinked one.
    home_anchors = {resolved.home, resolved.logical_home}
    targets: set[str] = {
        os.path.join(anchor, *_leaf_segments(d)) for anchor in home_anchors for d in leaves
    }
    # KIROCREW_HOME: the crew secrets (signing keys, governance ceiling, .env)
    # live directly under the override, not under either default crew prefix.
    if resolved.crew_home:
        for d in leaves:
            for prefix in _CREW_HOME_PREFIXES:
                if d == prefix or d.startswith(prefix + "/"):
                    leaf = d[len(prefix) :].lstrip("/")
                    targets.add(
                        os.path.join(resolved.crew_home, *_leaf_segments(leaf))
                        if leaf
                        else resolved.crew_home
                    )
                    break
    # An adapter's credential store follows that adapter's own home override.
    adapter_roots = dict(resolved.adapter_roots)
    for leaf, root_envs, under_root in _OVERRIDE_ANCHORED_LEAVES:
        if leaf in excluded or leaf not in _SENSITIVE_HOME_DIRS:
            continue
        for env in root_envs:
            root = adapter_roots.get(env)
            if root:
                targets.add(os.path.join(root, *_leaf_segments(under_root)))
    return tuple(sorted(targets))


# ---------------------------------------------------------------------------
# Layer two: the command-line orchestrator
# ---------------------------------------------------------------------------
# Everything above is layer one: declarations, predicates, the bounded resolver and
# the public path gates, reading nothing in this package but the shell reader one
# layer below. The gate below composes the tiers that sit ABOVE this module -- the
# egress tier and the rules catalog -- each of which load-imports layer one. That is
# why the references to them are call-time imports inside the body that needs them
# and never module-level: a module-level import of either would close a load-time
# cycle, and the alternative, handing them in as parameters, would put the tier
# list in a public signature.


def is_sensitive_bash_command(
    command: str,
    *,
    enabled_ids: "frozenset[str] | None" = None,
) -> str | None:
    """Refuse a bash command that reaches IMDS or leaks environment credentials.

    The subject is a SHELL COMMAND LINE. The two detectors read it with shell grammar
    -- separator runs are redundant, newlines and ``|`` split pipeline stages, an
    ``env | grep`` pipeline is one command -- and none of that holds for a Python
    source file. A caller with a source body in hand must not route it here: every
    shell pass over a source body produces a class of false denial on ordinary
    scripts. The cron script gate (``mcp_cron._vet_script_contents``)
    runs only full-text detectors that are meaningful on source, and the sandbox is the
    runtime control for what a script may open.

    This gate does NOT match PATHS in command text. Sensitive paths are enforced where
    the spelling of a command cannot talk around them: the OS sandbox hides the
    credential stores (``~/.aws``, ``~/.gnupg``, SSH keys) from the agent's process
    tree and mounts the governance keystone (``security_policy.json``, ``profiles/``,
    ``admission_policy.json``, ``computer_use.json``) read-only in every mode, and
    :func:`is_sensitive_path` refuses every resolved path the file tools open. A text
    matcher over ``cat ~/.aws/credentials`` adds no protection on top of that and
    denies ordinary read-only commands whenever a fenced spelling appears as data
    (a grep pattern, a commit message, a note), so no such matcher runs here; a
    keystone READ through the shell is permitted by design.

    Three checks, in order:

    * **Pass 0, size ceiling.** A subject longer than
      :data:`MAX_SCANNABLE_COMMAND_CHARS` is refused unscanned.
    * **IMDS access** (``exfil._check_imds_access``) via any IP encoding.
    * **Environment credential exfiltration** (``denied_rules._check_env_credential_access``):
      ``declare -p``, ``env | grep``, ``printenv`` and their kin.

    Returns denial reason string, or None if clean.
    """
    # Call-time imports, layer two. Both tiers load-import this module for the keystone
    # declarations, so a module-level import here would close a load-time cycle; handing
    # them in as parameters would put the tier list in a public signature instead.
    # Resolving them per call also keeps a patch applied to the package attribute
    # observable here, which a name bound at import time would not be.
    from .denied_rules import _check_env_credential_access
    from .exfil import _check_imds_access

    # ── Pass 0: size ceiling ──
    # Both detectors below are linear in the subject, and this bound is what makes
    # that a wall-clock ceiling: the gate runs synchronously on the event loop,
    # so its worst case IS the loop's worst case. An oversized subject is
    # refused, never scanned partially and never let through unscanned -- a
    # denied long command is recoverable by the operator, a stalled gateway and
    # an unscanned command are not.
    #
    # The diagnostic is built HERE rather than inside the refusal producer, which
    # both entry points share and which is handed two numbers rather than the
    # subject. Its span is the whole subject because nothing matched: this refusal
    # is "not scanned", and pointing at a region would name a match that was never
    # attempted. The census behind it is bounded for the same reason the scan is.
    if len(command) > MAX_SCANNABLE_COMMAND_CHARS:
        return annotate_refusal(
            _oversize_refusal(len(command), MAX_SCANNABLE_COMMAND_CHARS),
            refusal_diagnostic("keystone-scan-ceiling", "size-ceiling", command),
        )

    # IMDS access via any IP encoding (decimal, hex, octal, IPv6-mapped)
    imds_result = _check_imds_access(command, enabled_ids=enabled_ids)
    if imds_result:
        return imds_result

    # Environment credential exfiltration (declare -p, env|grep, printenv, etc.)
    env_result = _check_env_credential_access(command)
    if env_result:
        return env_result
    return None
