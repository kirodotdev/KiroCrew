"""The crew-home leaves the launcher may create or pin before it seals them.

``mount(2)`` cannot target an absent path, so a seal of an absent ceiling or mask target
is skipped unless the leaf is materialised first; these tables say which leaves may be
created (and with what empty content) and which are pinned without following a link.
One of the owners ``kiro_crew.sandbox`` is carved into: every name here stays readable
and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

from kiro_crew.sandbox_leaves import (
    _AUTH_STORE_STAGING_LEAF,
    _LIVE_TARGET_STAGING_LEAF,
    _MD_NOTEBOOK_RETIRED_STATE_LEAVES,
    _MD_NOTEBOOK_STAGING_LEAF,
    _MD_NOTEBOOK_STATE_LEAVES,
    _SECRET_REQUEST_POLICY_LEAF,
    MD_NOTEBOOK_APP_NAME,
)

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
#:    * ``memory_stores`` — an empty root declares or provisions no store. The
#:      gateway can later create named stores inside the directory bind; no
#:      database, member configuration or Global V1 path is created here;
#:    * ``playwright-cli`` — an empty dir means the launcher is absent,
#:      exactly as a missing dir does; its directory bind shows a later gateway
#:      install while withholding every agent-side write;
#:    * ``computer_use.json`` — ``computer_use.enable_state.load_state`` reads ``{}``
#:      as DISABLED, which is what an absent keystone means;
#:    * ``oauth_endpoints.json`` — ``security._validate_operator_oauth_entries``
#:      extends trust by nothing for ``{}``;
#:    * ``aws_service_consent.json`` — ``aws_consent._read_all`` returns ``{}`` for
#:      both absent and empty, so every service stays unconfirmed;
#:    * ``decisions_consent.json`` — ``decisions.consent.load_state`` reads ``{}``
#:      as NOT CONSENTED, which is what an absent keystone means;
#:    * ``settings_seeds.json`` — ``acp.seed_provenance._load`` finds no ``seeds``
#:      mapping in ``{}`` and returns having recorded nothing, so every settings
#:      path reads as unowned. Identical to absent, and the leaf that most needs
#:      materialising: it is written only once a claude-agent-acp session has
#:      actually seeded a work dir, so on every install that has not it is exactly
#:      the absent-and-therefore-writable name this list exists to close.
#:
#: 2. A STALE read of that empty document must fail toward refusal. The seal is a
#:    bind mount, which pins the INODE for the sandbox's lifetime, while every
#:    dashboard writer publishes through ``atomic_write`` (temp + rename), i.e. a NEW
#:    inode. So a sandboxed reader keeps seeing the empty document even after the
#:    operator writes the real one. For the three JSON files above that freezes them at
#:    "disabled" / "no consent" / "no extra endpoints" — narrower than the truth, and
#:    for ``settings_seeds.json`` at "Crew owns no settings file", so the writer takes
#:    its leave-it-alone branch: the seed is not refreshed, and nothing is overwritten
#:    or unlinked. The empty ``profiles`` dir is exempt from the concern entirely: a
#:    directory bind shows live contents, so a profile added later is visible.
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
#: The same ``mount(2)`` limit leaves the ``sensitive_dirs`` / ``sensitive_files``
#: mask loops skipping their own absent targets. That is a real sibling gap, not one
#: this list closes: a mask needs the opposite treatment (an empty bind OVER the
#: name), and ``_CREW_HIDDEN_LEAVES`` has no reader to prove an empty document is
#: absent-equivalent, so each leaf needs its own argument.
_CREW_PRECREATE_READONLY_DIR_LEAVES: tuple[str, ...] = (
    # The app-agent launch directory. Absent, the seal is skipped and a sandbox could
    # create it and plant the spec a launch copy's kiro-cli would read; its only
    # writer (``launch_process_cwd``) accepts the existing empty 0700 directory.
    "app-agent-launch",
    # Empty roots confer no run authority; directory mounts expose later
    # gateway records without letting a sandbox create the missing root.
    "subagents",
    "member-memory-bindings",
    "memory_stores",
    "profiles",
    # The crew webview template directory. A fence only fences an EXISTING path:
    # the Linux launcher skips the read-only mount for an absent target, so on a
    # fresh install -- where no operator has dropped an override yet -- the
    # directory does not exist, the seal is silently skipped, and the agent can
    # create it and author its own template. Which is precisely the write the
    # read-only listing above exists to deny, so without this entry that listing
    # protects only hosts that happen to have the directory already.
    "panel-templates",
    "playwright-cli",
    # The KAS engine bundle root, for the ``playwright-cli`` reason: on an install
    # that has never run ``kirocrew kas install`` the directory is absent, and an
    # absent target is a seal skipped and a name the agent may create.
    "engines",
    # The decision log, on the ``profiles`` argument rather than the JSON one.
    # (1) An EMPTY dir means what an ABSENT dir means to its only reader:
    # A reader resolves a day-file by name and skips one that is not there, so no
    # day-files is no rows either way. (2) A stale read cannot arise, because a
    # directory bind shows LIVE contents -- a day-file the gateway writes later is
    # seen, not frozen out. Without this entry the seal skips the absent directory,
    # which is the state of every install that has never sampled a decision, and
    # leaves exactly the name an agent would create in order to forge a verdict.
    "decisions",
    # Downloaded model weights, on the same argument. (1) Every reader resolves a
    # weight or decoder FILE by name and treats a missing file as not installed, so
    # an empty directory reads as no model, exactly as an absent one does. (2) The
    # directory bind is live, so a file the gateway downloads later is seen. Without
    # this entry the seal skips the directory on every install that has not finished
    # its first download, which is the window a sandboxed shell would use to swap
    # the bytes the loaders verify.
    "models",
    # The redaction allow-list, on the same argument: no file means no host
    # allowed, which is what an empty directory means too, and the bind is live.
    "redaction-allow",
    # Pi's gate launcher and sealed extension. Materialised here rather than only by
    # the adapter so the directory is a read-only mountpoint before ANY sandbox starts,
    # including the first pi spawn on a fresh install.
    "pi-gate",
    # KAS MCP launch snapshots: the launcher reads a snapshot by name and refuses a
    # missing one, so an empty directory means what an absent one does, and the
    # bind is live, so a snapshot the gateway writes later is seen.
    "kas-mcp-launch",
    # Empty directories are absent-equivalent to the approval and resolution readers.
    # Pre-creation gives Linux concrete bind targets on a fresh install.
    "mcp-launch-approvals",
    "mcp/resolved",
    # The per-template MCP additions record, on the same argument: an absent record
    # reads as no additions, so an empty directory is the same answer, and the bind
    # is live, so the gateway's first write is seen. Without this entry the seal
    # skips the directory until that write, which is the window a sandboxed shell
    # would use to plant a record the next rebuild merges.
    "template-mcp-additions",
    # The skill projection's side files, on the ``profiles`` argument. (1) Every reader
    # resolves a record, a lease or the ledger by name under it and treats a missing
    # one as absent, so an empty directory is no record, no lease and no ledger,
    # exactly as an absent one. (2) The bind is live, so the records the gateway
    # writes on the first spawn are seen. Without this entry the seal skips the
    # directory on every install until that first spawn, which is the window a
    # sandboxed shell would use to plant the record a later resume resolves through.
    "skill-projection",
    # The settings-seed provenance directory, on the ``profiles`` argument. (1) Its
    # reader (``acp.seed_provenance._read_disk_seeds``) reads a missing sidecar file
    # as no records, exactly as it reads an absent directory. (2) The bind is live,
    # so every later seed write is seen.
    # Without this entry the seal skips the directory until the first seed, and the
    # name is one an agent could create to forge the record.
    "seed-provenance",
)
#: Read-only directory leaves whose NAME must remain the mounted name. A resolving
#: symlink is unsafe here: the mount follows its target and leaves the lexical name
#: replaceable, which would let an agent choose the executable the gateway runs --
#: or, for ``decisions``, keep writing the log the owner reads verdicts from after
#: its target was sealed instead of the leaf. Every seal here is the load-bearing
#: half of a control, so none may be satisfied by a link the governed party planted.
_CREW_NOFOLLOW_READONLY_DIR_LEAVES: tuple[str, ...] = (
    "app-agent-launch",
    "playwright-cli",
    "engines",
    "subagents",
    "member-memory-bindings",
    "decisions",
    "models",
    "redaction-allow",
    "pi-gate",
    "kas-mcp-launch",
    "mcp-launch-approvals",
    "mcp/resolved",
    "template-mcp-additions",
    "skill-projection",
    "seed-provenance",
)
assert set(_CREW_NOFOLLOW_READONLY_DIR_LEAVES) <= set(_CREW_PRECREATE_READONLY_DIR_LEAVES)
#: Read-only FILE leaves whose NAME must remain the sealed name, for the same reason
#: as the directory list above and needing its own entry because the file loop in
#: ``sandbox_seals._materialize_sealable_ceilings``
#: only WARNS on an alias where the directory loop REFUSES. A bind mount seals the
#: link's REFERENT, so a leaf that resolves leaves the lexical name replaceable in a
#: writable parent: a sandboxed process unlinks it and drops its own file there, and
#: the seal is intact around a name that now means something else. For ``cloud.json``
#: that name decides which container image a Fargate launch runs, and the task's
#: execution role delivers the model credential into it.
_CREW_NOFOLLOW_READONLY_FILE_LEAVES: tuple[str, ...] = ("cloud.json",)

#: Every strict no-alias crew-home leaf a DELEGATED spawn's workspace must not overlap, with
#: the wording that names which seal an operator is looking at. The refusal's target list is
#: DERIVED from these keys rather than from one of the two lists above, because a list-shaped
#: derivation covers one SHAPE and leaves the other uncovered with nothing saying so: a leaf
#: sealed as a DIRECTORY is not reached by a derivation over the FILE leaves, and a workspace
#: set to it keeps the write its seal denies. The assert below makes drift loud in both
#: directions -- a sealed leaf with no wording, and wording for a leaf nothing seals.
_DELEGATED_OVERLAP_LEAF_REASONS: "dict[str, tuple[str, str]]" = {
    "cloud.json": (
        "sealed cloud configuration",
        "the agent could choose the container image a Fargate launch runs, and the task's "
        "execution role delivers the model credential into it",
    ),
    "playwright-cli": (
        "sealed browser runtime",
        "the agent could replace the browser executable the gateway runs",
    ),
    "engines": (
        "sealed agent engine",
        "the agent could replace the KAS engine code and Node runtime every later session runs",
    ),
    # These two are gateway-owned run records: results stay readable, and agent code must
    # not rewrite the app owner a continuation restores its authorization from. The wording
    # follows _CREW_READONLY_LEAVES' own note on them. Every leaf sealed on either nofollow
    # list needs an entry here, which is what the assert below enforces.
    "subagents": (
        "sealed run records",
        "the agent could rewrite the app owner a cold continuation restores its "
        "authorization from",
    ),
    "member-memory-bindings": (
        "sealed run records",
        "the agent could rewrite the app owner a retained V1 run restores its "
        "authorization from",
    ),
    # Sealing the seam that turns the feature on without sealing the record it writes
    # would be half a control: appending one feedback row is enough to put a verdict
    # in the owner's own summary. Every legitimate writer is the gateway, outside the
    # sandbox.
    "decisions": (
        "sealed decision log",
        "the agent could append a feedback row the owner's summary counts as a "
        "verdict nobody gave",
    ),
    "models": (
        "sealed model weights",
        "the agent could swap the weights a digest-pinned loader verifies before it "
        "reopens them by name",
    ),
    "redaction-allow": (
        "sealed redaction allow-list",
        "the agent could allow the host it wants to send conversation data to",
    ),
    "pi-gate": (
        "sealed pi gate runtime",
        "the agent could plant the launcher a later pi session execs out of",
    ),
    "kas-mcp-launch": (
        "sealed KAS MCP launch snapshots",
        "the agent could choose the command and environment a later MCP server start runs",
    ),
    "mcp-launch-approvals": (
        "sealed MCP launch approvals",
        "the agent could approve a command the gateway launches outside the sandbox",
    ),
    "mcp/resolved": (
        "sealed resolved MCP launches",
        "the agent could replace the executable the gateway substitutes for an approved launch",
    ),
    "template-mcp-additions": (
        "sealed template MCP additions",
        "the agent could record a server of its choosing that the next rebuild mounts",
    ),
    "skill-projection": (
        "sealed skill-view records",
        "the agent could forge the record that maps a stored skill-view name to the agent "
        "a later resume runs as",
    ),
    "seed-provenance": (
        "sealed settings-seed record",
        "the agent could forge the record that lets Kiro Crew overwrite and delete a "
        "settings.local.json the user wrote",
    ),
    "app-agent-launch": (
        "sealed app-agent launch directory",
        "the agent could plant the agent spec kiro-cli runs for an app's own agent",
    ),
}
assert set(_DELEGATED_OVERLAP_LEAF_REASONS) == set(_CREW_NOFOLLOW_READONLY_FILE_LEAVES) | set(
    _CREW_NOFOLLOW_READONLY_DIR_LEAVES
)

_CREW_PRECREATE_READONLY_FILE_LEAVES: tuple[str, ...] = (
    # The launch record. Criterion 1 (an EMPTY document means what an ABSENT one means) is
    # met by ``LaunchState.load`` treating a document carrying none of its three keys as no
    # record at all and consulting the legacy fields, which is exactly what it does for an
    # absent file -- so a pre-created ``{}`` cannot strand an install whose pointer still
    # lives in ``cloud.json``. Criterion 2 (a stale sealed read fails toward refusal) holds
    # too: a sandboxed reader frozen at ``{}`` sees no tag and the command exits with "no
    # previous launch found" rather than acting on one, which is narrower than the truth.
    "cloud_launch_state.json",
    # The microvm crew records, pre-created for the reason this list exists: the file does
    # not appear until the lane's first launch, and ``mount(2)`` cannot seal a name nothing
    # occupies -- so without this the leaf stays WRITABLE in-sandbox on every install that
    # has not launched a microvm crew yet, which is most of them.
    #
    # Criterion 1 (an EMPTY document means what an ABSENT one means) holds on BOTH of
    # this store's readers, which is what the criterion asks and what the tolerant read
    # alone does not establish: ``CrewStore.load`` answers ``{}`` for either, and
    # ``load_for_write`` accepts exactly ``{}`` as the pre-created store rather than
    # refusing it for carrying no ``crews`` list -- without which the first launch on a
    # sealed install would be the one launch that could not start. Criterion 2 (a stale
    # sealed read fails toward refusal) holds as well: a reader frozen at ``{}`` finds no
    # crew and answers "no such crew" rather than acting on one.
    "microvm_crews.json",
    "computer_use.json",
    "oauth_endpoints.json",
    "aws_service_consent.json",
    "decisions_consent.json",
    # Consent to forward SSH_AUTH_SOCK: ``ssh_auth_sock_consent.is_granted``
    # reports no consent for an absent, empty, AND unreadable store alike, so a
    # pre-created ``{}`` means exactly "not forwarded" (criterion 1); the writer
    # publishes through ``atomic_write`` (new inode), so a sandboxed reader frozen
    # at ``{}`` under-reports consent, which is narrower than the truth
    # (criterion 2). Without this entry the Linux mount seal skips the absent leaf
    # -- the DEFAULT before any grant -- leaving it creatable from inside the
    # namespace sandbox.
    "ssh_auth_sock_consent.json",
    # The credential-redaction switch satisfies both criteria the way
    # ``ssh_auth_sock_consent.json`` does: ``redaction_switch.read_state`` reads
    # absent, empty and unreadable alike as ENABLED, so a pre-created ``{}``
    # means exactly what an absent file means (criterion 1); the writer
    # publishes through ``atomic_write`` (new inode), so a sandboxed reader
    # frozen at ``{}`` keeps redacting after the owner switches off, which is
    # narrower than the truth (criterion 2).
    "credential_redaction.json",
    "settings_seeds.json",
    # The operator's grants of ``owner`` trust to a hand-configured app registry.
    # Satisfies both criteria the way ``ssh_auth_sock_consent.json`` does:
    # ``registry._granted_owner_repos`` reads an absent, empty, AND ``{}``-versioned
    # document alike as NO grants (a ``{}`` carries no matching ``version``), so a
    # pre-created ``{}`` means exactly "no registry is owner-trusted" (criterion 1);
    # the writer publishes through ``atomic_write`` (new inode), so a sandboxed reader
    # frozen at ``{}`` keeps every hand-configured registry at the credential-free
    # ``index`` tier even after the operator grants one -- narrower than the truth
    # (criterion 2). Without this entry the Linux mount seal skips the absent leaf --
    # the DEFAULT before any grant -- leaving it creatable from inside the namespace
    # sandbox, which is exactly the write the read-only listing exists to deny.
    "registry_trust.json",
    # The runtime config and its overlay. Criterion 1: ``KiroCrewConfig.load()`` yields
    # the same defaults for ``{}`` as for an absent file, and an empty overlay overrides
    # nothing; the gateway creates ``config.json`` at boot, so in practice only the
    # overlay is ever absent here -- which is exactly the name an agent would otherwise
    # create to win the merge. Criterion 2 is met in the only sense that matters for a
    # settings file: a sandboxed reader that sees an older document sees stale
    # SETTINGS (a crew roster, a probe timeout), never a looser CONFINEMENT, because
    # every decision these files feed that confines the agent -- its sandbox tier, app
    # admission, unsandboxed-exec -- is taken by the gateway outside the sandbox, which
    # reads the live file. Known Linux residual, shared with every leaf on this list:
    # the host publishes each save through ``atomic_write`` (temp + rename), and the
    # kernel detaches a file bind whose target is renamed over from another mount
    # namespace, so the seal holds from spawn until the NEXT gateway save of that file
    # and the name is then unsealed in that sandbox until it respawns. That save is
    # not only the operator's: the gateway saves on its own (boot migrations,
    # member-identity publication, memory-store provisioning) and an in-sandbox
    # agent can reach some of those paths through an ordinary tool call. The fix
    # for all sealed leaves at once is a read-only DIRECTORY bind, tracked separately.
    "config.json",
    "config.local.json",
    # The cloud launcher's config, and the leaf where an ABSENT file is the more
    # dangerous case: with no file there is no seal, so an agent could CREATE the
    # whole ``fargate`` block -- its own image beside the owner's real secret ARNs --
    # and the owner's next launch would deliver the model credential into it.
    # Criterion 1: ``CloudConfig.load()`` returns the same defaults for ``{}`` as for
    # an absent or unparseable file, and ``fargate_config()`` reads ``{}`` as no
    # block, so an empty document means exactly what an absent one means.
    # Criterion 2: a stale sealed read fails toward refusal. A pinned ``{}`` leaves a
    # sandboxed reader seeing no Fargate block and no saved profile even after the
    # operator writes one, so the lane stays UNREGISTERED and an in-sandbox launch
    # reads as unconfigured -- narrower than the truth, never wider.
    "cloud.json",
    # The fork-lineage sidecar satisfies both criteria the way
    # ``ssh_auth_sock_consent.json`` does: ``agent_state._read`` returns ``{}``
    # for absent, unreadable, AND an empty document alike, so a pre-created
    # ``{}`` means exactly "no lineage recorded" (criterion 1); the writer
    # publishes through ``atomic_write`` (new inode), so a sandboxed reader
    # frozen at ``{}`` under-reports fork status — which grants nothing, since
    # the write seal is what withholds forgery and it applies regardless
    # (criterion 2). Without this entry the Linux mount seal skips the absent
    # sidecar — the DEFAULT state before any fork — leaving it creatable from
    # inside the namespace sandbox.
    "agent_model_state.json",
    # The sidecar's advisory lock, same absent-file reasoning: the lock is
    # created on first use, so without pre-creation a sandbox spawned before
    # any lineage write finds it absent and creatable — and a sandbox-created
    # lock is exactly the replaced-inode attack the read-only seal exists to
    # stop. An empty lock file is absent-equivalent by definition: its content
    # is never read, only its identity is locked.
    "agent_model_state.json.lock",
)

#: The one masked leaf that carries its own argument (see the sibling-gap note
#: above): a DIRECTORY the gateway creates on demand, whose EMPTY state is
#: absent-equivalent because nothing but the gateway reads it -- it cuts a fresh
#: per-call subdirectory for one CLI transfer and removes it again. Left to lazy
#: creation, a sandbox spawned on a fresh install before the first transfer finds
#: the target absent, the ``sensitive_dirs`` loop skips it, and the directory the
#: gateway creates later appears INSIDE that running sandbox's view -- where a
#: same-UID agent can swap the transfer's destination for a link. Materialised
#: (empty, 0o700) before every namespace spawn instead, so the mask always has a
#: name to bind over.
#:
#: ``appearance-library`` is here for the same reason with a different consequence:
#: ``dashboard/appearances.py`` builds the crew library on FIRST USE, so an install
#: that has never imported a pack has no directory for the mask loop to bind over,
#: and the first import then creates it visible to every sandbox already running --
#: where a same-UID agent can ``rm -rf`` packs the user cannot get back. The store
#: tolerates finding its root already present and empty.
#:
#: ``quarantined-clones`` shares that first-use shape: the root is built when the
#: first clone is quarantined, so an install that has never had one offers the mask
#: loop no name, and the marker that must outlive an agent's process would be
#: created visible to every sandbox already running. Its resolver treats an
#: existing empty root as usable, and proves the root writable before certifying
#: any clone, so materialising it early changes nothing it relies on.
_CREW_PRECREATE_HIDDEN_DIR_LEAVES: tuple[str, ...] = (
    "aws-control-staging",
    "appearance-library",
    "quarantined-clones",
    # First created by the first member-born session's vouch, so a sandbox spawned
    # before then would otherwise see the directory appear unmasked.
    "vouched-executions",
    # md-notebook's write-staging directory, for the same reason and by the same rule: a
    # direct child of the data home, so the plain ``mkdir`` above is sound. Left to lazy
    # creation, a sandbox spawned before the first state write finds it absent, the
    # ``sensitive_dirs`` loop skips it, and the directory the backend creates later shows
    # up INSIDE that running sandbox — with the PAT staging window in it.
    _MD_NOTEBOOK_STAGING_LEAF,
    # The live-target stub's staging directory, by the same rule: created before the
    # first spawn so the mask has a mount target, and the temp the materialiser stages
    # in it is never visible to a running namespace.
    _LIVE_TARGET_STAGING_LEAF,
    # The auth stores' staging directory, by the same rule. Left to lazy creation, a sandbox
    # spawned before the first key or chain-state write finds it absent, the ``sensitive_dirs``
    # loop skips it, and the directory the gateway creates later shows up INSIDE that running
    # sandbox -- with the signing-key staging window in it.
    _AUTH_STORE_STAGING_LEAF,
    # The mediated-secret request directory, by the same rule: the gateway mints its key
    # (and the owner CLI publishes the policy) after a sandbox may already be running, so
    # the directory must exist, and be masked, before any namespace starts.
    _SECRET_REQUEST_POLICY_LEAF,
    # ``crew-panels`` is the same requirement seen from the mirror side of the
    # ceilings above: a read-only ceiling is materialised so the SEAL can apply,
    # a hidden leaf so the MASK can. The skip lands precisely on a fresh install,
    # where the agent could then create the directory itself and write what the
    # gateway later reads back as authoritative. Same failure the
    # ``panel-templates`` ceiling has, one list over.
    "crew-panels",
    # Panel dismissals, by the same rule and with a sharper version of the same
    # timing: this directory does not exist until the operator dismisses their
    # first card, so on every fresh install the mask loop finds the name absent
    # and skips it. A sandbox spawned in that window can then create the
    # directory itself and forge dismissals the gateway reads back as the
    # operator's, hiding runs from the panel. Materialising it empty gives the
    # bind a target from first boot.
    "panel-dismissals",
    # Append-only per-unit crew logs, and the hazard is the sharpest here: the
    # record is the AUTHORITY a reader trusts instead of re-deriving, and the
    # store creates this root on its first write. A sandbox spawned before that
    # write finds the name absent, the ``sensitive_dirs`` loop skips what does not
    # exist, and the mask is then vacuous for the life of that sandbox -- which can
    # itself create the directory and populate it with entries a conductor would
    # read as the gateway's. Materialising it empty at 0700 gives the bind a name
    # to cover before anything can write one. macOS needs no entry here: a
    # Seatbelt deny is a path rule that holds for a name that does not exist yet.
    "crew-log",
    # The chat_tag grants store writes by atomic rename of a sibling temp, so
    # the whole directory must exist before the isdir-guarded mask loop runs —
    # otherwise the first sandbox spawned before the first grant write sees an
    # unmasked leaf appear later.
    "tag-grants",
    # The pinned-order store is created on the first reorder, so it is
    # materialised here for the same reason as tag-grants.
    "pinned-order",
    # The crewmate-teams store is created on the first team the owner makes, so a
    # sandbox spawned before that finds the name absent and the mask is vacuous
    # for its lifetime; materialised empty at 0700 so the bind always has a
    # target.
    "crew-teams",
    # The crewmate records leaf is created by the first rules, goal or DM-binding
    # write (or by its one-time move from ``trust/``), so it is materialised empty
    # at 0700 for the same reason: an absent name gets no mask.
    "member-records",
    # The cross-process work root, and it needs this entry MORE than the stores
    # above rather than less: it is created lazily by the first ``allocate_work``
    # call, so on a fresh home the leaf is absent at every spawn until some job
    # allocates, the ``isdir``-guarded ``sensitive_dirs`` loop skips what does not
    # exist, and the mask is vacuous for the life of those sandboxes. The
    # ``scratch`` sibling carries no entry because every spawn allocates one, so
    # that root exists by construction; nothing allocates a work directory for a
    # spawn. Left absent, a sandboxed agent could itself create the directory at a
    # key it expects a job to use -- keys are deterministic by design -- and, since
    # the allocation marker that distinguishes this module's own tree is a name in
    # the code, plant that too, so ``allocate_work`` rejoins the planted tree rather
    # than refusing it. Materialised empty at 0700 before every spawn so the bind
    # always has a target.
    "work",
    # The refused-inbound spool root, and its consequence is the sharpest kind: the
    # store is created by its own first write, so on a host where no inbound message
    # has been refused the name is absent, the ``isdir``-guarded ``sensitive_dirs``
    # loop skips it, and the mask is vacuous for the life of every sandbox spawned
    # first. One of those can create the directory ITSELF and leave an entry the
    # gateway reads on the next start and quotes verbatim, as the operator's own
    # words, into a conversation the egress recheck still authorizes. The module's
    # own refusals do not reach this shape -- they refuse a LINK at the directory,
    # the leaf or the lock, and a directory an agent created is a real directory
    # holding a plain single-linked file. Materialised empty at 0700, which is what
    # the spool's own ``restrict_to_owner=True`` writes already require of it; its
    # readers open the leaf inside, so an empty root reads exactly as an absent one.
    "inbound-spool",
    # The secret vault: the vault key and every stored secret. ``SecretVault`` creates
    # this directory on the owner's FIRST stored secret, so on a home with none yet the
    # ``isdir``-guarded ``sensitive_dirs`` loop skips the name, and the key and ciphertext
    # written later appear unmasked inside every sandbox already running. The store makes
    # its key and ciphertext inside an existing directory exactly as inside a new one.
    ".vault",
    # The Kiro sign-in store (access and refresh tokens, in its own vault underneath).
    # ``TokenStore`` makes it on the first Crew sign-in, which is the same lazy shape as
    # ``.vault`` with the same result: a sandbox spawned before that sign-in would see the
    # tokens appear unmasked. ``make_owner_only_dir`` accepts the existing 0700 directory.
    "kas",
    # The rest of the mask list's direct-child directories whose store creates them on
    # first write rather than at gateway start. Each store treats an empty root exactly
    # as an absent one: it mkdirs with ``exist_ok`` before writing, and every reader
    # keys on the files inside, never on the directory itself.
    # ``test_sandbox_precreate_secret_dirs`` pins that every directory leaf on a mask
    # list is either here or made at boot.
    "ledger",
    "work-ledger",
    "cron-running",
    "workflow_library",
    "routing",
    "webhooks",
    "mcp-apps",
    "whatsapp",
    "backup",
    # The Web Push stores, written lazily on the first subscribe or key fetch; see
    # its entry in ``_CREW_HIDDEN_LEAVES``.
    "web-push",
    # The governance cache, masked in every tier and lifted READ-ONLY for an enterprise
    # sandbox (``sandbox_tiers.enterprise_policy_cache_view``). Written on the first
    # successful fetch, so a sandbox spawned before then would find no name to mask or
    # seal and could create the directory and plant a ceiling there. An empty directory
    # is no cache to every reader (``read_cache_meta`` finds no document), and
    # ``make_owner_only_dir`` accepts the existing 0700 directory.
    "policy_cache",
)

#: The masked md-notebook leaves materialised before a namespace spawn, and what each
#: holds. This is the per-leaf argument the sibling-gap note above asks for: ``mount(2)``
#: cannot mask an absent path and the ``sensitive_files`` loop guards on ``isfile``, so an
#: ABSENT leaf gets NO mask, and a namespace that outlives the leaf's later creation reads
#: the real bytes — the PAT among them. That was vacuous while nothing could create these
#: files on a sandboxed host; the backend carve-out
#: (:func:`app_backend_visible_targets`) makes creation possible, so the mask has to be
#: made non-vacuous first. Each document is its reader's absent-equivalent, and the
#: md-notebook backend is the only reader:
#:
#:   * ``vaults.json`` — ``_read_vaults_sync`` returns ``[]`` for absent (OSError) and
#:     for ``[]`` alike;
#:   * ``settings.json`` — ``_load_settings_sync`` returns the defaults for absent and
#:     reads ``{}`` as every-field-default;
#:   * ``pat`` — ``_read_pat_sync`` maps absent (OSError) and empty (``"" or None``) both
#:     to ``None``.
#:
#: The STALE-read criterion that excludes most hidden leaves does not arise: the agent's
#: view is the pinned empty MASK file either way, never this document, which is exactly
#: the mask's intent.
#:
#: The files now sit INSIDE the wholly-masked state directory, which gets its own mount
#: target from :data:`_CREW_PRECREATE_HIDDEN_DIR_LEAVES`. Each still gets a regular-file
#: stub, so a link or special file at a state name is refused before launch, and the
#: gateway's legacy move (``md_notebook_state``) treats these exact bytes as "no state".
#:
#: The retired bare names get the same stubs, so their masks always mount: an absent bare
#: name would let an agent create a file or a link there, and a link at a masked leaf
#: refuses every later spawn. The legacy move skips a stub source, so a stub is never moved.
_MD_NOTEBOOK_PRECREATE_CONTENT: dict[str, bytes] = {
    f"{_MD_NOTEBOOK_STAGING_LEAF}/pat": b"",
    f"{_MD_NOTEBOOK_STAGING_LEAF}/vaults.json": b"[]\n",
    f"{_MD_NOTEBOOK_STAGING_LEAF}/settings.json": b"{}\n",
    f"workspace/{MD_NOTEBOOK_APP_NAME}/pat": b"",
    f"workspace/{MD_NOTEBOOK_APP_NAME}/vaults.json": b"[]\n",
    f"workspace/{MD_NOTEBOOK_APP_NAME}/settings.json": b"{}\n",
}
assert set(_MD_NOTEBOOK_PRECREATE_CONTENT) == set(_MD_NOTEBOOK_STATE_LEAVES) | set(
    _MD_NOTEBOOK_RETIRED_STATE_LEAVES
)

#: The masked live-target pointer materialised before a namespace spawn, and what it
#: holds. Same gap as :data:`_MD_NOTEBOOK_PRECREATE_CONTENT` closes, arrived at from the
#: opposite direction: the ``sensitive_files`` loop guards on ``isfile``, so an ABSENT
#: pointer gets NO mask, and a namespace that outlives the pointer's later creation sees
#: the real file in a directory it can write. For md-notebook that gap leaks a secret; for
#: this leaf it hands over a code-execution input — the pointer names the checkout the
#: gateway ``execve``s into, so an agent that writes one chooses what the host runs next.
#:
#: That gap was never vacuous for THIS leaf: the crew data-home root is writable in every
#: sandbox (an in-sandbox ``atomic_write`` stages its temp there), so with the pointer
#: absent an agent shell can simply create one. The pointer's legitimate writer is now the
#: gateway process (Dev Fleet's in-gateway cutover route); the sandboxed backend and its
#: build children are readers at most, and only through that route.
#:
#: A DIRECT child of the data home, so :func:`_publish_empty_ceiling` needs no
#: intermediate-component walk — the hazard :func:`_materialize_md_notebook_mask_targets`
#: guards against (an agent-writable ancestor swapped for a link) has no path here.
#:
#: The document is the pointer's absent-equivalent by construction rather than by
#: coincidence: ``live_target.NO_TARGET_DOCUMENT`` is defined for this purpose and
#: ``read_target_reason`` answers ``(None, None)`` for it, exactly as for an absent file —
#: no boot warning, no "unusable pointer" in the fleet view. Spelled as a literal here for
#: the reason the leaf name is (this module does not import the config-loader chain);
#: ``test_sandbox_dev_fleet_live_target.py`` pins the two equal.
_LIVE_TARGET_PRECREATE_CONTENT: bytes = b'{\n  "checkout": null\n}\n'

#: What a materialised ceiling holds — the empty JSON object every reader above
#: already treats as its absent default. NOT a zero-byte file, which is not valid
#: JSON and would read as CORRUPT rather than as absent.
_EMPTY_CEILING_DOCUMENT: bytes = b"{}\n"

#: Prefix of the in-flight temp ``_publish_empty_ceiling`` stages in its target's
#: parent. Named so a sweep of that directory can tell a gateway-owned temp mid-publish
#: from an orphan it is meant to remove.
_CEILING_TEMP_PREFIX: str = ".kirocrew-ceiling-"
