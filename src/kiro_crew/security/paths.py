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


def _leaf_basename(spec: str) -> str:
    """Final segment of a ``/``-authored leaf spec."""
    return _leaf_segments(spec)[-1]


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
    # aim at either. Only the human-driven dashboard cutover writes it, and the
    # gateway's own startup reader opens it directly rather than through this
    # gate, so both keep working.
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
    # the shared gate. The authenticated dashboard ``/api/aws/consent`` handler
    # and the ``kirocrew aws-consent`` CLI are the only writers and open the
    # path directly, not through this gate, so both keep working.
    "aws_service_consent.json",
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
    # Named memory stores. Each subdirectory is ONE crew's private memory silo --
    # its markdown tree, its FTS index and its vector-store SQLite file -- and the
    # whole point of a named store is that a crew reaches only its own. Agent file
    # tools run as the same UID as every store on disk, so owner-only modes decide
    # nothing here: without this entry any crew's agent could read another crew's
    # preferences and lessons straight off disk, or rewrite them, which is the
    # boundary the split exists to draw. Read AND write, because reading another
    # crew's memory is the primary harm and writing it is steering that crew's
    # future turns.
    #
    # A DIRECTORY entry, for the reason ``routing`` and ``webhooks`` above are:
    # markdown files are published through ``atomic_write``'s ``mkstemp`` sibling,
    # so fencing final names only would leave a writable path to the same bytes
    # under a random temp name.
    #
    # DELIBERATE ASYMMETRY, do not "tidy" it: the DEFAULT store's own ``memory.db``
    # and ``workspace/memory/`` stay readable, because that is the agent's own
    # memory and reading it is the product working. Fencing them would be a
    # default-path behaviour change, which the coexistence constraint forbids. So
    # ``is_sensitive_path(<home>/memory.db)`` is False and
    # ``is_sensitive_path(<home>/memory_stores/work/memory.db)`` is True, on
    # purpose. Full reasoning: docs/system-specs/modules/security.md.
    #
    # Every legitimate reader opens a store path DIRECTLY rather than through this
    # gate -- the established keystone-reader pattern -- so the memory subsystem is
    # unaffected.
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
    for leaf in ("config.json", "config.local.json", "playwright-cli-config.json")
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
    """The path prefix a stall is charged to: the first two components.

    A wedged mount stalls everything beneath its mount point, and mount points
    sit at depth one or two (``/home/<user>`` autofs, ``/Volumes/<share>``,
    ``/net/<host>``, ``C:\\Users``), so two components is the narrowest key
    that still covers the whole stalled subtree.  Scoping the cooldown here is
    what keeps a stall on the REMOTE half of an ``ssh`` command from switching
    resolution off for the local workspace where a bypass symlink would live.
    """
    normalized = os.path.normpath(expanded)
    parts = normalized.split(os.sep)
    keep = 3 if parts and parts[0] == "" else 2  # leading "" for an absolute path
    return os.sep.join(parts[:keep]) or normalized


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

    Only a resolution that actually timed out is recorded.  A refusal issued
    because every worker was already pinned costs nothing (nothing is
    submitted) and must not charge the refused prefix -- often the local
    workspace -- a backoff it never earned, or a transient dual-mount outage
    would keep refusing healthy paths for the accrued window after the mounts
    recover.  The log line deliberately omits the path: the token is
    agent-supplied and is what the gates exist to keep out of clear-text logs.
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
    expanded: str, worker: Callable[[str], _ResolvedT]
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
    than ``_PATH_RESOLVE_TIMEOUT_SECS``.

    The UNC shortcut is NOT here: skipping a ``\\\\server\\share`` token is a
    stance about agent-supplied CANDIDATES (:func:`_resolved_forms_bounded`),
    whose fence targets a UNC realpath never produces.  The anchors are the
    fence itself, and a UNC home with a junction inside ``KIROCREW_HOME`` must
    still be canonicalised or a canonical-spelling request would miss the
    governance file (found in review); the bound makes that probe safe.
    """
    budget = _PATH_RESOLVE_TIMEOUT_SECS
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
    try:
        started = threading.Event()
        worker_tid: list[int] = []

        @functools.wraps(worker)
        def _tracked(arg: str) -> _ResolvedT:
            worker_tid.append(threading.get_native_id())
            started.set()
            return worker(arg)

        future = path_resolve_executor().submit(_tracked, expanded)
    except RuntimeError:
        # Pool already shut down (interpreter exit).  Lexical forms only.
        return None
    try:
        value = future.result(timeout=budget)
    except FutureTimeoutError:
        if not started.is_set() and future.cancel():
            # Only a future proven dead by cancel() may go untracked: one that refuses
            # cancellation is running, and would pin a worker _wedged_workers() cannot see.
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
        logger.debug(
            "sensitive-path resolution timed out blocked in the filesystem (tid=%s)",
            tid,
        )
        _mark_stalled(prefix, budget)
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


def _candidate_forms(path_str: str, base_dir: str | None = None) -> set[str]:
    """Expand *path_str* into every candidate form the sensitive-path gates match.

    Symlink-resolved forms defeat a link bypass; the lexical forms are the
    fail-safe fallback when resolution cannot complete (over-matching a
    sensitive-looking path is the safe direction). ``base_dir`` anchors a
    relative input against the caller's known working directory. Shared by
    :func:`_path_in_home_dirs` (is the path INSIDE a protected location?) and
    :func:`path_contains_sensitive` (does the path CONTAIN one?) so the
    symlink/anchoring hardening cannot drift between the two directions.
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
    candidates: set[str] = _resolved_forms_bounded(expanded)
    candidates.add(os.path.normpath(expanded))
    candidates.add(expanded)
    return candidates


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
        os_home_real = _realpath_or_none(os_home) or os_home
        if os_home_real.casefold() != os_home.casefold():
            for d in home_dirs:
                sensitive_targets |= _anchor_both_separators(os_home_real, d)
    # ``home`` arrives RESOLVED from the cache key, so this is normally a no-op;
    # it still opens the directory on Windows, which is why the whole rebuild
    # runs off the loop.  None degrades to the lexical anchors already in the set.
    home_real = _realpath_or_none(home) or home
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
                    full_real = _realpath_or_none(full)
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
        agents_real = _realpath_or_none(agents_full)
        if agents_real is not None:
            sensitive_targets.add(agents_real.casefold())
    # An ACP adapter's OAuth token follows that adapter's own home override, so
    # the ``$HOME``-rooted entry anchored above covers only the documented
    # default. Re-anchor the token leaf under each override the adapter honours
    # (the default form stays, so every location is always covered). Guarded on
    # membership in *home_dirs* for the same reason as the agents dir above: a
    # write-tier build must not gain a read-tier target.
    _adapter_roots = dict(resolved.adapter_roots)
    for _leaf, _root_envs in _OVERRIDE_ANCHORED_LEAVES:
        if _leaf not in home_dirs:
            continue
        _basename = _leaf_basename(_leaf)
        for _env in _root_envs:
            _root = _adapter_roots.get(_env)
            if not _root:
                continue
            _full = os.path.join(_root, _basename)
            sensitive_targets.add(_full.casefold())
            _full_real = _realpath_or_none(_full)
            if _full_real is not None:
                sensitive_targets.add(_full_real.casefold())
    return sensitive_targets


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
# fails OPEN. A few seconds bounds that window.
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
# The value is 0.1s, NOT a "few seconds", because a skills walk issues thousands
# of calls in a burst and one build serves the whole burst either way. Measured
# cold-walk cost against this constant:
#     5.0s -> 0.95s    1.0s -> 0.93s    0.1s -> 0.95s    0.0s -> 4.66s
# So 0.1s keeps the entire win while cutting the stale window 50x versus 5.0s.
# Only 0.0 (no cache) closes the window completely, and that reverts to the 4.7s
# scan whose GIL-held cost wedges the event loop — the defect this exists to fix.
_HOME_TARGETS_TTL_SECS = 0.1
# key -> (expiry_monotonic, targets)
_home_targets_cache: dict[tuple[object, ...], tuple[float, set[str]]] = {}


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
    # ``_OVERRIDE_ANCHORED_LEAVES``, which maps one leaf to the roots its parent
    # can move to. Without it the relocated tree sits at a path no matcher
    # covers, so an agent inside a pod could read the operator's identity token
    # at the pod-path spelling while the identical bytes at ``~/.aws`` are
    # refused.
    os_home: str | None


