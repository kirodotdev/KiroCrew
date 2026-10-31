"""Keystone declarations: the paths the agent's file tools must never read, and never write.

The sensitive home directories, the crew data home's secret leaves (expanded under every
crew-home prefix), the keystone publish-artifact suffixes and parents, and the
write-protected tier with its kiro-cli leaves. The comments on the declarations carry
more weight than the code does: each one records why an entry sits on the read-plus-write
floor rather than the write-only tier, which is not recoverable from the list itself.
Read them before adding, moving or removing an entry. A new fenced path goes here.

Declarations only: nothing here resolves a path or decides a verdict.
``kiro_crew.security.paths`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`); the target builder and the path
gates read these lists, and this module imports no other part of the path floor.
"""

from __future__ import annotations

from kiro_crew.agent_sdk import host_auth, native_allow_stores
from kiro_crew.identity_stores import AUTH_SQLITE_DB, AUTH_SQLITE_SIDECAR_SUFFIXES, fenced_home_dirs
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME

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
    # (sandbox_tiers.py) is SEPARATE, so kiro-cli's own auth is unaffected.
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
    # Web Push: the VAPID private key and every device's push secret, enough to raise
    # OS banners on the owner's devices. Opened directly by the notification subsystem.
    "web-push",
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
    "vouched-executions",  # member-store vouches; also sandbox-masked
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
    # KAS engine bundles the gateway installs then executes (``acp.kas_bundle``);
    # same class as ``playwright-cli``: READONLY in-sandbox, fenced from file tools.
    "engines",
    # Legacy SEL HMAC key location (pre-``trust/`` installs, and any stale file
    # a backup restore resurrects). Kept alongside the ``trust``
    # directory entry below so the key is gated at BOTH locations.
    "sel_hmac.key",
    # SEL trust-root directory: sel_trust_root.py stores/migrates the audit chain's HMAC
    # signing key at ``trust/sel_hmac.key`` — OUTSIDE the log's directory, so
    # write access to the log dir does not imply re-signing power. The whole
    # dir is gated (like ``profiles``/``run``) so future trust-root material is
    # covered without a new entry. sel_trust_root.py opens the key directly, not through
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
    "template-mcp-additions",  # approved MCP entries' env/headers; as the bindings above
    "security_events.jsonl",
    # Rotated SEL segments. sel_rotation.py closes the live log at a size cap and renames
    # it into this directory, so a segment holds exactly the same audit records
    # the live file does and must be gated identically — a rotated log that the
    # agent could read (or rewrite, then let the chain re-anchor from) would make
    # rotation itself the way around the fence. Directory entry, so every
    # segment is covered without a per-name matcher. sel_reader.py opens segments
    # directly, not through this gate.
    "security_events.d",
    "app_admission.json",
    "security_policy.json",
    "profiles",
    # The centrally-distributed ceiling's last-known-good cache
    # (``platform/policy_distribution_cache.py``). Gated as a DIRECTORY, and the reason
    # is not just that it holds a copy of the policy: the sidecar metadata records
    # the SOURCE the copy came from, and the loader honours that source when
    # deciding whether the cache is this host's last-known-good. An agent able to
    # write here could publish itself a ceiling AND the provenance to make the next
    # boot adopt it — a full escape from ``security_policy.json`` being unwritable,
    # around the side. Read matters as much as write: the cache is a verbatim copy
    # of the policy the trust-root entry above exists to keep unreadable, so
    # leaving it open would hand over the exact deny patterns an evasion needs.
    # policy_distribution_cache.py opens both files directly, not through this gate.
    "policy_cache",
    "admission_policy.json",
    "denied_commands.json",
    # Operator grants of ``owner`` trust to hand-configured app registries, on the same floor
    # as ``denied_commands.json``: a writable grant clones a registry the agent controls.
    "registry_trust.json",
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
    "pinned-order",  # person-only sidebar order; mirror: sandbox._CREW_HIDDEN_LEAVES
    # Crewmate teams (``crew_teams.py``): the owner's grouping of the roster. Not
    # a secret, but it decides which team view a crewmate's questions and work
    # roll up into, and a crewmate must not be able to move itself or a sibling.
    # Whole directory (``atomic_write`` temp sibling); the OS-sandbox counterpart
    # is ``sandbox._CREW_HIDDEN_LEAVES``. Opened only by the gateway and by the
    # operator's own ``kirocrew agent create`` / ``delete``, never by an agent tool.
    "crew-teams",
    "member-records",  # crewmate rules/goals/DM bindings; mirror: sandbox._CREW_HIDDEN_LEAVES
    # Subagent panel dismissals (``subagent_persistence.record_panel_dismissal``):
    # one file per run whose finished card the operator dismissed, which the
    # panel's durable reader consults to keep that card hidden. It decides what
    # the OPERATOR sees, so the run it is about -- and any other sandboxed
    # process -- must be able to neither forge one (hiding a run nobody
    # dismissed) nor delete one (resurrecting a card the operator cleared).
    # Whole directory (``atomic_write`` temp sibling); the OS-sandbox counterpart
    # is ``sandbox._CREW_HIDDEN_LEAVES``. Deliberately NOT under ``trust/``,
    # which stays sandbox read-write for SEL. Only the gateway opens it.
    "panel-dismissals",
    # The operator's OAuth consent-endpoint extension
    # ({additional_authorization_endpoints: [{host, path}]}). Each entry widens
    # the banner-only OAuth entropy carve-out (_OAUTH_AUTHORIZATION_ENDPOINTS),
    # so this is a trust boundary of the same class as ``denied_commands.json``
    # directly above: an agent that could WRITE it could exempt an
    # attacker-controlled host from the exfiltration heuristics — widening its
    # own trust ceiling — and one that could READ it would learn which extra
    # hosts are exempt and aim there. Read+write blocked on both the tool path
    # (``is_sensitive_path``) and the shell forms. The only legitimate reader
    # (``_load_operator_oauth_endpoints`` in ``exfil``) opens the file
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
    "ssh_auth_sock_consent.json",
    # The owner's credential-redaction switch. Same class of control as the
    # consent records above: the record is what AUTHORIZES the credential scrubber
    # to stand down in the owner's dashboard file viewer (the one surface it
    # governs), so an agent that could write it would switch off the pass that
    # keeps the secrets it can read out of that view. A missing or unreadable
    # file reads as ENABLED, so fencing the
    # write is what keeps the switch the owner's. The owner-gated dashboard
    # ``/api/security/credential-redaction`` handler is the only writer and opens
    # the path directly, not through this gate.
    "credential_redaction.json",
    "token_signing.key",
    "refresh_chains.json",
    # The staging directory the two leaves above publish through. Both are masked as
    # individual FILES in the data-home root, and a mask covers a PATH, so a temp staged
    # BESIDE either of them sits in that root under a name no mask covers -- readable in
    # any agent sandbox, and ``link(2)``-able by a same-uid agent during the write. The
    # temp holds the FULL signing key or chain state for the whole write, so that window
    # is a forged-token path, and a crash between write and publish leaves the same bytes
    # on disk indefinitely.
    #
    # Named as the whole DIRECTORY, like ``md-notebook-staging``, ``live-target-staging``
    # and ``aws-control-staging`` above: the ``startswith(target + os.sep)`` rule then
    # covers every temp name inside it, present and future. The keystone-artifact suffix
    # rule below does NOT reach these temps -- it covers a direct child of a keystone
    # leaf's own directory, and a staged temp sits one level below that -- so this entry
    # is what protects them, and it is the stronger of the two: it covers a leftover
    # whatever it is named, not only one ending in ``.tmp``.
    #
    # A TOP-LEVEL leaf, not a path under another directory, so no agent-writable ancestor
    # can be renamed out from under it. The gateway process is the only writer and it
    # opens these paths directly rather than through this gate, so publishing keeps
    # working.
    "auth-store-staging",
    # Mediated-secret owner-signed egress policy: writable, it would let an agent try
    # to authorize its own origin. Only the gateway and owner CLI open it, directly.
    "secret-request-policy",
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
    # agent sandbox: the sandbox launcher scripts (``sandbox_argv.py`` execs
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
    # KAS MCP launch snapshots hold servers' env values (``acp.kas_env_launch``); the
    # ``pi-gate`` split: readable in the sandbox for the launcher, fenced from file tools.
    "kas-mcp-launch",
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
# (hooks.on_tool_call), via ``is_sensitive_write_path``. That gate sees the
# file-edit tool only, and the loader's clamp bounds NUMBERS, not SWITCHES
# (``agent.sandbox: "off"``), so the load-bearing half is the OS sandbox, which
# mounts both files read-only (``sandbox._CREW_READONLY_LEAVES``). The operator
# edits config out-of-band (dashboard API / own-terminal CLI), outside both.
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
    # app-agent-launch: the empty directory a launch copy's kiro-cli starts in
    # (``apps.agent_launch.launch_process_cwd``); a spec written into it is the spec
    # kiro-cli runs. Read-only in the sandbox for the same reason.
    for leaf in (
        "config.json",
        "config.local.json",
        "playwright-cli-config.json",
        "subagents",
        "app-agent-launch",
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
    # Gateway-written inputs to decisions made outside the sandbox. gatewayd spawns a stubbed
    # server's backend as the user: ``mcp-launch-approvals`` records which launch each name may
    # run and ``mcp/resolved`` supplies the substituted executable. ``gateway_id`` is the id the
    # Remote Crew cycle guard compares; a write would choose whether a loop is detected. All
    # are written by the gateway, and reading them decides nothing inside the sandbox.
    f"{prefix}/{leaf}"
    for prefix in _CREW_HOME_PREFIXES
    for leaf in ("mcp-launch-approvals", "mcp/resolved", "gateway_id", "skill-projection")
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
    # The microvm lane's CREW RECORDS (``cloud/microvm/record.py``): the launch record's
    # reason above without the confirmation step it still has. Each row carries a
    # ``microvm_id`` that ``teardown`` passes straight to ``launcher.terminate``, so an
    # agent able to write this file could point one crew's row at another crew's VM and
    # have the owner's next delete destroy that one instead. Readable like the paths
    # above, and the gateway writes it outside the agent's file-edit gate.
    f"{prefix}/microvm_crews.json"
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
    # The redaction allow-list (``redaction-allow/hosts.json``,
    # ``security.redaction_allow``): the hosts a reader allowed long-query and
    # base64 links for. An agent that could write it could allow the host it
    # wants to send conversation data to. Readable, since it names hosts and no
    # secret; there is no legitimate agent WRITE -- the gateway's owner-only
    # ``/api/redaction/allowed-hosts`` routes write it directly. The directory
    # is also mounted read-only in the sandbox (``sandbox._CREW_READONLY_LEAVES``);
    # this entry covers the file-edit tool on a host with no OS sandbox.
    f"{prefix}/redaction-allow"
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
    # are overwritten with Crew's seed and unlinked on reset. The digest check cannot
    # defend it, because the forger reads the same bytes Crew's own writer does.
    #
    # WRITE-protected, not read+write sensitive: the record holds no secret (it
    # names work dirs and digests), so the file-READ tools keep working and an
    # operator can still see why a seed was or was not adopted. There is no
    # legitimate agent WRITE at all — ``seed_provenance.record`` writes through
    # ``atomic_write``, outside this gate. Both the record's directory and its old
    # top-level name (read by a process started before the upgrade) are protected.
    f"{prefix}/{leaf}"
    for prefix in _CREW_HOME_PREFIXES
    for leaf in ("seed-provenance", "settings_seeds.json")
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
_WRITE_PROTECTED_HOME_PATHS += [
    # The Meetings app's per-meeting user notes and their pasted images: free text
    # the user cannot regenerate, kept outside the meeting directory every meeting
    # agent may ``fs_write``. Write-only on purpose: the dashboard serves the images
    # through ``/api/file-raw``, which applies the READ floor. The app backend opens
    # these files directly and never routes through this gate.
    f"{prefix}/apps/meetings/data/notes"
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


# The crewmate folder (``config.paths.mate_agents_dir()``): crewmate and mate service
# specs in the agents-dir format, fenced like it. Only gateway handlers write it, directly.
_KIRO_MATE_AGENTS_DIR = ".kiro/mates/agents"
_WRITE_PROTECTED_HOME_PATHS += [_KIRO_MATE_AGENTS_DIR]
# Its siblings: who owns each crewmate file, revoked approvals, the tool-access
# record and the migration ledger. Only gateway handlers write them.
_KIRO_MATE_STATE_LEAVES: tuple[str, ...] = (
    ".kiro/mates/owners.json",
    ".kiro/mates/revoked-approvals.json",
    ".kiro/mates/tool-access.json",
    ".kiro/mates/migrated.json",
)
_WRITE_PROTECTED_HOME_PATHS += list(_KIRO_MATE_STATE_LEAVES)


# ── kiro-cli global MCP registry (~/.kiro/settings/mcp.json) ──
# The sibling of the agents dir above, and an INPUT TO A SECURITY DECISION by the
# same route: an ``autoApprove`` on an entry here is honoured by default
# (``mcp.honour_auto_approve``), and kiro-cli approves an autoApproved MCP tool
# LOCALLY and emits no permission request -- so those verbs skip
# ``hooks.on_tool_call`` and the tool gate never runs for them.
# ``governance._is_owner_written`` admits an entry on its NAME SHAPE (no ``:``, no
# provenance marker), never on who wrote the file, so a plainly-named entry an agent
# appended is indistinguishable from one the owner typed. The opt-in's own wording is
# what this entry makes true: it respects a list "hand-added to ``mcp.json``" -- by a
# HAND.
#
# WRITE-protection, NOT read+write sensitive, as for the agents dir: the registry
# must stay READABLE (the app MCP-policy merge reads it, ``kirocrew doctor`` reports
# it, and the deregistration scrub has to find an older build's leaked entries).
# Kiro Crew's own writers take the advisory lock and ``atomic_write`` the path
# directly without routing through this gate, so app registration, health-check
# promotion and that scrub keep working; only the agent's file-edit tool is refused.
#
# A LEAF, not the ``settings`` directory: that directory also holds
# ``amazon-internal.json``, whose ``sandbox`` key ``sandbox.py`` reads, and fencing a
# whole kiro-cli-owned directory on this file's account would claim a protection this
# entry has not reasoned about.
#
# A literal, like the agents dir, to avoid a config->security import cycle, and
# deliberately not resolved through a data-home helper:
# ``config.paths.shared_kiro_settings_writable`` records that this file belongs to the
# kiro-cli installation and resolves from the REAL home. ``KIRO_HOME`` moves kiro-cli's
# whole user directory INCLUDING ``settings`` (see ``kiro_home``'s scope caveat), so
# that location is re-anchored in ``_home_dir_targets_uncached``.
_KIRO_SETTINGS_MCP_JSON = ".kiro/settings/mcp.json"


_WRITE_PROTECTED_HOME_PATHS += [_KIRO_SETTINGS_MCP_JSON]


#: The ``KIRO_HOME``-relative entries on the write-only tier, as ONE list because
#: they share an anchoring rule rather than an owner: each is re-anchored under
#: ``KIRO_HOME``, and like every entry each has its ``$HOME``-rooted form
#: resolved, so a symlink anywhere below the home root cannot move the real file
#: out from under the fence. The ``KIRO_HOME`` re-anchoring is driven from this
#: tuple and the ``$HOME`` half from the entries themselves, so a third kiro-cli
#: leaf joins both by landing here and in the write tier.
_KIRO_CLI_WRITE_TIER_LEAVES: tuple[str, ...] = (
    _KIRO_AGENTS_DIR,
    _KIRO_SETTINGS_MCP_JSON,
    _KIRO_MATE_AGENTS_DIR,
    *_KIRO_MATE_STATE_LEAVES,
)


# Other harnesses' own permission files: the same threat, anchored like the leaves above.
_NATIVE_ALLOW_STORE_LEAVES: tuple[str, ...] = native_allow_stores.home_leaves()
_WRITE_PROTECTED_HOME_PATHS += list(_NATIVE_ALLOW_STORE_LEAVES)
_native_store_write = native_allow_stores.matches_store  # a project's own harness config

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
