# Claude Code provider — a selectable ACP harness

## Public provider boundary

`AgentConfig.provider` admits only the ACP provider, and
`KiroCrewConfig.create_provider_factory()` constructs `AcpProvider`. Harness
choice is a separate field, `agent.acp_backend`. The public baseline currently
selects kiro-cli (`ACP_BACKEND_KIRO`, the empty string), Claude, KAS, Codex,
OpenCode, Pi, goose and DeepSeek -- every id in `ACP_BACKENDS_KNOWN`. DeepSeek was
the one exception until Crew's gate plugin routed its tool calls through the host
permission gate (`Routing.VERIFIED_GATE_EXTENSION`, `agent_sdk/backends.py`);
`test_baseline_ships_every_known_backend` pins the baseline as
`ACP_BACKENDS_KNOWN - NOT_SHIPPED_SELECTABLE`, and that allowlist is empty again.

`DefaultProviderRegistry` registers no extra backend. The protocol hook remains
for editions, but `register_selectable_backend` accepts only a core-known harness
whose `ACP_BACKEND_ROUTING` verdict is not `UNVERIFIED`; it refuses both unknown
ids and known-but-ungated harnesses. Adding a genuinely new harness therefore
requires core definitions and an established routing contract, not just a call to
the register. Once accepted, registration reaches the config gate, dashboard
PATCH allowlist, and `GET /api/config/schema` together
(`test_a_registered_backend_reaches_the_allowlist`,
`test_a_registered_backend_reaches_the_schema_endpoint`).

`acp_backends.resolve_selected_backend()` normalizes an `agent.acp_backend` value
this deployment cannot select to the Kiro harness. This boundary is load-bearing:
`AcpProvider` rejects unknown harnesses, so normalization prevents a persisted or
hand-edited value from becoming a startup failure.
`TestConfigRoundTrip.test_unselectable_values_degrade_to_the_default` exercises
that path, and `test_harness_parity.test_unselectable_backend_degrades_to_kiro`
asserts the outcome against the live registry rather than a hardcoded verdict —
which is why `claude` now *survives* that gate instead of degrading.

Selectable is not the same as usable, and it is not the same as permitted.
Whether a *deployment* may pick a registered harness is answered by the
`agent_backend` governance scope narrowing the registry
(`apply_selectable_denials`, floored at `GOVERNANCE_FLOOR_BACKEND` = kiro-cli).
Whether a *machine* can run it is answered by `agent_sdk.probe_backend`.

## The Claude harness

`acp/harness/claude.py` owns the whole Claude launch, and it is a live path on a
plain public build:

- `AcpClient._is_claude` recognizes `ACP_BACKEND_CLAUDE`, and `AcpClient._spawn`
  hands the launch to that host's adapter, `ClaudeLaunch`
  (`acp.harness.process_adapter_for`).
- `_resolve_claude_acp_bin()` finds the `claude-agent-acp` Node entry script and
  returns `(argv, searched_path)`; the result is memoized process-wide in
  `_claude_acp_argv_cache`, so the search runs once and the "not found" message
  names exactly the directories that were searched.
- `_resolve_claude_code_executable()` finds the `claude` CLI and the adapter's
  `apply_spawn_env` exports it as `CLAUDE_CODE_EXECUTABLE` when the caller has not
  set one. The adapter
  forwards it to `@anthropic-ai/claude-agent-sdk` as
  `pathToClaudeCodeExecutable`; without it the SDK fails `session/new` with
  "Claude native binary not found", because it does not search `PATH` for
  `claude` on its own.
- The adapter is a **public** npm package, `CLAUDE_ACP_NPM_PKG =
  "@agentclientprotocol/claude-agent-acp"`. Nothing on this path is
  edition-private.

Availability is therefore a property of the operator's machine, not of the build:
Claude Code needs **two** locally-installed binaries, the `claude-agent-acp`
adapter and the `claude` CLI. `agent_sdk.backend_install._probe_claude` reports
which of the two is absent (`COMPONENT_CLAUDE_ACP_ADAPTER`,
`COMPONENT_CLAUDE_CODE_CLI`) plus the command that installs the adapter, so a
half-install does not read as a total one. A probe that itself fails reads
`UNKNOWN`, never `MISSING`.

Both operator-facing surfaces read that one verdict:

- `kirocrew doctor` prints Claude Code as an optional backend — present, absent
  with the missing components named, or uncheckable. It is never a hard failure;
  kiro-cli is the floor.