#: Sensitive leaf -> the ``$HOME``-override VARIABLES its parent can be moved by.
#:
#: PROJECTED from the harness declarations, not enumerated: the pairing has to
#: name the same leaf the list above fences and the same variable the resolver
#: below reads, and it was a third hand-maintained copy of both. A leaf absent
#: from the *home_dirs* list being built is skipped, so a write-tier build never
#: leaks a read-tier target.
#:
#: Read once at import, like the leaf list itself: the declarations are static
#: data, and re-projecting per gate call would put a table walk on the hot path.
_OVERRIDE_ANCHORED_LEAVES: tuple[tuple[str, tuple[str, ...]], ...] = (
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


def _home_dir_targets(home_dirs: list[str]) -> set[str]:
    """TTL-cached :func:`_home_dir_targets_uncached`.

    Keyed on the *home_dirs* list plus the RESOLVED home and crew-home roots
    (see the note above the constant for why the raw env vars are not enough).

    ponytail: the returned set is the cached instance, not a copy — both
    callers only iterate it. A future caller that MUTATES the result would
    poison the cache for every other caller; copy here if that ever happens.
    """
    # Resolve the roots ONCE and use the same tuple for both the key and the
    # build. Resolving separately lets a root symlink repointed between the two
    # reads file one root's targets under the other root's key — a fail-OPEN
    # TOCTOU, pinned by the regression test
    # test_roots_are_resolved_once_for_key_and_build.
    roots = _resolved_root_key()
    key = (tuple(home_dirs),) + roots
    now = time.monotonic()
    cached = _home_targets_cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    targets = _rebuild_targets_bounded(home_dirs, roots)
    # Bound the dict: the key space is tiny (two constant home_dirs lists ×
    # roots), but a test or embedder that churns KIROCREW_HOME must not grow it
    # without limit.
    if len(_home_targets_cache) > 32:
        _home_targets_cache.clear()
    _home_targets_cache[key] = (now + _HOME_TARGETS_TTL_SECS, targets)
    return targets


def _rebuild_targets_bounded(home_dirs: list[str], roots: _ResolvedRoots) -> set[str]:
    """Rebuild the target set on the ``mc-pathres`` pool.

    The anchors -- ``$HOME``, the ``KIROCREW_HOME`` / ``KIRO_HOME`` / adapter
    override roots and the ~40 keystone leaves under them -- are the paths the
    sensitive-target set is built FROM, as opposed to the agent-supplied
    candidate checked AGAINST it.  They are deliberately NOT ``realpath``'d
    inline on the event loop every time the 0.1s cache expires: on a Windows
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

    A rebuild that does not complete canonically within the budget RAISES, and
    every gate turns that into a refusal -- the same invariant as
    :func:`_resolved_root_key` (see the comment there for the three weaker
    fallbacks review found open: lexical spellings, a UNC skip, and serving the
    previous canonical set).  A pool fault is treated exactly like a stall, and
    a UNC home is probed here too (bounded): the candidate-side UNC shortcut is
    about tokens, not about the fence.  The stall is recorded, so the rebuild
    does not re-probe the wedged mount every 0.1s -- it refuses without touching
    the filesystem until the cooldown lapses.
    """
    try:
        targets = _run_resolution_bounded(
            roots.home, lambda _home: _home_dir_targets_uncached(home_dirs, roots)
        )
    except PathResolutionStalled:
        targets = None
    if targets is None:
        raise PathResolutionStalled(roots.home, _stall_prefix(roots.home))
    return targets


def _path_in_home_dirs(path_str: str, home_dirs: list[str], base_dir: str | None = None) -> bool:
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
    """
    if not path_str:
        return False

    try:
        candidates = _candidate_forms(path_str, base_dir)
        # The anchors are bounded the same way (see _rebuild_targets_bounded):
        # a stall with no prior canonical resolution to serve refuses too.
        sensitive_targets = _home_dir_targets(home_dirs)
    except PathResolutionStalled:
        # Canonical form unavailable (wedged mount under the path): refuse.  A
        # lexical-only match here would pass a workspace symlink into a
        # credential store for the length of the stall.
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


def _is_keystone_publish_artifact(path_str: str, base_dir: str | None = None) -> bool:
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
        artifact_parents = _home_dir_targets(_KEYSTONE_ARTIFACT_PARENTS)
        candidates = _candidate_forms(path_str, base_dir)
    except PathResolutionStalled:
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
    """
    return _path_in_home_dirs(
        path_str, _SENSITIVE_HOME_DIRS, base_dir
    ) or _is_keystone_publish_artifact(path_str, base_dir)


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
    for leaf, root_envs in _OVERRIDE_ANCHORED_LEAVES:
        if leaf in excluded or leaf not in _SENSITIVE_HOME_DIRS:
            continue
        basename = _leaf_basename(leaf)
        for env in root_envs:
            root = adapter_roots.get(env)
            if root:
                targets.add(os.path.join(root, basename))
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
