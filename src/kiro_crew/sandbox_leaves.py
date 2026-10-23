"""The disposition of every crew-home leaf under the OS sandbox.

Each leaf of the data home is ``HIDDEN`` (masked in every mode), ``READONLY`` (readable,
never writable, so the agent cannot choose its own ceiling) or ``VISIBLE`` (in-sandbox
code needs read and write); a foreign harness's child is additionally withheld the
credential-bearing leaves. One of the owners ``kiro_crew.sandbox`` is carved into:
every name here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import os

from kiro_crew.identity_stores import AUTH_SQLITE_DB, AUTH_SQLITE_SIDECAR_SUFFIXES

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

#: The live-target pointer's file name under the crew data home.
#:
#: The pointer names the checkout the gateway execs into at startup, so it is masked from
#: EVERY sandboxed process — including Dev Fleet's own backend, whose cutover runs in the gateway.
#: That backend is a sandboxed spawn whose ``npm ci`` / build children run inside the same
#: namespace (a nested sandbox is denied by design), so any leaf it could reach, a
#: worktree's lifecycle script could reach too. The cutover therefore runs in the gateway
#: process (``dev_fleet/gateway_routes.py``) and the mask here stays whole; what this
#: module adds is a MOUNT TARGET for it (:func:`_materialize_live_target_mask_target`), so
#: the mask is never vacuous on a host that has not pinned a target yet. Spelled here
#: rather than imported from ``service.live_target`` to keep this low-level module free of
#: that import chain; ``test_sandbox_dev_fleet_live_target.py`` pins the spellings equal.
_LIVE_TARGET_LEAF: str = "live_target.json"
#: The masked DIRECTORY the pointer's absent-equivalent stub is staged in before it is
#: linked into place. Staging beside the target — in the data-home root — would put the
#: temp at a name every sandbox can see: a concurrent namespace could ``link(2)`` it and
#: keep a second, writable path to the inode the gateway later reads, and a bind mask
#: covers a PATH, not an inode. A hidden directory covers every name inside it, present
#: and future, so the temp is never visible; :func:`_materialize_live_target_mask_target`
#: additionally refuses any pointer whose link count is not exactly one. Same shape and
#: reason as ``_MD_NOTEBOOK_STAGING_LEAF`` / ``aws-control-staging``.
_LIVE_TARGET_STAGING_LEAF: str = "live-target-staging"

#: The masked DIRECTORY the gateway's own auth stores are staged in before they are
#: published. ``token_signing.key`` and ``refresh_chains.json`` are masked as individual
#: FILES, and a mask covers a PATH rather than an inode, so a temp staged BESIDE either of
#: them sits in the data-home root -- which is writable in-sandbox -- under a name no mask
#: covers. A same-uid agent that lists that root while a write is in flight can ``link(2)``
#: the temp and keep reading the bytes after the publish rename, and the spawn-time
#: :data:`_CREW_HARDLINK_REFUSED_LEAVES` check cannot see it: that check runs before a
#: spawn, while this window opens during one. A crash between write and publish leaves the
#: same unmasked file on disk holding the same bytes.
#:
#: A directory mask covers every name inside it, present and future, so both the staging
#: window and a crash orphan are covered. Same shape and reason as
#: :data:`_LIVE_TARGET_STAGING_LEAF`, :data:`_MD_NOTEBOOK_STAGING_LEAF` and
#: ``aws-control-staging``; the gateway process is the only writer, so unlike md-notebook it
#: needs no backend carve-out to hand the directory back to a sandboxed app.
_AUTH_STORE_STAGING_LEAF: str = "auth-store-staging"

#: The md-notebook builtin's name, and its own state files under the crew data home.
#: Named so the mask, the backend carve-out that lifts it, and the materialiser that
#: gives it a mount target cannot drift apart on a literal.
MD_NOTEBOOK_APP_NAME: str = "md-notebook"
#: The backend's state DIRECTORY, masked as a WHOLE DIRECTORY like ``whatsapp``. The three
#: state files live inside it, and every writer stages its temp inside it and renames onto
#: a name inside it. The mount point is the directory, so a rename of a name inside it
#: never crosses a mount (the cause of the ``EXDEV`` the old file-per-mount layout hit) and
#: never detaches the mask in a live agent's namespace. A directory mask covers every name
#: inside it, present and future, so the staging window and any crash orphan stay masked.
#:
#: A TOP-LEVEL leaf, for the same reason ``aws-control-staging`` is one: a mask covers the
#: leaf, not its ancestors, so a directory under the agent-writable
#: ``workspace/md-notebook`` could be renamed out from under its own mask.
_MD_NOTEBOOK_STAGING_LEAF: str = f"{MD_NOTEBOOK_APP_NAME}-staging"
_MD_NOTEBOOK_STATE_FILES: tuple[str, ...] = ("pat", "vaults.json", "settings.json")
#: The live state files, inside the state directory.
_MD_NOTEBOOK_STATE_LEAVES: tuple[str, ...] = tuple(
    f"{_MD_NOTEBOOK_STAGING_LEAF}/{name}" for name in _MD_NOTEBOOK_STATE_FILES
)
#: Where an older build kept the same files. Nothing writes them now, but a copy the
#: gateway's one-time move (``md_notebook_state``) has not moved yet can hold the PAT, so
#: they stay masked in every mode and are NOT carved back to the backend.
_MD_NOTEBOOK_RETIRED_STATE_LEAVES: tuple[str, ...] = tuple(
    f"workspace/{MD_NOTEBOOK_APP_NAME}/{name}" for name in _MD_NOTEBOOK_STATE_FILES
)

#: Crew-home leaves with no legitimate in-sandbox reader — bind-masked in every mode.
_CREW_HIDDEN_LEAVES: tuple[str, ...] = (
    # Gateway diagnostics: recorded host and gateway state, plus loop-stall dumps.
    # The gateway process writes these; an agent reads them through the owner-gated
    # /api/debug routes and the kirocrew-debug MCP tools, which redact on the way
    # out. The raw rows do not: they carry frame labels, folded stacks and process
    # detail that the read path scrubs, so a sandboxed session reading the files
    # directly would collect exactly what the routes exist to filter. Whole
    # DIRECTORY rather than a leaf file, because the day files rotate by name and
    # the append pins the directory itself.
    "diag",
    # Channel credentials. Already file-masked in cc/strict via ``_CC_FILES``; listing
    # it here extends the same treatment to standard, where a spawned command could
    # otherwise read every Slack/Discord token off disk.
    ".env",
    # App data holding live credentials or owner-authorization bits. Whole DIRECTORY,
    # not the leaf file, because an atomic write renames a sibling temp into place.
    "apps/aws-control/data",
    # Gateway-owned transfer staging for the aws-control app: the AWS CLI lands
    # object bytes here before the gateway reads them back. Masked so a same-UID
    # agent cannot swap a destination for a link between the gateway's create
    # and the CLI's open; the CLI spawn that must write into it names its
    # per-call directory in ``extra_visible_dirs``, which lifts the mask for
    # that one fixed-argv child only. A TOP-LEVEL leaf rather than one under
    # ``apps/aws-control/``: a mask covers the leaf, not its ancestors, and an
    # agent-writable ancestor could be renamed out from under it mid-transfer.
    "aws-control-staging",
    # Restart-surviving vouches (``kiro_crew._durable_vouch``). Each file is the
    # gateway's word that a session may reach its member's private store, so a
    # sandboxed writer could forge admission to a peer member's memory. Only the
    # unsandboxed gateway reads or writes it. Deliberately NOT under ``trust/``,
    # which stays read-write for the SEL log.
    "vouched-executions",
    # Quarantine markers for auto-improvement clones whose provisional rollback AND
    # retirement both failed. Each marker is the only durable record that a clone still
    # carrying a REFUSED, unscanned commit must never be reused, and the process it has to
    # outlive is an agent's: masked so a reviewer's shell cannot plant, rewrite or delete
    # one. Nothing in-sandbox reads it -- the marker is written and consulted host-side by
    # `auto_improvement.backend.clone_setup` -- so HIDDEN rather than READONLY. A TOP-LEVEL
    # leaf for the `aws-control-staging` reason: a mask covers the leaf, not its ancestors,
    # and a marker under `apps/auto-improvement/data/` would sit below a directory an agent
    # can rename out from under the mount.
    "quarantined-clones",
    "apps/meetings/data/edits",
    "whatsapp",
    # The refused-inbound spool. Fenced from agent FILE TOOLS by
    # ``security._CREW_SECRET_LEAVES``; masked here so a spawned command cannot
    # reach it either -- an entry an agent could write is posted on the next
    # start, verbatim, as a gateway-authored notice into the conversation it
    # names. Whole directory, because the spool is written by atomic replace via
    # a sibling temp name, and because the lock file beside it is what serializes
    # concurrent refusals. Nothing inside the sandbox reads or writes it: both
    # the spool write and the notice pass happen in the GATEWAY process, which
    # opens the paths directly.
    "inbound-spool",
    # The durable task queue (``tasks/tasks.db`` + SQLite siblings). Fenced
    # from agent file tools by ``security._CREW_SECRET_LEAVES``; masked here so
    # a spawned shell's ``sqlite3`` cannot read other sessions' task prompts or
    # rewrite their rows. Whole directory (WAL/journal/shm siblings). Nothing
    # in-sandbox touches it: the subagent manager, the runner adapters and
    # ``/api/tasks`` all live in the gateway process and open it directly.
    "tasks",
    # The per-process scratch root (``agent_scratch``): every kiro-cli session
    # and every shared runtime gets ``<home>/scratch/<label>-<rand>`` as its
    # ``TMPDIR``. Masked as a WHOLE so one session tree cannot open another's
    # scratch; each spawn passes its OWN directory back through
    # ``extra_private_dirs`` (``acp/client.py``, ``acp/runtime.py``) -- a
    # window INSIDE the mask, not a lift of it -- and a spawn made on behalf of
    # an existing session tree (a companion runtime, a dedicated subagent
    # process, a recycled runtime's successor) passes the TREE's work directory
    # as a second such window, so ``$KIROCREW_SCRATCH`` names one place for the
    # whole tree. Siblings from other trees stay hidden either way.
    "scratch",
    # The cross-process work root (``work_root``): ``<home>/work/<key>``, for
    # state that must OUTLIVE the process that created it. Masked as a whole and
    # given NO window, which is the difference from ``scratch`` above: the only
    # caller that may allocate is UNSANDBOXED gateway code (the maintenance
    # sweep), and no environment variable names the root. The other two classes
    # that plausibly want durable state do NOT qualify and must not be told
    # otherwise -- a script cron's ``boot_platform()`` shares the agent's mount
    # namespace (see the reconciliation note above) and an app backend is itself a
    # sandboxed spawn -- so each is served the way the Notes state files below
    # are: whatever SPAWNS it allocates host-side and passes that one key
    # directory back as a window, never a lift of the mask. Leaving the mask
    # window-less is what keeps the failure honest: on Linux the mask is a
    # WRITABLE empty tmpfs bind, so an in-sandbox allocation would otherwise
    # succeed and lose its bytes with the namespace.
    # Keys are deterministic by design -- an
    # issue or pull-request number, so a later run finds the directory again --
    # so an unmasked root would let one session tree guess a name and rewrite
    # another job's in-flight clone, a sharper hazard than the random-suffixed
    # scratch names carry. The mask alone would be VACUOUS here, which is why this
    # leaf appears in two more places: the root is created lazily by the first
    # ``allocate_work`` call, so it is also in ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``
    # (the loop below binds nothing over an absent name), and it is in
    # ``security.paths`` so the file tools refuse it too. All three, because each
    # one alone leaves a different path open.
    "work",
    # The Notes state files' old bare spellings: kept masked for a copy not yet moved,
    # and never carved back (the backend reads only the state directory).
    *_MD_NOTEBOOK_RETIRED_STATE_LEAVES,
    # The Notes state DIRECTORY, masked whole. It is OWNED by the md-notebook backend,
    # itself a sandboxed spawn (`apps/backend.py`), which gets it back through
    # :func:`app_backend_visible_targets`; the mask still applies to every OTHER
    # sandboxed process.
    _MD_NOTEBOOK_STAGING_LEAF,
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
    # The conductor work ledger: a worker's full file toolset must not reach any
    # conductor's records except through the routes that check its binding.
    "work-ledger",
    # Every append-only per-unit crew log, crew and session alike (crew_log/store.py).
    # The design treats the crew log as the authority a conductor reads instead of
    # re-deriving, so an in-sandbox process able to write here could forge an
    # entry attributed to the gateway or rewrite the history it is reporting
    # into. The library's write rules bind only callers who go through it, and the
    # file-tool fence answers only the agent's own tools -- neither answers a
    # sandboxed subprocess calling ``open()``, which is what this entry is for.
    # Nothing in-sandbox reads one: the store runs in the GATEWAY process, so
    # HIDDEN rather than READONLY. Named at the shared ``crew-log`` root, so a
    # future unit kind is covered without a new entry.
    "crew-log",
    # The RETIRED root, for the reason its file-tool fence is kept: this rename
    # ships no migration, so entries an older build wrote under ``<home>/ledgers``
    # are still on disk and would lose their OS mask on upgrade. Deliberately NOT in
    # ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``: that set exists for a root the store
    # creates LAZILY, and nothing creates this one any more, so there is no writer to
    # race. Precreating it would re-materialise the retired name on every machine.
    "ledgers",
    "cron-history",
    # The single-use step-up nonce that authorizes recording a flagged-file
    # delivery grant. Whole DIRECTORY (arm renames a sibling ``.tmp`` into place),
    # with NO in-sandbox reader: the gateway writes it on arm and the host
    # ``kirocrew file-delivery approve`` reads it. Masked because a same-UID agent
    # could otherwise FORGE a nonce here with a runtime-constructed shell path
    # (the file gate's text/argv matcher does not see such a path) and then drive
    # the owner's loopback browser to POST that chosen nonce, recording a grant
    # with no human present -- the exact self-approval hole the step-up exists to
    # close. Deliberately NOT under ``trust/``, which is sandbox-visible for SEL
    # appends.
    "file-delivery-consent-pending",
    # The cron in-flight markers, masked rather than sealed read-only because
    # nothing in the sandbox reads one: they are written and cleared by the run
    # task in the GATEWAY process, and the boot-time loop-stall breaker that acts
    # on them runs in ``CronService.start()``, which only the gateway calls. The
    # in-sandbox ``mcp_cron`` service is a store accessor and never starts.
    "cron-running",
    "workflow_library",
    # The crew appearance library: user-imported packs a crew wears. Written and
    # read only by the GATEWAY (the owner-gated ``/api/appearances`` routes); no
    # in-sandbox code opens it. Left visible, a sandboxed agent could ``rm -rf``
    # packs the user cannot get back -- the same data-loss class ``backup`` and
    # ``workflow_library`` are masked for.
    "appearance-library",
    # The chat_tag authorization store (grant rows saying which tags an agent
    # may self-apply). It governs what the AGENT is allowed to do, so it must
    # not be writable by the population it governs — and ``trust/`` cannot
    # host it, because that directory stays sandbox read-write for the SEL
    # appends above. Written and read only by the GATEWAY (dashboard tag CRUD
    # + the chat_tag applier); no in-sandbox code opens it.
    "tag-grants",
    # The person's pinned-session order: person-only by route, so the agents
    # it would reorder for must not rewrite it on disk. Gateway-only reader and
    # writer, like tag-grants above.
    "pinned-order",
    "agentcore-inbound",
    "routing",
    "webhooks",
    # The live-target pointer: it names the checkout the gateway ``execve``s into at
    # startup, so a sandboxed process that could write it would choose the code the
    # whole host runs next. Masked from Dev Fleet's OWN backend as well — that spawn's
    # build children share its namespace, so a carve-out for it is a carve-out for any
    # worktree's lifecycle script. The only writer is the gateway process (Dev Fleet's
    # in-gateway cutover route); the backend reads pointer state through that route.
    # Given a mount target before every spawn by
    # :func:`_materialize_live_target_mask_target`, because an ABSENT file cannot be
    # masked and the data-home root is writable in-sandbox.
    _LIVE_TARGET_LEAF,
    # Where that pointer's absent-equivalent stub is staged before being linked in; a
    # whole-directory mask so the in-flight temp is never a visible, linkable name.
    _LIVE_TARGET_STAGING_LEAF,
    "backup",
    "mcp-apps",
    # Published crew webview records. Same model as the entries above, and named
    # here rather than under ``trust/`` for a specific reason: ``trust`` is a
    # declared READ-WRITE exception below (in-sandbox ``verify_session_pid`` reads
    # ``trust/sel_hmac.key`` and the in-sandbox MCP servers append to the audit
    # log), so a record under it stayed writable by a sandboxed command that built
    # the path at runtime -- defeating command matching, which has no literal path
    # to match. Masking costs no live consumer: the publishing MCP tool does not
    # import the store at all, it POSTs to ``/api/agent-panel/publish``, so the
    # gateway process is the only writer and the only reader.
    #
    # Listed in ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES`` too, because on Linux the
    # mask is a bind mount and the loop guards on ``isdir`` -- an absent directory
    # is SKIPPED, which on a fresh install is exactly the disposition this entry
    # exists to deny.
    "crew-panels",
    # Subagent panel dismissals (``subagent_persistence``): one file per run whose
    # finished card the operator dismissed. Exactly the same shape as
    # ``crew-panels`` and here for the same reason rather than under ``trust/``:
    # the record is an OWNER decision about what the panel hides, so a sandboxed
    # process that built the path at runtime must not be able to forge one (hiding
    # a run nobody dismissed) or unlink one (resurrecting a cleared card), and
    # ``trust`` stays sandbox read-write for SEL. Masking costs no live consumer:
    # the gateway is the only writer (the dismiss route and the manager settle) and
    # the only reader (the panel's durable rebuild).
    #
    # In ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES`` too, because the directory is created
    # on the FIRST dismissal -- so on a fresh install the ``isdir`` guard would skip
    # it and the mask would be vacuous for the life of that sandbox, which is
    # precisely the disposition this entry exists to deny.
    "panel-dismissals",
    # Crewmate teams (``crew_teams.py``): which crewmates are on which team. The
    # same shape as ``crew-panels`` and for the same reason it is not under
    # ``trust/``: a team is the OWNER's grouping, so the crewmates it groups must
    # not be able to rewrite it, and ``trust`` stays sandbox read-write. Two
    # owner-invoked writers open it, neither sandboxed: the gateway (the
    # owner-gated ``/api/teams`` routes, the crew create/delete/package-sync
    # hooks) and ``kirocrew agent create`` / ``delete`` from the operator's shell. Whole
    # directory: ``atomic_write`` publishes through a sibling temp.
    "crew-teams",
    # Each crewmate's governing records (``member_records.py``): its permanent
    # rules, its goals and its DM thread binding, one ``<slug>.json`` per kind.
    # The person writes them through the dashboard and the gateway injects them
    # into that crewmate's prompt or routes its thread by them, so no sandboxed
    # process may read or write any of them. Deliberately NOT under ``trust/``,
    # which stays sandbox read-write for SEL; an older build kept them there and
    # the gateway moves them once (``member_records.migrate_from_trust``). Only
    # the gateway and the operator's own CLI open it. Whole directory: every
    # writer publishes through a sibling temp.
    "member-records",
    # Auth stores and signing keys owned by the gateway web server alone.
    "token_signing.key",
    "refresh_chains.json",
    # The staging directory those two publish through. Masked as a whole DIRECTORY so the
    # in-flight temp -- which holds the same key and chain-state bytes as the two leaves
    # above -- and any crash orphan are covered at every name, present and future.
    _AUTH_STORE_STAGING_LEAF,
    "kas",
    "ops_mission_control_secrets.json",
    "ops_mission_control_policy.json",
    # Web Push credential stores (``notifications/push_store.py``, ``vapid_keys.py``), owned by the
    # gateway alone: ``vapid_keys.json`` holds the VAPID EC private key and
    # ``subscriptions.json`` each device's ``p256dh``/``auth`` secret. Masked as a whole
    # DIRECTORY so the ``atomic_write`` temp they publish through, which holds the same
    # bytes, is covered at every name. No in-sandbox reader exists: the fan-out runs in
    # the gateway loop. In ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES`` too, because the stores
    # are written lazily (first subscribe, first key fetch), so on a fresh install the
    # ``isdir`` guard would otherwise skip the mask and let an agent read the key or
    # plant an endpoint.
    "web-push",
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

#: Hidden leaves whose Linux mask must refuse the read instead of answering it empty.
#:
#: The Linux mask is an empty file bound over the real one, so a sandboxed ``rsync``,
#: ``cp -a`` or ``tar`` of the data home reads zero bytes and copies them as the file's
#: contents. For ``token_signing.key`` that copy is permanent damage: the gateway never
#: overwrites an existing key (``token_secret._load_or_create_secret`` only creates one
#: with ``O_EXCL`` or a non-clobbering link), so a destination that receives the empty
#: copy signs with an ephemeral secret on every boot. An unreadable mask makes the copy
#: fail loudly instead: rsync reports ``Permission denied``, leaves the destination's
#: key untouched and exits 23. The macOS backend already denies this read, so this is
#: the same answer on Linux. Nothing in a sandbox reads the key; the gateway, which
#: does, runs outside it.
_CREW_UNREADABLE_MASK_LEAVES: frozenset[str] = frozenset({"token_signing.key"})
assert _CREW_UNREADABLE_MASK_LEAVES <= set(_CREW_HIDDEN_LEAVES)

#: Crew-home leaves masked at a RELOCATED data home on top of ``_CREW_HIDDEN_LEAVES``:
#: the secret vault. The tier lists name the vault only at its two ``$HOME``-joined
#: spellings, so a ``KIROCREW_HOME`` outside ``$HOME`` needs it re-anchored here the
#: way ``kas`` is. Kept out of ``_CREW_HIDDEN_LEAVES`` on purpose: that list also
#: drives the alias walk and the planted-link spawn refusal, and the vault's mask at
#: the default home goes through neither. Joined at each call site, not here, so a
#: patched ``_CREW_HIDDEN_LEAVES`` still reaches the plan.
_RELOCATED_CREW_HIDDEN_LEAVES: tuple[str, ...] = (".vault",)

#: Crew-home ceilings and gateway-managed data: readable by sandboxed code,
#: never writable by it. See the READONLY note above for why hiding a ceiling
#: inverts its effect; named memory stores need write integrity, not secrecy.
_CREW_READONLY_LEAVES: tuple[str, ...] = (
    # The empty directory kiro-cli starts in when it runs an app agent's launch copy
    # (``apps.agent_launch.launch_process_cwd``). kiro-cli reads project agents from
    # its process directory, so a sandboxed writer that could create
    # ``.kiro/agents/<launch name>.json`` here would choose the spec kiro-cli runs.
    # Read-only rather than hidden: kiro-cli itself must enter and list it.
    "app-agent-launch",
    # Gateway-owned run records restore app authorization on cold continuation.
    # Keep results readable, but agent code must not rewrite its app owner.
    # The second root holds the same authority for retained V1 runs only.
    "subagents",
    "member-memory-bindings",
    # Built-in named-store writes run in the gateway. Keep arbitrary sandboxed
    # code from rewriting that learning authority while allowing reads; this
    # ordinary sandbox rule is not a cross-member confidentiality boundary.
    "memory_stores",
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
    # The operator's grants of ``owner`` trust to hand-configured app registries.
    # Same class: a writable grant lets an auto-approved agent have a registry it
    # controls cloned with the machine's git identity.
    "registry_trust.json",
    "computer_use.json",
    "oauth_endpoints.json",
    "aws_service_consent.json",
    # The runtime config and its overlay. They are ordinary settings files, but they
    # also carry the switches that LOOSEN confinement -- ``agent.sandbox`` (``"off"``
    # skips this very sandbox for every later spawn), ``agent.apps_allow_third_party`` /
    # ``agent.apps_trusted`` (admit app code the gateway runs in-process, outside any
    # sandbox), ``agent.sandbox_allow_unsandboxed_exec``, ``agent.approval_mode``. The
    # loader's load-time clamp neutralises an inflated number, not a loosened switch, and
    # ``is_sensitive_write_path`` fences only the file-edit tool: a spawned shell's
    # ``open(..., "w")`` reaches the file however the write is spelled, so only a kernel
    # denial holds. Both files are sealed because ``KiroCrewConfig.load()`` deep-merges
    # ``config.local.json`` OVER ``config.json``, so sealing one leaves the setting
    # writable through the other. Every legitimate writer -- the dashboard config API,
    # the channel handlers, the gateway's boot-time default and migrations, and
    # ``kirocrew config`` from the operator's own terminal -- runs outside the sandbox;
    # reads stay open, and an in-sandbox ``kirocrew config set`` fails with a pointer to
    # those surfaces (``cli_config``). The ``<leaf>.lock`` sidecar stays writable: it
    # carries no setting, and the vector-memory publish takes it from wherever it runs.
    "config.json",
    "config.local.json",
    # Recorded consent to send conversation state to the external decision
    # provider. Same class as ``aws_service_consent.json``: a writable grant lets
    # an auto-approved agent switch on, for itself, the egress of the messages it
    # is being prompt-injected through. The read-only mount is the load-bearing
    # half; the shell gate matches no paths.
    "decisions_consent.json",
    # The decision log that keystone's feature writes. Sealing the grant and
    # leaving the RECORD writable would be half a control: an agent that cannot
    # switch the seam on can still append a ``kind="feedback"`` line -- the exact
    # shape ``decisions.log.build_feedback_row`` produces -- and the owner's own
    # summary would count a verdict nobody gave. A top-level DIRECTORY, like
    # ``profiles``: read stays open (a directory bind shows live contents, so a
    # day-file written later is visible), and every legitimate writer is the
    # gateway, outside the sandbox. Nothing writes a decision row from inside one.
    "decisions",
    # Downloaded model weights and the speech decoder store (``embeddings.models_dir``,
    # ``stt.models.models_dir``, ``stt.decoder.store_dir``). An input to a trust
    # decision: each loader checks a pinned sha256 and then reopens the file by name,
    # so a writable directory leaves a window to swap the bytes between the check and
    # the open. ``security.paths`` already refuses the agent's file-edit tool here;
    # this seal is the load-bearing half against a sandboxed shell, which that gate
    # never sees. A symlink or junction at the name is refused: a bind would seal its
    # referent and leave the name replaceable in the writable data home. Keep weights
    # as real files under models/; to put them on another disk, relocate the whole
    # data home (KIROCREW_HOME), so the seal covers them. READONLY rather than hidden,
    # because the in-sandbox knowledge embedder loads its GGUF from here.
    # Every writer -- the embedding and speech downloaders, the decoder fetch and the
    # speech loader's crash marker -- runs in the gateway, outside the sandbox. A
    # sandboxed spawn that needs a writable directory of its own cannot be given one
    # beneath this leaf: a private window opens only inside a HIDDEN tree. Such a
    # directory belongs under ``run`` as an ``extra_writable_dirs`` carve-out, as the
    # decision runtime's ``run/decisions/<id>`` does.
    # The seal covers paths, so an alias of a loader file is judged at the LOADER, and
    # fails that one load rather than every sandboxed spawn on the host. A link at a
    # step below models sits inside the seal, so only the host can have made it, and
    # it threatens only the bytes a loader reads. Each loader asks
    # ``require_real_model_path`` (no symlink or junction at any step from models to
    # the file) and ``require_unaliased_model_file`` (no second hard link on the
    # descriptor it reads): whisper in ``stt.models.ModelStore``, the GGUF in
    # ``embeddings._model_file_stamp_unaliased``. The decoder runs only a snapshot
    # whose bytes it re-hashes. A symlink or junction at the models NAME itself
    # refuses the spawn on both backends: the namespace builder through
    # ``_CREW_NOFOLLOW_READONLY_DIR_LEAVES``, Seatbelt through
    # ``_refuse_linked_models_leaf``.
    "models",
    # The hosts a reader allowed long-query links for, per workspace. Every entry
    # relaxes the exfiltration check for that host, so a writable list lets a
    # prompt-injected agent allow the host it wants to send conversation data to.
    # A top-level DIRECTORY for the ``decisions`` reasons: the gateway is the only
    # writer, an empty directory reads as "no host allowed", and a directory bind
    # shows the gateway's later writes live.
    "redaction-allow",
    # Recorded consent to deliver a scanner-flagged file. Same class as
    # ``aws_service_consent.json``: a writable grant lets an auto-approved agent
    # consent, on the owner's behalf, to shipping the owner's secrets. This seal is
    # the load-bearing half of that design -- ``is_sensitive_path`` covers the leaf
    # on the file-tool path, but the shell gate matches no paths at all, and as the
    # READONLY note above says a kernel write denial is what holds regardless of
    # how a command spells the way there.
    "file_delivery_consent.json",
    # Recorded consent to forward SSH_AUTH_SOCK into the sandbox. Same class as
    # ``aws_service_consent.json``: keeping the socket grants USE of the operator's
    # ssh-agent keys for the session, so a writable enable lets a prompt-injected
    # agent flip its own forwarding on and a subagent it spawns then authenticates
    # as the operator. This kernel write-denial is the load-bearing half --
    # ``is_sensitive_path`` covers the leaf on the file-tool path, but the shell
    # gate matches no paths, so the read-only mount is what holds regardless of how
    # a command spells the way there.
    "ssh_auth_sock_consent.json",
    # The owner's credential-redaction switch. Same class as the consent
    # records above: a writable record lets a prompt-injected agent switch off
    # the credential pass in the owner's dashboard file viewer, the one surface
    # the switch governs.
    # A missing or unreadable file reads as ON, so this kernel write-denial is
    # what keeps the switch the owner's regardless of how a command spells the
    # way there.
    "credential_redaction.json",
    # The browser launcher and its vendored Node package tree. Agent browser
    # commands must read and execute this directory, while a write would choose
    # the binary the unsandboxed gateway executes during startup reclamation or
    # an owner address-bar launch. The gateway installer runs outside the agent
    # sandbox, so it can still replace the managed copy.
    "playwright-cli",
    # The KAS engine bundles Kiro Crew installs itself (``acp.kas_bundle``): a
    # pinned ``@kiro/agent`` tree and the Node runtime that executes it. Same
    # class as ``playwright-cli``: a sandboxed KAS process must read and execute
    # this directory, while a write would choose the code every later KAS
    # session runs. ``kirocrew kas install`` runs in the gateway, outside the
    # sandbox, so it can still lay down a new version.
    "engines",
    # The cloud launcher's config. In-sandbox code READS it (the provisioner selector calls
    # ``CloudConfig.load()``, and so does the launch record's legacy fallback, which every
    # ``cloud`` verb reaches through ``LaunchState.load``), and a WRITE would let an
    # agent choose the container image a Fargate launch runs -- the task's execution
    # role delivers the model credential into that image before it starts, so a
    # rewritten ``fargate.image`` turns the owner's next launch into credential
    # delivery to an image the owner never chose. The digest rule constrains the
    # reference's FORM, not who owns the registry, so it is no obstacle. Same
    # both-layers treatment ``playwright-cli`` gets, for the reason the READONLY note
    # gives: ``is_sensitive_write_path`` covers the leaf on the file-tool path, while
    # a sandboxed shell's ``open(..., "w")`` reaches it however the write is spelled,
    # and only a kernel denial holds there. Nothing in the product writes this file at
    # all -- the launch path's own fields live in ``cloud.launch_state`` -- so the seal
    # costs no writer anything; it is the operator's file and only they write it.
    "cloud.json",
    # The launch RECORD. Sealed for a reason of its own rather than by association: the tag
    # in it is what ``cloud destroy`` resolves without ``--tag``, so a writable copy lets a
    # sandboxed process choose which stack a ``destroy --yes`` deletes. The write gate above
    # covers the agent's file-edit tool; only a kernel denial covers a sandboxed shell's
    # ``open(..., "w")``, however the write is spelled. Absent-file coverage is the
    # pre-create list (``sandbox_precreate._CREW_PRECREATE_READONLY_FILE_LEAVES``),
    # because a name nothing occupies is a name an agent creates.
    "cloud_launch_state.json",
    # The microvm lane's CREW RECORDS (``cloud/microvm/record.py``). Sealed for the same
    # reason as the launch record above and a sharper version of it: this document holds
    # each crew's ``microvm_id``, and ``teardown`` hands that value straight to
    # ``launcher.terminate`` -- no ``--tag`` to disagree with, no describe-and-ask step in
    # front of it. So a sandboxed process that could rewrite one crew's id would choose
    # which of the owner's VMs the next delete destroys, taking its home with it.
    # The gateway writes it outside the agent's file-edit gate, so the launch path is
    # unaffected, and absent-file coverage is the pre-create list
    # (``sandbox_precreate._CREW_PRECREATE_READONLY_FILE_LEAVES``).
    #
    # Deliberately NOT on the child-readable list, unlike ``cloud.json`` and the launch
    # record: those two are classified there because in-sandbox code genuinely reads
    # them, and this one has no in-sandbox reader at all -- the engine and the turn
    # route are the only callers of ``CrewStore`` and both run in the gateway. Adding
    # it there would grant a read nobody needs, which is a widening, not a seal.
    "microvm_crews.json",
    # The pi gate launcher and sealed extension must be readable and executable by
    # the enforced harness's child, but never writable by it. The launcher cache
    # accepts an existing path after ``isfile`` without re-verifying its content, so
    # a writable child could plant the launcher a later session executes.
    #
    # On the precreate and no-follow lists above as well, for the same two reasons
    # ``playwright-cli`` is: ``mount(2)`` cannot seal an absent path, so a leaf left off
    # them stays WRITABLE in-sandbox until pi first runs, and the mounted NAME has to
    # stay the real directory because the gateway later execs out of it. Those lists are
    # walked identically for every backend and carry no per-adapter branch, so an
    # unsafe state here refuses the spawn exactly as it does for any other entry.
    # ``acp/harness/pi.py``'s ``_pi_gate_artifact_dir`` re-checks the same states on the
    # pi launch path, before the sandbox is built, so a pi session refuses with a message
    # that names the directory it actually resolves.
    "pi-gate",
    # The app dev-mode AUTHORIZATION record (operator grants binding each dev
    # app to its resolved ui root — see apps/dev_mode.py). Sealing it makes
    # "operator, not agent" kernel-enforced: a sandboxed process cannot mint,
    # extend, or rewrite a grant no matter how the toggle is spelled (the
    # deny-list's text/argv tiers can be evaded by runtime construction —
    # ``$(printf ...)`` — but a Seatbelt/namespace write denial cannot). The
    # gateway ensures the file exists at startup (see apply_dev_mode's
    # reconcile) because the Linux launcher can only seal an EXISTING target.
    "apps/.dev-grants.json",
    # The settings-seed provenance record (``acp.seed_provenance``), an
    # AUTHORIZATION record on the same footing as the dev-mode grants above: an
    # entry in it is what lets Crew OVERWRITE and then DELETE
    # ``<work_dir>/.claude/settings.local.json``, because a re-seed proceeds only
    # when the file on disk still matches the recorded ``(size, sha256)``. A
    # sandboxed process that can write it can enter a digest for a settings file
    # the USER hand-wrote, and the next gateway start adopts that file, replaces
    # its ``permissions.defaultMode`` with Crew's, and unlinks it on reset.
    # READONLY rather than hidden because the write is the whole risk: the record
    # holds no secret (work-dir paths and digests), so masking it would buy
    # nothing and cost an operator the ability to see why a seed was adopted.
    # Paired with the same leaf on ``security``'s write floors -- the deny rules
    # fence how a command SPELLS this path, and the kernel denial is what still
    # holds when a spelling is built at runtime (``$(printf ...)``).
    #
    # The record lives INSIDE the ``seed-provenance`` directory, and the directory is
    # what is sealed, because the writer publishes every save through ``atomic_write``
    # (a new inode) and does so on every claude-agent-acp seed and release. A sealed
    # FILE whose inode changes after the pre-spawn pass recorded it is refused by the
    # launcher's pin, so sealing the file made an unrelated spawn fail whenever a seed
    # write landed in that window. The rename leaves the directory's inode alone.
    # The old top-level name stays sealed too: a Crew process started before the
    # upgrade still reads and writes it, so it must stay unforgeable for that process.
    "seed-provenance",
    "settings_seeds.json",
    # The crew webview template directory. A ceiling in exactly the sense above:
    # the whole value of splitting a panel into human-authored TEMPLATE and
    # agent-published DATA is that layout is authored by a person, so a crew must
    # never be able to write one -- a template it authored could put markup, and
    # therefore a hostile issue body's markup, straight into the operator's
    # dashboard. ``security._CREW_SECRET_LEAVES`` fences it from the agent FILE
    # TOOLS; sealing it read-only here closes the other half, because a fence that
    # only covers file tools is bypassed by any spawned shell that can write.
    #
    # READ-ONLY rather than masked, and the direction matters: templates are
    # versioned, human-reviewed repo content with nothing secret in them, so
    # reading one costs nothing, while hiding a directory the OPERATOR drops
    # overrides into would silently change which template renders.
    "panel-templates",
    # The fork-lineage / model-state sidecar (agent_state.py). Same
    # input-to-an-authorization-decision class as the ceilings above:
    # ``forked_from`` / ``private_to`` decide whether the fork endpoint treats
    # a template as one crew's private copy, so a forged entry makes the
    # owner's next PATCH mutate a SHARED template. The tool gate and the bash
    # text matcher fence the spelled paths, but a spawned interpreter's
    # ``open()`` or a split-string shell path reaches the file by constructed
    # runtime paths no text gate can see — only an OS disposition closes that.
    # Read-only, not hidden: in-sandbox readers (a script cron's fork-info
    # reads) must keep working, and every legitimate WRITER (the dashboard
    # fork/publish/reset handlers, the CLI) runs in the unsandboxed gateway or
    # user process. Absent-file coverage differs per backend: the seatbelt
    # denies by literal path (creation IS a write), while the Linux mount seal
    # needs a file to bind — so this leaf is ALSO in
    # ``_CREW_PRECREATE_READONLY_FILE_LEAVES`` and gets materialised as ``{}``
    # before every namespace spawn.
    "agent_model_state.json",
    # The sidecar's cross-process advisory lock. Unsealed, a sandboxed process
    # can unlink and recreate it, so concurrent writers lock DIFFERENT inodes
    # and the loser's stale read-modify-write erases lineage the winner just
    # recorded — the lock file's identity is what makes the lock a lock. No
    # sandboxed process legitimately takes it: every locker (agent_state's
    # mutators via the dashboard/CLI) runs unsandboxed. Absent-file coverage
    # mirrors the sidecar's own entry via the pre-create list.
    "agent_model_state.json.lock",
    # The operator's approved MCP launch fingerprints (``mcp_gateway.launch_approval``).
    # An input to a decision that runs a program OUTSIDE the sandbox: gatewayd
    # spawns a stubbed server's backend as the user, and this record is what says
    # which command a stubbed name may run. A sandboxed writer could approve its own
    # command. Read-only, not hidden: it holds hashes and server names, no secret.
    # Every writer (the dashboard stub toggle, the gateway's rewrite pass) runs in
    # the gateway process, outside the sandbox.
    "mcp-launch-approvals",
    # Gateway resolve-once artifacts choose the entry point substituted for an
    # approved npm launcher. The installer runs in the unsandboxed gateway.
    "mcp/resolved",
    # This gateway's stable random identity (``gateway_identity.GATEWAY_ID_FILE``). The
    # Remote Crew cycle guard compares it to refuse a chain that loops back on itself,
    # so a sandboxed writer would choose whether a loop is detected. The read path
    # already judges the descriptor it opens; this seal is the write half. Read-only,
    # not hidden: the id is random, carries nothing secret, and is already served on
    # ``/api/health``. The only writer is the unsandboxed gateway's first-read mint.
    "gateway_id",
    # The skill projection's side files (``acp/skill_projection_parts/side_files.py``): its
    # publication lock, the per-process lease records and the ownership sidecars with the view
    # ledger. The sidecars and the ledger are an INPUT TO AN AUTHORIZATION DECISION: a
    # conversation stored under a ``kirocrew-skill-view-*`` name resumes under the agent
    # its record maps it to (``source_agent_name``), with that agent's grants, and this
    # directory is read before the legacy one in the agents tree -- which the sandbox
    # already seals read-only. A sandboxed shell that could write a record here would
    # pick the agent a later resume runs as. Leases and the lock steer the prune (a
    # forged lease keeps any view alive). Read-only, not hidden: no credential lives
    # here, and an in-sandbox resolver may read a record. Every legitimate writer --
    # the spawn paths that prepare a view, the boot drain, the finalizer's prune --
    # runs in the gateway, outside the sandbox; an in-sandbox preparation already
    # failed closed on the sealed agents tree and falls back to the authored agent
    # the same way here. On the precreate and no-follow lists too: the directory is
    # absent until the first spawn prepares a view, and a link at the name would seal
    # the referent and leave the name replaceable.
    "skill-projection",
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


#: Leaves NO foreign harness's child may read by default, even though a sandboxed
#: process may. Absent from :data:`_CREW_CHILD_READABLE_LEAVES` and therefore kept in
#: the enforced adapter's OS credential mask.
#:
#: Two reasons put a leaf here, and the distinction matters to anyone adding one.
#: Almost every entry carries a live credential or a capability, so no child should
#: ever read it. One entry instead belongs to a SINGLE harness, so a blanket grant
#: would be too wide: it is withheld here and handed back to that one harness by
#: ``tool_gate.adapter_hidden_credential_dirs``, which keys its gate-artifact
#: exclusion on the backend. "Withheld" therefore means "not granted to everyone",
#: not always "secret".
#:
#: Paired with that list rather than derived from it. Neither is the complement of
#: the other at runtime: both are written out, and
#: ``test_sandbox_governance_mask`` pins that together they cover
#: ``_CREW_SANDBOX_VISIBLE_LEAVES | _CREW_READONLY_LEAVES`` exactly and do not
#: overlap. That pin is the whole point -- see :data:`_CREW_CHILD_READABLE_LEAVES`.
#:
#: Every entry carries a live credential or a capability, so handing it to a
#: third-party binary that self-approves its own passive reads is the exact class
#: that mask exists to compensate for. The first-class path's own disposition is
#: looser (``AGENTS.md`` records ``sel_hmac.key`` as a knowingly-carried VISIBLE
#: residual with no OS fence), and widening that residual from Crew-shipped binaries
#: to any enforced harness is a decision an operator makes, not one a mask change
#: inherits. Closing it properly means moving each in-sandbox reader behind the
#: gateway -- never a looser mask.
#:
#: ``run`` is here for the same reason and needs one extra fact, because
#: :data:`_CREW_SANDBOX_VISIBLE_LEAVES` says the child "cannot exec if it is masked".
#: That is true of the tier's OWN mask and not of this one: the Linux launcher
#: bind-mounts the empty dirs AFTER it is already running and only then execs
#: (``fork -> unshare(CLONE_NEWUSER) -> unshare(CLONE_NEWNS) -> bind -> exec``), so a
#: masked ``run`` never blocks the launcher that lives in it. It blocks only a child
#: that execs a SECOND artifact out of ``run`` from inside the namespace. The answer
#: for such a child is to move its artifact to a credential-free leaf of its own,
#: never to expose ``run`` and with it ``run/gateway-<port>.secret``, which
#: ``config.loader.read_local_secret`` resolves BEFORE the shared ``.local_secret``.
#:
#: The pi gate is exactly that child, and it takes that answer: its launcher and
#: sealed extension live under their own ``pi-gate`` leaf, so a pi session starts with
#: ``run`` masked. Keeping ``run`` masked is what makes such a leaf the answer;
#: exposing it to unbreak one harness would hand the gateway credential to four.
_CREW_CHILD_WITHHELD_LEAVES: tuple[str, ...] = (
    # The per-listener gateway credential (``run/gateway-<port>.secret``).
    "run",
    # The SEL trust root and the audit key inside it.
    "trust",
    "sel_hmac.key",
    # The dashboard internal-API bearer secret.
    ".local_secret",
    # Capability-bearing job state: a cron entry carries the session key its run
    # executes under, so a readable store leaks that key and a writable one mints work.
    "crons.json",
    # Durable cross-session capability, and the reason this entry reads as a
    # surprise: the leaf name says "bindings", but a binding FILE carries the raw
    # session key it binds (``member_memory_auth.bind_private_session_store`` writes
    # ``{"version": 1, "session_key": <raw>, "memory_store": ...}``), under both
    # ``sessions/<digest>/`` and ``pids/``. So reading the directory hands over every
    # member's key, not a digest of one, and a leaked session key stays usable.
    "member-memory-bindings",
    # The SEL audit log and its rotation directory. Withheld for the WRITE side above
    # all: ``_CREW_SANDBOX_VISIBLE_LEAVES`` keeps these read-write precisely so an
    # in-sandbox MCP server can append, and a foreign harness with the same access can
    # rewrite or truncate the record of its own actions. Losing the append makes an
    # audit-or-deny write fail, which DENIES the action it was auditing -- the safe
    # direction, and the reason this is a withhold rather than a carve-out.
    "security_events.jsonl",
    "security_events.d",
    # Both admission stores hold ``trust_keys``: signer -> SHARED SECRET, verified with
    # ``hmac.new`` plus ``compare_digest`` (``apps/admission.py``). A child that reads
    # one can sign a manifest or a policy that admission then accepts, so these are
    # credential files rather than the plain ceilings they resemble. Withholding them
    # costs in-sandbox app admission, in the safe direction: an empty
    # ``app_admission.json`` reads as deny-all and an empty ``admission_policy.json``
    # refuses to compose, so an app is turned away rather than admitted unchecked.
    "admission_policy.json",
    "app_admission.json",
    # Every other readable leaf can name the in-sandbox reader that breaks without it.
    # This one cannot: the gateway owns both the writes and the reads, so nothing in a
    # sandbox needs it. The seal beside it answers a WRITE ("not a cross-member
    # confidentiality boundary" is a statement about rewriting a learning authority),
    # and that argument does not carry to a read by a foreign harness, whose passive
    # reads are the very thing this mask compensates for -- they reach no gate and
    # leave no record. One member's silo holding another's preferences and lessons is
    # worth a denial that costs nothing.
    "memory_stores",
    # The microvm lane's crew records, withheld on the same ground as ``memory_stores``
    # and for the same reason it cannot be classified readable: no in-sandbox reader
    # breaks without it, because the gateway owns every read and every write -- the
    # engine and the crew-turn route are the only callers of ``CrewStore``. The seal on
    # the read-only list answers the WRITE (a rewritten ``microvm_id`` chooses which VM
    # the next delete destroys), and that argument says nothing about a READ by a
    # foreign harness, which reaches no gate and leaves no record. What a read hands
    # over is the owner's live cloud inventory -- each crew's VM id, its node id, its
    # endpoint and the NAME of its control secret -- which is reconnaissance for the
    # write this list cannot prevent. Withholding it costs nothing at all.
    "microvm_crews.json",
    # The governance ceiling, its trust root, and every policy or consent document
    # beside them. Withheld as one family because they share one reader and one risk:
    # each is an INPUT TO AN AUTHORIZATION DECISION that an in-sandbox process makes,
    # and an enforced harness's child reads them through a channel that reaches no gate
    # and leaves no record. Hiding them costs in-sandbox governance resolution, and it
    # costs it CLOSED: the launcher binds an empty file, an empty ceiling document makes
    # ``boot_platform()`` raise, and an empty consent record reads as consent withheld,
    # so a session refuses rather than proceeding ungoverned. That is the same direction
    # the rest of this list takes, which is why they sit together.
    "security_policy.json",
    "profiles",
    "denied_commands.json",
    "registry_trust.json",
    "computer_use.json",
    "oauth_endpoints.json",
    "decisions_consent.json",
    "file_delivery_consent.json",
    "ssh_auth_sock_consent.json",
    "credential_redaction.json",
    # The paid-AWS consent grant. Unlike its sibling consent records this one stores
    # IDENTIFIERS as well as a decision -- ``Grant.to_dict`` writes ``account`` and
    # ``arn`` -- so a read tells a foreign child which AWS account and caller identity
    # the owner works as. It sits on the read+write keystone floor, so no in-sandbox
    # reader had it before this change either: the app backend that consults it is not
    # an enforced-harness child. Withholding it costs nothing that worked.
    "aws_service_consent.json",
    # The ONE entry here that holds no secret. It carries the launcher and sealed
    # extension of a single harness's tool gate, and only that harness's child has to
    # execute them, so the grant is made per backend in
    # ``tool_gate.adapter_hidden_credential_dirs`` rather than to every child here.
    # Withheld from the shared set for reach, not for secrecy: a leaf no other child
    # needs is a leaf no other child should get, and the narrower grant says which one
    # does. The read gate still fences it from the agent's own file tools, and the
    # read-only and no-follow seals still stop any child replacing what it execs.
    #
    # The literal, not ``tool_gate.PI_GATE_ARTIFACT_LEAF``: that module resolves this
    # one lazily to avoid a load-time cycle, so importing its constant here would
    # close the cycle from the other side. The seal lists above spell it the same way.
    "pi-gate",
)

#: Leaves an ENFORCED harness's child MAY read. The other half of the pair above.
#:
#: Written out rather than computed as "everything not withheld", because the two
#: spellings fail in opposite directions and only one of them fails safely.
#:
#: A complement would make classification OPTIONAL: a credential-bearing leaf added
#: later to :data:`_CREW_SANDBOX_VISIBLE_LEAVES` or :data:`_CREW_READONLY_LEAVES`
#: would land in the child-readable set by default and be handed to a foreign harness
#: -- silently, because nothing reads as wrong and no test knows the leaf exists. A
#: docstring asking the next author to check the withhold list is not a control.
#:
#: Written out, classification is MANDATORY: a new leaf appears in neither list, the
#: completeness pin in ``test_sandbox_governance_mask`` fails, and the author has to
#: say which side it belongs on. The drift a hand-maintained list normally invites is
#: exactly what that pin converts from silent into loud, which is why duplicating the
#: names here costs nothing real.
#:
#: So the default direction is the point. Before this pair existed a forgotten leaf
#: stayed masked and broke a boot, which someone notices. Under a complement it would
#: be exposed, which nobody notices.
_CREW_CHILD_READABLE_LEAVES: tuple[str, ...] = (
    # The empty directory a launch copy's kiro-cli starts in. It holds nothing, so a
    # foreign child that reads it learns nothing.
    "app-agent-launch",
    # The browser launcher an agent browser command must read and execute. The only
    # entry here the read gate also fences, so the only one whose exclusion changes
    # what an enforced child can open; the rest are already outside the mask.
    "playwright-cli",
    # The KAS engine bundle a sandboxed KAS process must read and execute. The
    # second entry the read gate also fences, for the same reason as the first.
    "engines",
    # Authorization and lineage records an in-sandbox reader resolves. Read-only
    # sealed for the same reason: a forged entry decides a later grant.
    "apps/.dev-grants.json",
    "seed-provenance",
    "settings_seeds.json",
    "agent_model_state.json",
    "agent_model_state.json.lock",
    # Gateway-owned run records, read to restore app authorization on a cold
    # continuation. The risk they carry is a rewritten app owner, not a read, and the
    # read-only seal is what answers it. Classified for completeness rather than for
    # effect: this leaf is not on the read-gate floor, so the mask never covers it and
    # neither classification changes what any child can open.
    "subagents",
    # The decision log. Same shape as the entry above, and for the reason this module
    # gives it: read stays open on purpose, every legitimate writer is the gateway
    # outside the sandbox, and nothing writes a decision row from inside one. Also off
    # the read-gate floor, so the mask never covered it either way.
    "decisions",
    # Model weights and the speech decoder. No credential, and the in-sandbox
    # knowledge embedder reads the GGUF here. The risk is a write, answered by the
    # read-only seal. Off the read-gate floor too (only edits are refused), so the
    # mask never covered it.
    "models",
    # Allowed hosts for the exfiltration check. Host names, not credentials; the
    # risk is a write, answered by the read-only seal.
    "redaction-allow",
    # The operator's cloud configuration and the launch record beside it. Neither holds
    # a credential (``CloudConfig`` documents the file as the operator's own, with none),
    # and in-sandbox code READS both: the provisioner selector resolves the Fargate block
    # and every ``cloud`` verb reaches the record through ``LaunchState.load``. What they
    # carry is a WRITE risk -- the image a launch runs, the stack a destroy resolves --
    # and the read-only seal above is what answers it. Withholding them would mask a read
    # the product depends on and buy nothing.
    "cloud.json",
    "cloud_launch_state.json",
    # The runtime config and its overlay. NOT credential-free, unlike ``cloud.json``:
    # a channel bot token stored inline (``telegram.bot_token``, ``discord.bot_token``,
    # ``weixin``/``webex``; the ``sensitive`` fields in ``config/sections_*.py``) lives
    # in these files, and the env/.env spelling those fields recommend is only a
    # recommendation. The read is granted anyway because in-sandbox code DEPENDS on it:
    # every ``KiroCrewConfig.load()`` in a sandboxed CLI or MCP server opens both, and
    # withholding them would mask a read the product cannot run without. It is also
    # not a widening: neither is on the read-gate floor (``is_sensitive_path`` is False
    # for both), so the mask never covered them and a child could always open them --
    # classifying them here keeps exactly what it had. The risk the seal answers is the
    # WRITE: the switches that loosen confinement, which the read-only entry above
    # refuses.
    "config.json",
    "config.local.json",
    # The crew webview template directory. Holds no credential and is no input to an
    # authorization decision an in-sandbox process makes: a template decides how a
    # published panel is laid out, never who may publish one, and every reader runs
    # in the gateway (``agent_panel.available_templates`` behind the dashboard route,
    # the renderer behind publish) -- the ``kirocrew-panel`` MCP server asks that
    # route over HTTP rather than opening the directory itself. Classified for
    # completeness rather than for effect, like ``subagents`` above: the leaf is
    # WRITE-protected only (``security.paths._WRITE_PROTECTED_HOME_PATHS``), not on
    # the read-gate floor, so the mask never covers it and neither classification
    # changes what any child can open. The risk it carries is a WRITE (crew-authored
    # markup reaching the operator's dashboard), and the read-only seal above is what
    # answers it.
    "panel-templates",
    # Launch fingerprints and server names: no credential, and the decision it
    # feeds is made by gatewayd outside any sandbox, never by a child reading it.
    "mcp-launch-approvals",
    # Launch trees and records contain no credential. Their integrity is enforced
    # by the read-only mount; foreign harnesses may read the resolved package tree.
    "mcp/resolved",
    # A random id with no host, user or path in it, already published on
    # ``/api/health``. Its risk is a write, answered by the read-only seal.
    "gateway_id",
    # The skill projection's side files: view names, agent names, source paths and
    # digests, no credential. An in-sandbox resolver may read a record to map a
    # stored view name to its agent; the risk is the WRITE that would forge that
    # mapping, answered by the read-only seal. Off the read-gate floor, so the mask
    # never covered it.
    "skill-projection",
)


def crew_host_runtime_leaves() -> tuple[str, ...]:
    """Crew-home leaves an ENFORCED harness's child may read, per this module.

    :data:`_CREW_CHILD_READABLE_LEAVES` verbatim -- the half of this module's
    non-hidden crew leaves that is no input to an authorization decision and either
    holds no credential or is one an in-sandbox Crew process cannot boot without: the
    browser launcher, the authorization sidecars (``apps/.dev-grants.json``,
    ``settings_seeds.json``, the model-state pair), the gateway-owned run and decision
    records, the operator's cloud configuration, and the runtime config pair
    (``config.json`` / ``config.local.json``). That last pair is the exception the
    "either" carries: it can hold an inline channel bot token, and stays readable
    because every in-sandbox ``KiroCrewConfig.load()`` depends on it and it was never
    on the read-gate floor to begin with -- see its entry in the list.
    Its sibling :data:`_CREW_CHILD_WITHHELD_LEAVES` carries the rest, and
    ``test_sandbox_governance_mask`` pins the pair complete and disjoint against
    ``_CREW_SANDBOX_VISIBLE_LEAVES | _CREW_READONLY_LEAVES``, so a leaf added to
    either source list must be classified before it can ship.

    Two of the withheld entries are counter-intuitive on their names alone, which is
    why the pin exists rather than a convention: ``member-memory-bindings`` stores raw
    session keys rather than digests, and ``run`` holds the per-listener gateway
    credential next to the launcher. "Sounds like metadata" is not a classification.

    Published for the ONE caller that builds a second, independent mask over the same
    data home -- ``agent_sdk.tool_gate.adapter_hidden_credential_dirs``, the OS
    credential mask an enforced adapter is confined by. That mask projects the whole
    READ-GATE floor, and the floor covers a Crew runtime artifact for a different
    reason than a credential: it stops the AGENT'S OWN FILE TOOLS from opening one,
    while Crew's writers open it directly. Handed to a sandbox as a deny list, the
    same entry hides the artifact from the CHILD -- which is not the reader the floor
    was aiming at, and is the reader these lists exist to serve.

    The governance ceiling is NOT in this set, and that is deliberate rather than an
    omission -- see the governance family in
    :data:`_CREW_CHILD_WITHHELD_LEAVES`. It is the case where the two readers pull
    hardest in opposite directions: an empty bind over ``security_policy.json`` makes
    ``boot_platform()`` raise, so an in-sandbox Crew process under an enforced harness
    stops booting on exactly the governed hosts that set one. That cost is accepted
    because it lands in the safe direction -- a session refuses rather than proceeding
    ungoverned -- and a ceiling is an INPUT TO AN AUTHORIZATION DECISION, which a
    foreign harness's child reads through a channel that reaches no gate and leaves no
    record. So do not read the paragraph above as licence to move a ceiling or a
    consent record here to make a harness boot.

    Subtracting these is not a hole. An unenforced harness's child already sees every
    leaf that remains (those harnesses get no mask at all), each readonly entry is
    independently re-sealed read-only by ``wrap_argv``, and the read gate still fences
    all of them from the agent's own file tools. The two controls keep covering
    different readers.

    Derived from the lists, never re-spelled. A hand-copied list would drift the
    moment a leaf is added above, and the drift is silent in the safe-looking
    direction: the new leaf keeps its mask entry and the enforced harnesses alone
    lose it.
    """
    return _CREW_CHILD_READABLE_LEAVES