- The dashboard's agent-backend control **hides** a harness the deployment may
  not select instead of dimming it, because under a managed policy there is
  nothing the reader can do about it and advertising a forbidden option is the
  opposite of what a restriction is for. The currently-selected value is always
  kept visible. A rendered-but-disabled row therefore always names something the
  user can act on: install a binary, or restart the gateway. Covered by
  `hides a backend the deployment may not select, rather than dimming it`,
  `keeps the selected backend visible even if it reads as unselectable` and
  `saves the Claude Code selection the shipped build offers`.

### What Crew gates on this harness, and what a pre-approval skips

Start from what is NOT broken, because the difference is narrow and easy to overstate.

**By default, Claude asks and Crew decides.** In `default` permission mode with no
matching rule, every tool call reaches the SDK's `canUseTool` callback — which is
exactly what `claude-agent-acp` turns into ACP `session/request_permission`. That
arrives as a `permission_request` event and runs Crew's own approval path:
`hooks.on_tool_call`, its deny rules, its sensitive-path and write-protected-config
checks, and its SEL decision record. A Claude session is governed like any other on
that path.

**What escapes is a call that was already pre-approved, because it never asks.** The
SDK evaluates permissions in a fixed order and `allow` rules sit at step 5, ahead of
the callback at step 6. Anthropic's documentation states the consequence in bold:
*"Auto-approved tools never reach `canUseTool`."* No callback means no ACP request,
so for that specific call there is nothing for Crew to gate or record. The same holds
for `bypassPermissions` and for `acceptEdits` on the operations it covers.

**Why that matters here rather than being purely the operator's own choice.** Those
rules do not have to come from the operator. The SDK reads `.claude/settings.json`
from the **project directory** — the `project` setting source is enabled for default
options — so a cloned repository can carry allow rules its author wrote. Crew's public
core adds no `PreToolUse` hook and keeps every setting source for a session whose
settings file it authored, so it closes none of it there. For a project that owns its
own `settings.local.json`, it loads the `user` source alone (see "A project-owned
settings file" below). What Crew does write is a session-scoped
`.claude/settings.local.json`, and only while it can prove the file still holds the
bytes it wrote; that seed carries deny rules translated from the spec's
`disabledTools` and never merges into a foreign project settings file.

A session can now OPEN it further, and only on purpose. `KIROCREW_CC_PERMISSION_MODE=auto`
resolves through `agent_sdk.backends.resolve_cc_permission_mode` and is seeded as
`permissions.defaultMode`, which puts Claude's own classifier in charge of approving
tool calls: an approved call asks nothing, so it reaches no `hooks.on_tool_call` and
writes no SEL record, exactly as a project allow rule does. Only the literal value
`auto` resolves, a backend that seeds no settings file resolves to nothing, and with
no opt-in no `defaultMode` is written at all. An explicitly requested
`bypassPermissions` mode pre-approves by design. The audit half of that trade is
tracked in #12744.

This is documented, intended Claude Code behaviour, not a defect introduced by making
the harness selectable — the harness was already implemented and reachable by any
edition that registered it. But it means the guarantee differs per harness, so the
dashboard states the difference on the Claude row
(`claude_uses_its_own_permissions`) rather than leaving an operator to discover it
from a shell command that never asked. Kiro CLI and KAS have no equivalent
settings file that can pre-approve past Crew's gate, and deliberately carry no such
line.

Anthropic documents two mechanisms that would close even the pre-approved case — a
`PreToolUse` hook, which runs before every other step and whose deny holds even in
`bypassPermissions` mode, and excluding `project` from `settingSources`, which stops
the untrusted copy being read at all. `claude-agent-acp` forwards the second: it
spreads `_meta.claudeCode.options` over its own `settingSources: ["user", "project",
"local"]` default, verified against release **0.84.0** (`dist/acp-agent.js`). The
project-owned settings path below relies on it, so that path is floored at
`CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION` (0.84.0), twice. Before spawn, the installed
package's own `package.json` version decides whether the path is taken at all. After
`initialize`, the reported `agentInfo.version` must agree, or the session is stopped
before its first prompt. Below the floor, or with no readable version, the project file
stays loaded and the array stays withheld. The forwarding
itself is not read back; the floor is the evidence. Whether it forwards a `PreToolUse` hook is still not answered
in this repository, and that is the prerequisite for Crew gating *every* Claude tool
call rather than every call Claude asks about.

### MCP tools on a Claude session

The `claude-agent-acp` adapter reads **no** agent file: the `mcpServers` array on
`session/new` / `session/load` is the only channel Crew has onto a Claude session's
MCP surface (the harness mounts its own user- and project-scope `mcpServers` and
plugins beside it), and an empty one means zero Crew tools — `kirocrew-core`, cron and every
user-configured server absent, while the harness itself (prompts, streaming, model
and effort selection, the full `session/request_permission` flow) works. So
`_session_mcp_servers()` fills it, from `acp/session_mcp.py`:

- **The materialized kiro agent spec is the source.** `~/.kiro/agents/<name>.json`
  is already the merge point for the dashboard's Kiro Crew scope
  (`~/.kiro/crew/mcp.json`) and kiro's global `mcp.json`, so a user-installed
  server needs nothing CC-specific. There is no second, CC-shaped registry to keep
  in sync, and the spec is read **per spawn** — installing or toggling a server
  takes effect on the next session, with no gateway restart. The read goes through
  `agent_discovery._read_agent_spec`, the repo's one hardened reader, labelled
  `operation="session_mcp_servers"`: the agents directory is user-writable and
  shared with other tools, so a symlink whose resolved target is sensitive is
  refused and audited, and an oversized file is refused at the size cap rather
  than being read into memory mid-spawn.
- **`tools` references decide what mounts.** kiro-cli mounts a server only when
  `tools` names it (`@server` or `@server/tool`); the CC array has no such
  indirection, so the reference is applied during translation. Without that, an
  entry deliberately left unreferenced — the shape every `opt_in` grant uses, and
  what a hand-narrowed spec looks like — would come alive the moment a session ran
  on CC.
- **kiro-cli's registry filter is SYMMETRIC, and only one half of it is
  reproducible here.** Outside registry mode kiro-cli drops the entries that
  *carry* `type: "registry"`, and that half is mirrored exactly: the marker is on
  the entry, so the decision needs nothing else. Inside registry mode kiro-cli
  resolves each marked entry against the **admin's catalog** by map key, drops
  what the catalog omits, and applies the catalog's own command override — and
  none of that is available here, because only kiro-cli fetches the registry URL
  and it persists neither the URL nor the catalog. A marked entry therefore
  cannot be positively *authorized* on this backend, and a `"type": "registry"`
  line is one any user can add to their own spec, so in registry mode marked
  entries are **withheld** rather than launched. The unmarked ones are dropped for
  the reason they always were, which leaves a governed install with nothing from
  the spec at all on this harness.
- **Crew's own control plane is exempt from that withholding**, because it is
  re-derived from `agent.managed_mcp_spec_entry` rather than read from the
  user-editable spec: it is the host's own process, and withholding it would leave
  the session unable to report back to its channel — the exact defect this seam
  exists to fix, on precisely the installs that are most governed. The residual
  difference from kiro-cli is stated for what it is: an administrator who omits
  `kirocrew-core` from the catalog has it dropped there and kept here (one
  host-owned server wider), while every third-party server goes the other way.
- **A server the MCP gateway brokers yields to its stub, and the MIRROR places
  the stubs.** The pooled broker emits a stub under the SAME name as the
  agent-spec entry it rewrites. Emitting both would put two elements with one
  `name` in a single array: either the raw entry shadows the stub and the
  session bypasses the broker, or both register and every pooled backend runs
  twice (#927). The client resolves `injection_server_names` for its overlay and
  passes the set down; an unreadable overlay degrades to "no stubs", never to a
  session with no servers. The stub ELEMENTS come down the same call
  (`stub_elements`) and the mirror appends them itself -- the client's shared
  append (`_pooled_mcp_servers`) is inert for every mirrored backend -- so two
  of the rules that narrowed the translated half cover the stubs: a stub for a
  server this agent's `tools` never references is not mounted (the gateway
  overlay is written per agent from the GLOBAL settings file as well as the
  agent's own spec, so it can carry exactly that stub), and a session whose
  permission surface Crew does not own gets no stubs along with no array. The
  registry ceiling reaches the stubs through that same call: both
  `pooled_session_servers` and `injection_server_names` withhold a stub under
  registry mode, because a stub is by construction an UNMARKED entry -- the
  rewriter refuses to wrap a `type: "registry"` one -- and nothing here can
  resolve a name against the admin's catalog. Holding the line at that one
  overlay read covers every mirror that mounts `stub_elements`, codex included,
  and any future one; it is a no-op for the kiro-cli path, which drops an
  unmarked injected entry under registry mode by itself. Crew's control plane is
  exempt there on the same grounds it is exempt above, and the two functions must
  agree, or a name in the set with no element behind it withholds the spec's only
  copy of that server. A stub
  for a spec-narrowed server stays mounted, unlike codex: the narrowing rides
  `permissions.deny` in `settings.local.json`, which matches the stub because it
  registers under the same server name.
- **Crew's control plane is re-derived, not copied.** `kirocrew-core` and
  `kirocrew-cron` come from `agent.managed_mcp_spec_entry`, so a stale hand-edited
  command in the spec cannot cost a session the tools it needs to report back at
  all. A missing or malformed spec degrades to the control plane alone; nothing in
  the path raises, because a bad element fails the whole `session/new`.
- **`autoApprove` is not translated.** Its nearest CC equivalent is a
  `permissions.allow` entry, and a pre-approved tool is one Claude never asks
  about — so the call would never reach the host gate that carries the deny floor,
  the sensitive-path check and the governance ceiling. Every MCP call on this
  backend stays gated. `timeout` is kiro-only and dropped.
- **`disabledTools` becomes a `permissions.deny` rule.** It cannot ride along in
  an array element either, but it is a RESTRICTION: dropping it while forwarding
  the server it narrows would silently widen the session's tool surface, which is
  what the dashboard writes that key to prevent. `session_mcp_deny_rules` turns
  each entry into `mcp__<server>__<tool>` and the settings writer merges those
  into `permissions.deny`, which CC evaluates ahead of every allow rule and of the
  host callback. One asymmetry remains open: a `@server/tool` reference grants ONE
  tool on kiro-cli while the array mounts the whole server here, and the set to
  deny is not knowable without connecting to the server — those extra tools still
  reach the host permission gate, so the surface is wider, not ungated.

Both call sites are fed from the one function —
`_new_session_following_substitution` (`session/new`) and the `session/load`
branch — and the array is ordered by server name so the two are comparable.

Those call sites are **synchronous**, and that is a harness-parity constraint
rather than a style choice. The translation reads disk, but it runs once per
spawn in `_resolve_session_mcp_servers` (off the loop, from the claude adapter's
own launch, `ClaudeLaunch.resolve_spawn`) and lands in `_session_mcp_cache`; `_session_mcp_servers` only
hands the cached list out. Awaiting an executor hop at the shared site would put a
new scheduling and failure point on **every** backend's construction path,
kiro-cli included — and the kiro path is not allowed to change in service of an
adapter (`AUTOSDE.yaml` `harness-parity` H13). The capability set is still what
decides: the set check comes first, so a harness outside it returns `[]` before
the cache is ever consulted, and a set member with a cold cache resolves inline
rather than coming up with no tools. `_reset_state` clears the cache, which is
what keeps the per-spawn freshness promise.

The kiro-cli path is unchanged: it receives the same servers via `--agent`, and a
duplicate array here would shadow the spec's own entries, so that backend passes
none.

### Session-scoped Claude settings

`_write_claude_local_settings` writes `<work_dir>/.claude/settings.local.json`
before the primary spawn (not only on the model-substitution retry) with
`permissions.defaultMode` when requested and `permissions.deny` rules derived
from the spec's `disabledTools`. Once the provider-advertised model cache is warm,
it also writes `availableModels` and a pinned `model`; on a cold cache both model
keys are omitted so a stale static allowlist cannot collapse a versioned `[1m]`
id to the 200K window. The post-capture re-seed fills them after `session/new`
records the backend's actual list.

With `agent.model` at `auto` the seed writes no `model` key, so the adapter takes the
model from the user's own settings (`ANTHROPIC_MODEL`, then `settings.model`). After
`session/new` or `session/load`, Crew sends that reported id back to the adapter.
Otherwise a resumed session can run Claude Code's built-in default instead
([model-selection.md](../common/model-selection.md#-only-inherits-a-served-default)).

**Crew creates the file, or adopts only a byte-for-byte Crew-authored orphan;
otherwise it leaves the path entirely alone.** `work_dir` is routinely a checked-out
project the user also drives with `claude` by hand, so a foreign
`settings.local.json` may belong to the user or a live sibling session. Crew reads
neither for merging, rewrites neither, and deletes neither. A durable
`seed_provenance` record stores the expected digest and arbitrates a live claim;
that lets a later process recognize, claim, and re-seed a file Crew left behind
after a crash without treating a user replacement as its own. The claim itself
revalidates the claimant's digest against the durable entry under the
cross-process lock, so a digest cached before another process recorded new
bytes cannot authorize adopting — and later deleting — a user's restoration of
the older bytes.

A fresh create uses `O_EXCL` + `O_NOFOLLOW` at `0o600`, so two sessions sharing a
`work_dir` cannot clobber each other. Re-seeding an owned file uses an inode-pinned
move-aside plus atomic replacement; a concurrent user replacement is detected and
left in place. Teardown runs ownership check, durable revocation, and deletion in
one shielded worker-thread transaction. If revocation or deletion cannot complete,
the old seed is restored or re-recorded so a later session can repair it rather
than leaving an unowned permission mode on disk.

**One relaxation: a byte-identical sibling seed is SHARED, not refused.** Two
sessions of the same agent in the same `work_dir` render the same payload, and
refusing the second one bought nothing — it ran with the whole `mcpServers` array
withheld, so only one session per project directory ever had Crew's tools
(`spawn_run`, `cron_*`, `session_checkpoint`). When the file on disk is a live
sibling's seed whose bytes equal BOTH Crew's durable record
(`seed_provenance.share`, checked ignoring the live holder) AND the exact
payload this client would have written, the client takes a shared-reader state
(`_claude_settings_shared`): the permission surface counts as governed
(`_permission_surface_governed`), so the array is delivered — but the client takes
no live claim, records nothing, and `_claude_settings_authored` stays false, so its
teardown neither unlinks the file the owning session is still running against nor
pops that owner's live slot. A payload that differs in any byte — another
permission mode, another agent's deny rules, another allowlist — fails the digest
half and is refused exactly as before. The hazard the live-holder rule exists for
only arises when the payloads differ, so byte-equality is the precise boundary of
the relaxation. The boundary deliberately includes the model keys: the file pins
model resolution for every session that reads it, so sharing across a model
difference would silently override the sibling's own pick. Every refusal logs the
same quiet informational message (`_log_declined_share`) naming what the session
runs without.

The sharer's stake is a live registration (`seed_provenance.share`, taken BEFORE
the byte checks so the owner's teardown cannot validate-race it; withdrawn on the
sharer's reset, and on a failed validation only when that validation created it —
a re-validating sharer keeps the lease its original validation earned). The
in-process registry (`_SHARERS`, like the `_LIVE` slot) is a cache; the authority
a DIFFERENT process consults is the holder entry the registration persists into
the durable record itself, under the module's cross-process file lock. Each
persisted holder carries a PID-reuse-safe process identity (pid + process start
id, from `platform_compat`), so `seed_provenance.claim` and the re-seed path
refuse while ANY process holds a live stake, and a holder whose process is
provably gone is stale and reclaimable — a crashed gateway leaves no permanent
lock. The guarantee is scoped to hosts where every Crew process runs a revision
that reads holder entries: a process on an older revision keeps only
`{size, sha256}` from the sidecar and consults no holders, so version skew
during an upgrade is the pre-existing digest-only exposure, not a regression
this mechanism can close. On a host that cannot prove a process start id, the
sidecar persists the
digest with empty holder groups: cross-process live-holder distinction degrades
to digest-only adoption, while `_LIVE` and `_SHARERS` continue to arbitrate
same-process siblings. While any sharer is registered, the file's future is pinned for
it: the owner's teardown leaves the file and the durable record in place (the
recorded-orphan shape a `kill -9` already produces, which the next session adopts
and repairs once the sharers are gone), `seed_provenance.claim` refuses new
adoptions, and the owner's own re-seed is declined entirely while a sharer holds
the file — `seed_provenance.record` refuses any digest change under a live sharer,
so neither a post-capture model-key refresh nor an edited agent spec's deny rules
land under a reader that validated the current bytes. Both are picked up once no
sibling is reading the file; the client declines a digest-changing re-seed before
the write and re-runs the check after it, so a sharer registering mid-re-seed
still causes a restore, exactly as it retracts an in-flight adoption. The
lingering file after a
clean owner exit under a live sharer is a disclosed cost of that pin, accepted by
design: no leaver ever deletes a permission surface another session is still
reading, and the residue is recorded and self-healing — the next session
recognizes the record, adopts the file, and repairs or removes it on its own
teardown. A user replacing the file by
hand remains their own action on their own machine — the same disclosed boundary
the owner path has always had.

Two supporting invariants keep every interleaving honest. Every durable provenance
mutation reports whether the sidecar agrees, and the client changes its mirrored
session flag only after that result succeeds — with one deliberate asymmetry: when
a user has replaced Crew's seed, authorship (the half `_permission_surface_governed`
reads) drops the moment the bytes are observed foreign, whether or not the durable
hand-back landed, and a refused hand-back is carried on
`_claude_settings_claim_unrevoked`, which feeds no governance decision and only
makes teardown retry `forget`/`release` without touching the user's file. `record`
also promotes a reader to
owner in that one locked persist: the owner's reader lease disappears from both
memory and the persisted holder groups before authorship is published, so no
two-call owner-plus-own-sharer window can pin its teardown. A record reaches the
in-memory table only once its sidecar persist has LANDED, so a sibling can never
validate a share against a grant that then fails — which is also what lets a
failing re-seed persist restore the moved-aside prior bytes (still the recorded
ones) instead of stranding an unrecorded file. And every restore of a moved-aside
entry into a vacated pathname is a type-agnostic no-clobber rename —
`platform_compat.rename_noreplace` on POSIX, `os.rename` on Windows, which refuses
an existing target natively — so whatever the move-aside captured (Crew's own seed,
or a user's regular file, symlink or directory of any size) goes back exactly as it
is, with no copy, no size cap and no read of its bytes, and the aside name is
consumed by the move. On Windows that rename runs while
`platform_compat.pin_directory` holds the parent open, so the parent cannot be
swapped mid-restore and a junction already at the name makes the restore fail with
the aside kept. A settings file recreated at the pathname in that window is
preserved rather than silently overwritten: the rename refuses it and the moved
entry stays recoverable beside it as `.crew-gc` litter. Only a POSIX host or
filesystem without the no-clobber rename primitive falls back to the validated
byte copy `pinned_fs.put_back_no_clobber`, both names pinned to the shared
parent's descriptor; it restores only the regular-file inode captured at the
move and refuses an occupied pathname the same way. When that parent cannot be
opened, nothing is copied by name: the restore fails and the aside is kept as
`.crew-gc` litter for the user to recover.

What this deliberately does NOT do is preserve and restore a user's own file.
Doing that means reading and rewriting a path a checked-out repository controls,
which is how a snapshot read, a cross-session ownership registry, an ACL-preserving
restore write and a foreign-edit comparison all become necessary — and each of those
is a place to get it wrong on someone's project state. Refusing the path is the
invariant that removes all of them at once.

The cost is disclosed rather than hidden. For a session that does not take the
exclusion path below, it is a REDUCTION in what Crew applies, never a widening of what
the session can do:

- A project that already has its own `settings.local.json` gets no seed file.
  The file is left exactly as it is.
- An inherited `bypassPermissions` in such a file is **not** stripped. Crew used to
  strip it; stripping required rewriting the user's file, which is exactly the
  machinery this rule removes. Such a file is left out of the session instead (see
  below): whatever starting mode the adapter picks from it is read back and pinned.

#### A project-owned settings file

The project's file is left out of the session instead of being rewritten.
`_exclude_foreign_local_settings` takes that path, and `_claude_session_meta`
puts it on the wire for `session/new` and `session/load`:

| `_meta.claudeCode.options` key | Value |
|---|---|
| `settingSources` | `["user"]` (no `project`, no `local`) |
| `settings` | Crew's seed payload, inline (the flag tier) |
| `allowDangerouslySkipPermissions` | `false` |

The claude CLI then loads neither project tier: not the project's local file, and not
a checked-in `.claude/settings.json`. No `permissions.allow` a repository carries can
pre-approve a tool, so every call still reaches Crew's gate. Crew's deny rules and
model allowlist ride the inline settings. That makes the permission surface governed
(`_permission_surface_governed`), so the session gets the full `mcpServers` array:
`send_message`, `cron_add`, `spawn_run` and the spec's servers. A cron job on such a
project delivers its result. The cost is that project-scope config does not load on
this path either: its `settings.json` env and plugins, and its `CLAUDE.md` memory
files, which the SDK loads only with the `project` source. The runtime warning names
the same list.

What the project tiers REFUSE is carried, not dropped. Leaving a tier out must drop its
allows, never its restrictions, so `_project_settings_restrictions` reads the
`permissions.deny` and `permissions.ask` rules of both project files
(`settings.local.json` and a checked-in `settings.json`) and they ride the inline
settings beside Crew's own. An `ask` rule matters as much as a deny: it forces a
prompt, and so a trip through Crew's gate, even where a user-tier allow or
`acceptEdits` would pre-approve the call. The read is bounded, never through
a link, and never followed by a write. It does not reopen the race the design avoids: a
file that changes after the read can only have gained a rule Crew did not carry, and
its allows never load either way. That startup window is closed too:
`_verify_claude_project_denies_unchanged` re-reads both files after `session/new` (or
`session/load`) and before the first prompt, and stops the harness when either holds a
deny or ask rule the envelope did not ship, or cannot be examined. A rule added after that
is the stated residual: the session's inline settings are fixed at creation, and the
next session reads the files again. A file whose bytes cannot be examined (a link, a
non-regular or oversized file, a read error, JSON nested past the parser's depth)
withholds the array, because taking the exclusion then could drop a refusal. Content
claude itself could take no deny rules from (not JSON, a `permissions` or `deny` of the
wrong type) contributes none, since nothing is dropped that the CLI would have
enforced.

Project `hooks` and `sandbox` settings restrict a session too, but they cannot be
carried inline: a `PreToolUse` hook can also answer `allow`, and `sandbox` carries
`autoAllowBashIfSandboxed`, so either would reopen a pre-approval channel. A project
file that sets either therefore refuses the exclusion
(`_CLAUDE_PROJECT_UNCARRIED_GUARD_KEYS`): its tiers keep loading and the array stays
withheld, as before this path existed. The startup re-read treats a file that gained
either key the same way, and stops the harness.

Two smaller residuals. The adapter's own `SettingsManager` still reads
`availableModels` from every tier for its model picker, so a project file can shape
the picker even though the CLI loads Crew's inline allowlist; this is cosmetic for
permissions. An operator's `dontAsk` is kept (below): it refuses every call that is
not pre-approved, so it runs nothing `default` would not.

What still loads is the `user` tier. A session whose file Crew authored loads
`user`, `project` and `local`, so this path is narrower than the normal one. The user
tier is the inherited-config gap below.

The project file is never read for its mode or allows, only for its deny rules,
because it can change between a read and `session/new`. One thing a file still decides: claude-agent-acp picks the
STARTING permission mode itself, from every settings file, whatever `settingSources`
says. In 0.84.0 its `SettingsManager` resolves all tiers, the project's
`settings.local.json` included (its `settings.js` module), and `newSession` passes the
result to the SDK as an explicit `permissionMode` (`dist/acp-agent.js`,
`initialPermissionMode`). So a project file's `defaultMode: plan` or `dontAsk` still
starts the session in that mode on this path, and the pin below keeps it. The mode is
read back rather than trusted. `_pin_claude_starting_mode` takes `modes.currentModeId` from the
`session/new` (or `session/load`) response, which is the mode the session really
started in. When that mode decides MCP calls without asking Crew
(`bypassPermissions` or `auto`) or is not reported, it sends `session/set_mode`
`default` and waits for it to succeed, before the first prompt. Every other mode
(`default`, `plan`, `acceptEdits`, and `dontAsk`, which refuses every call not
pre-approved) is kept as the session started it, so an operator's stricter posture is
never widened. A pin that fails stops the harness,
so a session never runs with Crew's tools under a mode that approves on its own.

The pin runs only on this path. A session whose file Crew authored is not pinned: a
user `~/.claude` mode reaching it is the inherited-config gap below (W2-1), which this
path does not widen. The exclusion is never taken below
`CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION`, or when Crew requested a mode itself, so
neither case reaches the pin.

The adapter behaviour the pin rests on is captured, not assumed:
`test/fixtures/claude_mode_pin/` holds live claude-agent-acp 0.84.0 frames driven with
this path's envelope. A project `defaultMode: plan` started the session in `plan`, and
`session/load` reported the mode back. A project `defaultMode: auto` was filtered by the
adapter and the session started in `default`, so a project file cannot start this path
in `auto` or `bypassPermissions`. `session/set_mode` `default` from `auto` answered `{}`.
The pin tests replay those answers.

The array stays withheld when the session asked for a permission mode of its own,
since the pin sets only `default`, when a project file's deny rules cannot be read
(above), when the adapter is below the `settingSources` floor or reports no version,
and when the path is a link (below). The floor is decided before the array exists: the
claude adapter's launch reads the installed version (`_claude_adapter_installed_version`)
before the settings writer runs and before the array is warmed off the loop, so nothing
re-resolves the array after the handshake. A re-seed after the handshake uses the
reported version. A handshake that reports a version below the floor on a session that
took the exclusion stops that session (`_pin_claude_starting_mode`); the shared
handshake gains no step.

**A session whose permission surface Crew does not govern gets no `mcpServers`
array at all.** The array is the only channel Crew has onto the session's MCP
surface, so delivering it there would hand `spawn_run`, `cron_add`, `send_message`
and every configured server into a permission surface Crew does not control — a
`permissions.allow` entry in the project's own file pre-approves the tool, no
`session/request_permission` is ever sent, and Crew sees the `tool_call`
notification too late to withhold it. So `ClaudeCodeMirror.session_params` fails
closed on that precondition rather than taking it on trust from its caller.

A **symlink** at either component (the file or the `.claude` directory), or a
sensitive resolved target, is REFUSED rather than followed. Since Crew never rewrites
an existing file, and reads one only for its deny rules without following a link, the
exposure is the *create*: a dangling link is
absent to `exists()` yet writing it materializes Crew's settings at the link's
target, and a symlinked `.claude` directory puts the whole write somewhere the
project does not own. A repository can ship
`.claude/settings.local.json -> ~/.aws/credentials`, so this is attacker-influenced
input. Refusing costs that session the seed, which the caller logs as a warning
naming what is lost.

The launch tail (`acp.launch.launch`) also merges `extra_env` into the child
environment, which is how a
caller-supplied `CLAUDE_CONFIG_DIR` reaches the adapter
(`test_spawn_forwards_claude_config_dir_from_extra_env`). The gateway contract is
to forward inherited `ANTHROPIC_*` and `CLAUDE_CODE_*` variables to the harness
child, so the spawn scrub list must not grow to cover either namespace. The public
core does not set `CLAUDE_CONFIG_DIR` itself: an isolated CC config root (seeding a
Crew-owned directory from the user's `~/.claude`, keeping credentials and models
while stripping inherited `permissions` that would pre-approve past Crew's gate)
is **not implemented here** — see the known gap below.

### Known gap: the user's global `~/.claude` is inherited

With no `CLAUDE_CONFIG_DIR` set by the core, the adapter and the SDK read the
user's real `~/.claude`. Project-scope `settings.local.json` outranks it for
`defaultMode`, but `permissions.allow` entries **merge** rather than being
overridden — so a user whose global settings pre-approve a tool family gets those
calls auto-approved by Claude's own engine, which never calls `canUseTool` and so
never reaches Crew's gate. A project's own `settings.local.json` is not part of
this gap: a session that carries Crew's tools either authored that file or left it
out of its setting sources. Its checked-in `.claude/settings.json` is part of the
gap only for a session whose file Crew authored. Both are the same hazard the "no gate on pre-approved calls"
section above describes, arriving through inherited config. Closing it means an
isolated config root, which is a separate change: it has to carry credentials
across (or CC cannot authenticate at all) while dropping exactly the `permissions`
keys that bypass the gate.

### Standing rule

Unchanged by Claude Code becoming selectable: `agent.provider` stays
single-valued and **no provider selector is re-added**. The harness switch is
`agent.acp_backend`, and it is gated in exactly one place
(`resolve_selected_backend`).

## Model registry

`src/kiro_crew/model_registry.json` is the shared model data source for
`model_registry.py` and `website/src/model_registry.json`.
`test_frontend_registry_matches_python_source` compares their parsed JSON, and
`website/src/providers/modelRegistry.ts` imports the frontend copy. The
per-entry `claude_code` provider IDs are registry mappings for the adapter's
advertised ids, not values accepted by `AgentConfig.provider`.

`model_registry._build_indices` indexes canonical keys, provider IDs, and
aliases. `from_provider_id` uses that index to recover a canonical key from an
advertised adapter ID. `TestModelRegistry.test_bare_advertised_ids_fold_to_canonical_key`
pins the bare-ID case.

`model_registry.available_models` and `display_list` sort default entries first
rather than trusting JSON object order. This is load-bearing because the adapter
uses the resulting allowlist when an automatic selection omits an explicit
model. `TestModelRegistry.test_fable_5_not_default` and
`TestModelRegistry.test_available_models_is_default_first` pin the default and
ordering behavior.
