# MCP Server Architecture

How MCP (Model Context Protocol) servers are configured, merged, probed and
loaded, plus the two invariants every new Kiro Crew MCP tool must satisfy: it
ships as an MCP tool (not only a CLI command), and it holds no per-caller state.

Async spawn receipts declare supported parent-work delivery
(`dashboard/handlers/messaging.py::parent_work_supported`). `spawn_run` also
forwards `executor="remote"` and optional `instance_id` to the gateway; remote
placement is accepted only after the peer confirms a run id, and the tool result
includes `[remote:<concrete-instance>]` as executor evidence. Omitting
`instance_id` delegates least-loaded connected-peer selection to the gateway.
The MCP process never opens an SSH/SSM connection or transfers source itself.
See the
[subagent contract](../system-specs/modules/subagent.md) for the enforced limits
and model-only value judgments.

Related: the CPP extension-point seam this doc reads from is
[platform-context](../system-specs/modules/platform-context.md); the governance
ceiling that filters auto-approve is
[governance](../system-specs/modules/governance.md); the computer-use server's
own gate model is [computer-use](../system-specs/modules/computer-use.md).

> **Design invariant: Kiro Crew does NOT write to provider globals.**
> `~/.kiro/settings/mcp.json` is user-owned. Kiro Crew reads it and never mutates
> it. Kiro Crew's own additions go into the per-agent file it fully owns,
> `~/.kiro/agents/kirocrew.json`. That keeps tools scoped to Kiro Crew out of every
> interactive kiro-cli and Kiro IDE session the user runs outside Kiro Crew. If
> `kirocrew-core` / `kirocrew-cron` ever appear in a provider global, that is
> leftover state from an older install: clean it from the dashboard MCP panel,
> or run `kirocrew setup`, whose setup path calls the narrowly scoped
> `mcp_cleanup.clean_stale_managed_mcp()` helper.

## Config file hierarchy

| File | Owner | Purpose | Read by |
|------|-------|---------|---------|
| `~/.kiro/agents/kirocrew.json` | Kiro Crew gateway (`agent.rebuild_agent_config`) | The rendered Kiro agent: model + tools + merged `mcpServers` | kiro-cli, when spawned as the `kirocrew` agent |
| `~/.kiro/settings/mcp.json` | User | Kiro global MCP servers | kiro-cli for all agents; merged into Kiro Crew's agent file at render time |
| `~/.kiro/crew/mcp.json` | User, via the dashboard MCP panel | specific to Kiro Crew additions and per-server tool disables | Kiro Crew gateway only |

`rebuild_agent_config()` writes exactly **one** file, `~/.kiro/agents/kirocrew.json`.
There is no second rendered agent file: Kiro Crew's provider is ACP-only, and
non-kiro ACP harnesses consume projected configuration rather than another Kiro
agent file.

### Provider-global scopes come from the platform seam, not the core

A provider-specific global (Claude Code's `~/.claude.json`, for example) is
**not** read by this build. It is contributed at call time by the CPP
extension point `McpToolingProvider.extra_mcp_scopes()`
(`platform/interfaces.py`), and the public `DefaultMcpToolingProvider`
(`platform/defaults.py`) returns `[]`. So in this repo:

- `agent._extra_mcp_scope_globals()` yields no paths, so the rebuild merges the
  Kiro global only.
- `mcp_discovery._extra_scope_sources()` yields no extra scopes, so discovery
  scans the two core files only.
- `dashboard/handlers/mcp.py`'s apply and uninstall paths write the Kiro scope
  only.

The three stay symmetric on purpose. If discovery scanned a file that apply and
uninstall could not manage, a server would show up in the dashboard that the
dashboard could never remove, and the rebuild would keep re-merging it into
sessions. `agent._CC_MCP_JSON` and `mcp_discovery.SCOPE_CC_GLOBAL` are retained
as the canonical constants for a companion edition and for tests, not as
evidence that the core reads that file.

### Merge order in `rebuild_agent_config()`

The existing `~/.kiro/agents/kirocrew.json` is the merge **base** when one
exists and `clean=False`, so any server the user already customized survives
(`autoApprove` edits, hand-edits, servers added with `kiro-cli mcp add --agent
kirocrew`). Onto that base:

1. **App-contributed servers** (`_collect_app_mcp_servers()`), keyed
   `{app}:{server}`, are assigned first so an app's namespaced entry outranks a
   same-named leftover in a shared file. Assignment, not `setdefault`: the
   manifests are authoritative and are re-derived on every rebuild, so keeping
   the previous rebuild's entry would preserve an `autoApprove` grant this pass
   had just stripped.
2. **`~/.kiro/settings/mcp.json`** (Kiro global). An absent name is added. A name
   a PREVIOUS rebuild left behind is *reconciled*: the transport-independent keys
   the source owns (`timeout`, `disabled`) are overwritten, and one the source has
   since dropped is removed, so an edit to the source reaches the generated spec
   instead of losing to the value the first rebuild froze. Every other key on the
   entry is the user's and survives by omission, `autoApprove` included.
   Reconciling retires the name, so a lower-priority scope cannot reconcile it
   again. `command`/`url` and the fields dependent on them (`args`, `env`,
   `headers`) are NOT reconciled — see the fallback's as-a-unit rule below.
3. **Seam-contributed provider globals**, so they can only fill gaps the Kiro
   global did not: a name step 1 or step 2 already claimed in this pass has been
   retired, so it is left alone. Empty in this build.
4. **`~/.kiro/crew/mcp.json`** via `update()` on an existing entry, so
   Kiro Crew's `command`/`args`/`env` win while user-set fields such as
   `autoApprove` survive.

Kiro global outranks any seam-contributed provider global because Kiro Crew is
kiro-cli-only. Managed servers are skipped by every merge loop: their
`command`/`args` are set by `_refresh_dynamic_fields()` and must not be
overwritten by a stale global entry.

**Resolution-aware fallback.** The same server can be defined in several sources
with different commands. If the merged winner's `command` does not resolve (a
bare command whose binary is not on the rebuild PATH), the rebuild retries the
same server's spec from the other sources in priority order (kirocrew, then
kiro-global, then provider-global) before dropping it. When it falls back to a
different source it adopts that source's `command`, `args` and `env` **as a
unit**, so one source's command is never paired with another's arguments.
Resolution, the dashboard probe, and the `env.PATH` written into the agent
config all go through the same `env.spec_env_path()`, so a server cannot probe
healthy on the dashboard while being silently dropped from the agent config —
or launched from it with a PATH the probe never validated.

A spec's `env` is applied per key by the consumer that spawns the server, so a
declared `PATH` **replaces** the child's inherited one rather than extending it.
A spec that names one directory to add would therefore hand the server a PATH
holding only that directory. `spec_env_path()` expands a declared `env.PATH`
into the full effective PATH — the spec's own entries first, then the augmented
inherited PATH, deduped — before it is written out. A forwarded declared `PATH`
reaches a pooled backend the same way: `gatewayd` composes it through
`env.mcp_search_path`, so the spec's entries LEAD that backend's PATH rather
than being the whole of it. Consequence to know about:
the emitted value is a snapshot of the rebuild-time environment, so it encodes
this host's directories (mise data dir, installed Node version bins, the
running interpreter's bin) and is not portable to another machine.

### `includeMcpJson` is pinned false

```json
{
  "includeMcpJson": false,
  "mcpServers": {
    "kirocrew-core":     { "command": "…", "args": ["mcp-core"] },
    "kirocrew-cron":     { "command": "…", "args": ["mcp-cron"] },
    "kirocrew-computer": { "command": "…", "args": ["mcp-computer"] }
  }
}
```

The gateway already merges the Kiro global into the agent file, so the agent
file is the superset. With `includeMcpJson: true` kiro-cli would merge the
global a second time at session start, producing duplicate entries and letting a
stale path in the global shadow the fresh path the gateway just resolved.
Kiro Crew forces `false` on every agent it manages (the primary agent and every
app agent). Plain kiro-cli agents outside Kiro Crew keep kiro-cli's own default.

### Managed servers

`agent._MANAGED_MCP_SERVERS` holds the eight servers the gateway owns end to
end: the always-on `kirocrew-cron` and `kirocrew-core`, the gated
`kirocrew-computer`, and the opt-in `kirocrew-dashboard`, `kirocrew-work`,
`kirocrew-crew-log`, `kirocrew-debug` and `kirocrew-panel`. Every emitted or
explicitly granted entry is refreshed on every rebuild by
`_refresh_dynamic_fields()`, which rewrites `command`/`args` from the live
`kirocrew` binary, strips stale remote-transport fields (`url`, `headers`) left by
older builds, and re-pins `env.KIROCREW_HOME` to the home the gateway is actually
running under while preserving the user's own env keys.
User customizations are preserved, `autoApprove` included: a list the owner wrote
by hand is a deliberate statement about their own tools and survives the refresh,
which is what `mcp.honour_auto_approve` (on by default) decides. It is worth
knowing what it costs, because the cost is not obvious from the key's name:
kiro-cli approves those tools locally, so Kiro Crew never sees a permission
request for them and its own tool gate does not run. Turn the setting off to drop
every verb no server spec declares and put those tools back through the gate. A
verb a managed or edition spec declares is unaffected either way, and a
governance ceiling strips the key whatever the setting says.

Only the OWNER's own list is honoured. An app's `<app>:<server>` entry is granted
by its manifest rather than chosen by the owner; an agent config materialized for an
app declines the opt-in for its whole map, since an app's keys arrive from the
manifest, the shipped spec and the per-agent policy and need not be namespaced; and
an entry carrying the `x-kirocrew` provenance marker is Kiro Crew's own. All three
stay on the strict floor, as does a value that is not a `list[str]`, which kiro-cli's
strict parsing would reject with no rebuild able to repair the file. On that app map
a verb a server spec DECLARES still survives.

Ownership is judged by the entry's shape -- its name and provenance marker -- never by
who wrote the file, so a plainly-named entry an agent appended reads as the owner's.
`~/.kiro/settings/mcp.json` is therefore on the write-protected tier of the file-tool
floor: an agent's file-edit tools cannot write it. The shell route is not fenced by
that floor; [governance.md](../system-specs/modules/governance.md#self-protection-the-keystone)
owns that residual.

For KAS native managed servers, session projection supplies the actual gateway
listener port and the allocation-time caller session key. These values come
from the gateway rather than the editable agent environment. Native tools keep
their own session identity and reach the serving instance on a non-default port;
third-party servers receive neither value.

An entry may also carry a **`spec_gate`** — a predicate consulted at spec
EMISSION time. `kirocrew-computer` is the one row that has one, and the
distinction it draws is the difference between a capability that advertises no
tools and one that costs nothing: emitting the entry is what makes kiro-cli spawn
the backend, so an in-process enable check can only ever refuse work in a process
that is already resident (~109 MB, per chat process, including every `spawn_run`
subagent). While the gate is closed the server appears in neither `mcpServers`
nor `tools`, so nothing is spawned at all. Both loops that write specs honour it,
and asymmetrically on purpose:

- `build_agent_config()` withholds the entry **and pops one arriving from the
  user override file** — a platform gate exists because there is no driver on
  this OS, and an override must not smuggle a server past it;
- `_refresh_dynamic_fields()` **retracts** an entry a previous pass wrote while
  the gate was open, because a skip-only refresh would mean turning a feature off
  never reclaims the process turning it on started;

`kirocrew doctor` is a third, read-only consumer: its MCP sections resolve the
same registry gate (`cli_doctor._spec_gate_closed`) so a gated-off server's
absence reads as informational rather than as a missing entry, so doctor never
demands what emission deliberately omits.
**The `@server` refs in `tools` / `allowedTools` are left exactly as they are.**
Withholding the entry is the whole control: a `@server` ref resolves against the
agent's own `mcpServers` plus the global `mcp.json`, so with no entry in either
there is nothing to launch — the ref names nothing and mounts nothing.

| | |
|---|---|
| **Preserved** | the user's `tools` and `allowedTools` refs, verbatim — including a mount hand-narrowed to a single `@server/tool` |
| **NOT preserved** | the entry's `autoApprove` and user `env` keys. An off/on cycle resets these; the operator re-applies them |

Stripping the refs as well is the tidier-looking design and it is where a whole
class of defects came from. The removed set is not reconstructible from the server
name — a user can narrow `tools` to ONE `@server/tool` ref while the re-enable path
re-adds the BARE ref — so anything that prunes must also stash and restore, and
every way that stash can fail silently **widens** the mount: an unwritable stash, a
stash cleared before the spec write landed, a rebuild path that skipped the
restore. Leaving the refs alone has none of those states, and the only place a stash
could live is a sidecar the agent itself can write.

That last point is why the entry's own fields are still dropped rather than stashed:
a restored `autoApprove` would be an **agent-authored** value that a later rebuild
installs into the spec, and kiro-cli approves an auto-approved MCP tool *locally* —
no permission request is emitted, so `hooks.on_tool_call` (deny floor,
sensitive-path check, governance ceiling) and the SEL audit are never reached. For
tools that can click and type into an already-authenticated application that is a
self-granted gate bypass. Losing an approval is the safe direction; restoring one
from a file the agent can write is not.

Withholding is recorded to SEL as `mcp_server_withheld`, derived from the gate plus
the shipped template rather than from a config delta — nothing in the spec changes
shape when a gate closes, so there is no delta to observe, and the audit trail would
otherwise have no record that a shipped server was deliberately not emitted.

The gate decision is snapshotted **once per rebuild** and threaded through both emit
loops, so a keystone flip landing mid-rebuild cannot produce a spec that emits one
server's entry under the old decision and another's under the new one.

Under an enterprise MCP registry, `_refresh_dynamic_fields()` also maintains a
`"type": "registry"` marker on these managed entries — added when
`agent.mcp_registry_mode` is declared, and REMOVED when it is not. The marker is
maintained rather than preserved because it tracks the account the gateway is
signed in to, not a user preference, and because the client's filter is
symmetric: outside registry mode a marked entry is the one that gets dropped.
`command`/`args` stay either way, since the registry path is not the only
consumer of this spec (doctor's handshake probe and the CC sidecar sync both
launch from it — though doctor skips the probe for a server whose spec gate is
closed, since no emitted spec defines it). See
[../guides/enterprise-mcp-governance.md](../guides/enterprise-mcp-governance.md).

`kirocrew-computer` carries **no `autoApprove` key and none may ever be added.**
kiro-cli approves an auto-approved MCP tool locally and emits no permission
request, so `hooks.on_tool_call` (the PreToolUse deny floor, sensitive-path
check and governance ceiling) is never reached for it. For a tool that can click
and type into an already-authenticated application, that would be a complete
gate bypass. Its stdio shim answers an empty `tools/list` while the keystone
enable is off — retained as defence in depth for a mid-session disable, on top of
the `spec_gate` above that keeps the process from existing in the first place.

### The final ref reconcile

`tools` and `allowedTools` hold `@server` and `@server/tool` refs, and kiro-cli
mounts only what `mcpServers` declares, so a ref naming a name absent from the
final map mounts nothing. Several passes narrow that map without touching either
list: the resolution pass replaces `mcpServers` wholesale and drops unresolvable
servers by OMISSION, and the locked app re-merge deletes entries whose app is not
confirmed enabled. Because the merge base is the previous rendered config, a ref
outlives the server it named — across a rename, an uninstall or a disable.

One `prune_dangling_tool_refs()` pass over the FINAL map therefore runs at the
single funnel every write path goes through, after the passes that mutate
`allowedTools` and before the auto-approve filter below. Reconciling the final
map rather than each narrowing pass is what keeps a pass added later safe by
default.

**The two lists take separate exemption sets, because they fail in opposite
directions.** `declared` names servers whose absence this rebuild EXPECTS a later
pass to reverse and governs `tools`; `declared_grants` governs `allowedTools`.
Keeping a mount ref too long costs one mount attempt against a name that holds
nothing, while dropping one can unmount a server for good, since an existing
config deliberately never re-adds a template ref. Keeping a GRANT too long hands
the next server on that name an auto-approval nobody granted, on the one list
that never reaches the PreToolUse gate, while dropping one costs an approval a
human can give again. So a name this rebuild is UNSURE about is exempted as a
mount and not as a grant: the mount survives the doubt and the grant does not. A
caller with no such doubt passes one set and both lists read it.

Three classes populate the mount set: a server whose `command` did not resolve on
this pass, a gated-off shipped server whose entry is withheld while its ref is
retained by design, and a name some readable source still declares. A `@` name in
`RESERVED_TOOL_NAMESPACES` addresses a kiro namespace rather than a server —
`@builtin` carries the whole built-in tool surface plus the `tool_search` loader
— so it is never in the map and is never a leftover.

An unresolved disabled source deliberately denies grants across its complete alias
family: the canonical alias and every numeric-suffixed sibling whose final dash
segment contains only digits. It removes those refs from `allowedTools` and blocks
the rebuild from adding them again, while leaving `tools` and `mcpServers` intact.
The collision allocator can hand any family alias to a distinct live server, so a
stale grant on that name must not auto-approve the replacement. Prefix-sharing
aliases outside the numeric-suffix family keep their grants.

**App-contributed names are read twice, before and after the rebuild's work.** A
`{app}:{server}` key is minted by an app manifest, so a disabled app's grant must
not outlive it; but an ownership read that FAILED cannot be told from one that
found no owner, and only the second is safe to treat as unowned. An app's
enablement is read as a tri-state so that a metadata read fault is not recorded
as a deliberate disable. Ownership only ever decides the GRANT: the reconcile
never unmounts on an ownership answer, because `mcp_server_alias()` is
many-to-one and a collision is resolved by suffixing, so which claimant a
`base-2` sibling came from is a guess that is unrecoverable by the next rebuild
— dropping its `tools` ref on that guess deletes a server nothing re-adds, while
keeping a ref costs one mount attempt against an empty name. The grant requires
positive evidence a guess cannot supply: a name an app claims EXACTLY keeps its
grant only while its own claimant is readably switched on, and family membership
never lends a sibling an enabled owner's answer. A name a readable source still
declares outranks a switched-off app's claim on it, since the rebuild's own ref
sync would otherwise re-add the pruned per-tool grant as a WHOLE-server one.

**Both outcomes are recorded where an operator can see them.** Revoking a grant
emits `mcp_auto_approve_revoked` to SEL, the same feed as the withhold above,
because it is a permission decision and no config delta records it. Dropping a
mount ref is logged at WARNING, which the shipped `agent.log_level` default
shows: the ref named nothing, so the tool was already unreachable, but a
misclassified absence is unrecoverable and an operator debugging a tool that
stopped being offered should not have to raise the log level first.

### The final auto-approve pass

`allowedTools` is kiro-cli's blanket auto-approve list, and it is the one path
that never reaches the PreToolUse gate. Builtin grants (`fs_read`,
`execute_bash`, …) arrive straight from the shipped agent template, so no
per-writer path re-touches them. The last thing `rebuild_agent_config` does
before writing is filter the whole assembled `allowedTools` list through one
predicate: a ref the governance ceiling has an opinion about loses its blanket
grant and its calls go through the gate, where the per-argument rule actually
applies; a ref the ceiling is silent about is kept. `mcpServers[*].autoApprove`
gets the same treatment on the final map, with one difference: an owner-written
verb is KEPT, because `mcp.honour_auto_approve` is on by default, and only a real
`false` there drops every verb no server spec declares. `tools` is deliberately left intact,
because mounting a tool is not auto-approving it. Both outcomes are recorded in
SEL, `mcp_auto_approve_withheld` when a grant is taken away and
`mcp_auto_approve_honoured` when an owner-written one is kept, so an operator can
see both why a template tool prompts and which calls are skipping the gate.

### Two writers, one lock

`~/.kiro/agents/kirocrew.json` has two independent writers: this whole-file
regenerator and the app-MCP registration path
(`apps.bridges._register_mcp_servers`), which does a read-modify-write of the
same file under `bridges._mcp_lock`. A register landing between the rebuild's
app-server snapshot and its write would be silently erased by the full-file
regeneration, so the rebuild takes that same lock across a final re-read and
merge of the app-namespaced servers. An app server is dropped only when its app
is confirmed no longer enabled; absence from the on-disk map is not by itself
proof (a clean rebuild starts from an empty map, and dropping on that basis made
an enabled app's tools vanish).

## Discovery and probing

Source: `mcp_discovery.py`.

`list_servers()` reads `~/.kiro/agents/kirocrew.json`, then each scope file with
provenance, re-resolves stale managed commands, and overlays cached probe
results. Every returned `McpServerInfo` carries a `presence` dict so the
dashboard can render per-scope badges. The `kirocrew` badge is the **effective**
state after the merge minus every entry in `~/.kiro/crew/mcp.json` whose
`disabled` is present and anything but a literal `false`
(`mcp_cleanup.mcp_entry_is_muted`, which fails closed); the other badges are raw
membership in that scope's file.

Probes run from `POST /api/mcp/probe`:

- **stdio** servers are spawned and driven through an MCP `initialize` handshake
  followed by `tools/list`.
- **HTTP** servers get the same two JSON-RPC calls over POST. The remote probe
  offers `MCP_CLIENT_PROTOCOL_VERSION` (`2025-06-18`) in `initialize`. A `-32602`
  refusal of that offer whose `data.supported` lists an older revision makes it
  retry once with the newest such revision. Every request after `initialize`
  carries `MCP-Protocol-Version` set to the revision the server answered with.
  The connection L1 smoke test (`connections/l1_smoke.py`) imports the same
  helpers, so both paths negotiate the same way. The stdio probe offers
  `2024-11-05`.
- **Disabled servers are never spawned.** `probe_all()` returns a muted row as
  an unprobed `status: "disabled"` placeholder with no error text, keeping the
  tools of its last real probe, and writes nothing to the probe cache.
- **Both calls must succeed for `ok`.** An initialize that answers and a
  `tools/list` that does not (no response, an error reply, a non-200) is a
  server no session can get a tool out of — the badge certifies "tools usable",
  so that combination reports as an error naming `tools/list`, not as `ok` with
  an empty list.
- Every result carries **`probedAt`** (wall-clock seconds of the probe that
  produced the status) and **`probeMode`** (`handshake` for a real round trip,
  `declared` for the managed in-process fallback below). Both ride the cache
  into the API payload, so the UI can say *when* a status was true — the caches
  legitimately serve results up to their TTL, and an undated "Online" reads as
  "now". A remote result may additionally carry **`authChallenge`** and, when
  the grant lookup produced a verdict, **`authGrantPresent`**. Both ride the
  same cache and are described with `needs_auth` below.
- Timeout is `dashboard.mcp_probe_timeout_secs` (default 15s;
  `_PROBE_TIMEOUT_SECS` is the fallback if config is not loaded yet). Results
  are cached for `_PROBE_TTL_SECS` (1800s), after which status reads as
  "outdated" — with `probedAt` preserved, because *when it was last true* is
  the most useful thing an outdated row can say.
- The cache is keyed by server name, and each entry also records a fingerprint
  of the config it was probed under (`_probe_identity`: command, args as the
  probe spawns them, and non-secret env for a local server; url and header
  names for a remote one). An entry whose fingerprint does not match the current
  config reads as NOT cached (`unknown`, no tools, no `probedAt`) rather than
  `outdated`: an expired entry was true of this server, a mismatched one
  describes a different target. Every reader that takes an entry by name
  applies that check: `list_servers()` compares the fingerprint for tools and
  `authChallenge`, and the handler-side readers call `cached_probe_is_current`
  (`GET /api/mcp`'s handler-cache overlay, `GET /api/mcp/probe`, and the
  shareability verdict). A failed probe after an edit does not inherit the
  previous target's tools.
- The handshake response is kept, not just the tool names: advertised
  `capabilities`, the `protocolVersion` the server ANSWERED with, `serverInfo`,
  and per-tool `annotations`. These feed the shareability verdict (below); the
  probe already paid for the round-trip, so reading them costs nothing.
- `client_info` overrides the identity sent in the handshake. The shareability
  pre-flight uses it to ask one server under two identities; such a run is
  excluded from the shared per-name probe cache, because a synthetic-identity
  handshake is a diagnostic and not the canonical observation the dashboard
  renders.
- Remote header VALUES may carry `${VAR}`/`${env:VAR}` references — the
  documented config form kiro-cli resolves at session runtime. The probe
  resolves them through the gateway rewriter's declared-env expander
  (`mcp_gateway.rewriter._expand_env_placeholders`): same regex, same
  credential-filtered source view, and an unresolved reference stays literal —
  so the probe presents the credential a session presents instead of sending
  the reference as text and reporting the server's correct rejection as a
  failing row. Probe-error redaction keys on the resolved values the probe
  actually sent.
- A remote server that answers the handshake with `401` — or with `403` carrying a
  `WWW-Authenticate` challenge — and whose sent headers carry no static
  `Authorization` credential gets status `needs_auth` and an empty `error`, not
  `error`. An `Authorization` value still carrying an unresolved `${VAR}`
  reference (a missing or credential-filtered variable) supplied nothing, so it
  does not count as a static credential here. The probe
  holds no OAuth token, because kiro-cli owns token custody
  ([design-notes/mcp-oauth-ownership.md](design-notes/mcp-oauth-ownership.md)), so
  the status code alone carries no verdict on the server: an unauthorized server
  and one the runtime calls successfully both return it.

  **Two pieces of evidence split that ambiguity**, and the wording follows what
  they support rather than the status code. The probe parses the challenge (a
  Bearer challenge carrying a `scope` list or an https `resource_metadata` URL sets
  `authChallenge`), and it stats kiro-cli's paired grant artifacts for the url to
  set `authGrantPresent`. Three wordings follow, one per answer the pair supports:

  | Evidence | Badge | Why |
  |---|---|---|
  | challenge, `authGrantPresent` false | **Sign-in required** | "Nobody has signed in" is observed, so the row names the action and states where it happens. |
  | challenge, `authGrantPresent` true | **Signed in** (muted) | A grant artifact exists. The badge reports THAT, never that the server answers — the probe holds no token, so validity is the one thing it cannot check, and the hover says so. Toned muted rather than warn: amber is the panel's "act now" colour, and a resolved row wearing it is indistinguishable by colour from one that still needs a sign-in. |
  | no challenge, or `authGrantPresent` absent | **Not verified** | An older gateway, or a bare `401`. Naming any state here would assert more than the probe observed. |

  The middle row is what gives the guided sign-in an ending: without it a user who
  followed the instruction landed back on the same badge they started from. It is
  deliberately not "Online" — a stored grant is evidence a sign-in happened, not
  proof it has not since been revoked, and only a probe carrying the runtime's
  token could close that gap.

  Reaching that ending is a VISIBLE instruction, not a hover. The panel is served
  from the probe cache for the whole TTL, so a user returning from a completed
  sign-in meets a row that still reads "Sign-in required"; if the "probe to
  refresh" step lived only in the badge's `title` it would reach neither a
  keyboard nor a touch user, at the moment of highest doubt. The cell carries one
  clause, and the `title` carries the longer form naming the control and the state
  the row lands in.

  Grant-key derivation, paired artifact paths, and presence checks live in the
  leaf `mcp_grant` module. The probe, connection mint, and persisted connection
  status all consume that shared layout so a kiro-cli cache change cannot make
  those surfaces disagree with one another. Its origin serialization matches
  the Rust URL runtime at the byte boundary: Unicode domain names are
  IDNA-encoded and IPv6 literals retain their brackets before hashing.

  **`grant_presence()` is the single tri-state**, and there is deliberately only
  one spelling of it. Each paired artifact is stat-ed exactly once and classified
  by errno — the ENOENT family is a definitive absence, any other `OSError` is
  "unknowable" — then the pair combines: either artifact definitively absent
  decides it, any remaining failed stat makes it unknowable, otherwise present.
  It is **not** built on `Path.is_file()`, and that is load-bearing rather than
  stylistic: from Python 3.14 that method swallows every `OSError` and answers
  `False`, so a permission error or a stalled mount would be indistinguishable
  from "nothing was ever written". This package declares `requires-python >=
  3.12` with no ceiling, so a build on 3.14 would silently collapse the middle
  answer and tell the owner of an authorized server to sign in again — the exact
  harm the three-valued design exists to prevent. Two spellings over the same
  artifacts is how one of them loses that answer, which is why the probe and the
  status module resolve through this one function.

  **The grant stat is SEL-audited on whichever answer its caller acts on.** The
  mint and the status module poll for a grant to *appear*, so only the positive
  moves anything and only the positive is recorded — auditing each poll would
  write one critical event per iteration of a single flow. The probe reads once
  and renders either answer, and an absent pair is exactly what produces
  "Sign-in required", so it passes `audit_absence=True` and the access leaves a
  trail as `success` or `missing`. Opting in is the caller's, not the default's,
  so a future polling caller cannot silently flood the log.

  **Absence in the payload is meaningful, and the UI gates on an explicit `false`.**
  `authChallenge` is omitted when the probe learned nothing about authorization.
  `authGrantPresent` is omitted for that reason too, and *also* when the grant
  lookup could not answer at all — a cache home that raises, such as a permission
  error or a broken mount. Reporting an unanswerable lookup as `false` would tell
  the owner of an already-authorized server to sign in again, so the three-valued
  result is preserved to the wire rather than flattened. Each probe clears both
  fields before it runs, and `list_servers()` re-attaches them from the cache only
  when the entry's config fingerprint matches, so a row whose url was edited does
  not inherit the previous endpoint's verdict from either side.

  One degradation is NOT covered by that, and the limit is worth stating. The
  lookup mirrors kiro-cli's own cache-key derivation and artifact layout, both
  undocumented internals. If kiro-cli re-keys them, the stat succeeds against a
  path that is simply absent, so the answer is `false` rather than unanswerable and
  an authorized server reads "Sign-in required". That row does not recover on its
  own: a second sign-in mints artifacts under the NEW key while Crew keeps stat-ing
  the old one, so it goes on asking for a sign-in until the mirror here is
  corrected. The recorded-hash tests pin the mirror only against itself, so the
  drift would not fail in-repo either. Detecting it needs an observation of an
  artifact kiro-cli actually wrote.

  A `401` on an entry that DOES carry a static `Authorization` header stays
  `error`: a supplied credential was rejected, which is a real fault. The
  challenge is still recorded there, because "this server wants OAuth, so no
  static header can satisfy it" is the actionable part of that failure. The GRANT
  is not: `authGrantPresent` is read only by the "Sign-in required" wording, which
  is gated on `needs_auth`, so looking it up on an `error` row would run a stat —
  and let `grant_observed` write a critical SEL event — for an observation nothing
  reads.
- A probed stdio child that ignores a closed stdin costs
  `_PROBE_TEARDOWN_WAIT_SECS` twice (graceful wait, then again after SIGKILL)
  before the process-group reap, which is why that budget is a named constant
  tests can shrink.
- **A probe that could not RUN is reported as a probe limitation, not a server
  fault.** `SandboxUnavailableError` is caught ahead of the generic handler:
  `probe_server` spawns through `sandboxed_spawn_argv(mode="standard")`, which
  fail-closes on a host with no OS sandbox backend (any Windows host, macOS >= 26).
  But kiro-cli launches these servers from the agent config **without going through
  this probe**, so the servers work while the probe cannot spawn them. Reported as
  an ordinary error, every row rendered red with "0 tools" and sent the user
  debugging a server that was fine. `server.error` therefore leads with the
  machine-readable `mcp_probe_sandbox_unavailable:` prefix (mirroring the `code`
  field on dashboard JSON error bodies), states that the server itself may be fine,
  and names the `agent.sandbox_allow_unsandboxed_exec` remedy (on Windows this
  refusal means the key is declared `false` or a governance floor is pinned, since
  the platform default permits the spawn). Because the cause is
  the HOST, it recurs identically for every server on every discovery cycle, so the
  remedy paragraph warns once per server name
  (`_warn_probe_sandbox_unavailable_once`) and demotes repeats to DEBUG.
  - **A managed server FALLS BACK to its declared tool list when — and only when —
    the sandbox refuses.** Every server in
    `mcp_discovery._MANAGED_SERVER_TOOL_MODULES` declares its tools statically in
    this package (the same `_list_tools()` functions the stdio shims answer
    `tools/list` from), so `_managed_tools_in_process` can serve the listing with
    no subprocess at all.
    That is what removes the `agent.sandbox_allow_unsandboxed_exec` opt-in for a
    read-only listing on a backendless host.
    - **Fallback, never primary.** When a backend exists the real spawn still runs,
      because it is the only thing that proves the server can **start**.
      `_fix_stale_managed_command` exists precisely because that invocation goes
      stale ("command not found: kirocrew; the built-in cron/core tools then never
      load"), and the probe is the one surface that catches it. Short-circuiting on
      the server *name* would report `ok` for a managed server that cannot run,
      silently changing what `ok` means in the shared `_cache_probe` store.
    - **Why the import is acceptable only here.** Reading the declaration imports
      package code **into the gateway process**, which the gateway does not
      otherwise do (these modules are absent from `sys.modules` at boot). The
      package directory is writable by the same uid the agent runs as and is not on
      the sensitive-path floor, so on a host where the sandbox *works*, importing
      would beat the isolation the spawn provides, which is why this is never the
      primary path. Reaching the fallback means the
      sandbox could not confine anything anyway, so the import concedes nothing the
      refused spawn had not already conceded.
    - The substitution is logged at **WARNING**, once per server: `ok` here means
      "this package declares these tools", not "the server answered", and the
      default log level is WARNING, so at info it would be invisible on exactly the
      hosts where it always happens. The result also carries
      `probeMode: "declared"` into the cache and the API payload, and the
      dashboard renders it as a **warn `Declared` badge rather than a green
      `Online`** — colour carries the distinction, because a scan of a dozen
      rows reads colour long before it reads small print. Third-party servers
      have no declaration to
      read and keep the honest `mcp_probe_sandbox_unavailable` error.
    - Modules are imported **lazily** (they pull in the validation/artifacts graph,
      which cannot be imported at `mcp_discovery` import time). Any failure returns
      `None` and the original refusal is reported, so a bad read never invents a
      result. An **empty** list is a real result: `mcp_computer._list_tools()`
      returns `[]` by design while the keystone enable is off.
- An MCP command that does not resolve is a **stable** fact, so it warns once
  per `(server, command)` and demotes the repeats to DEBUG. Timeouts and
  handshake errors stay at WARNING every time: a server that newly starts timing
  out is news, one whose binary is absent is not. The ledger self-heals, so the
  warning returns if the command later resolves and then breaks again.

`GET /api/mcp` also kicks off a background re-probe when it sees a server that
is not in the probe cache yet, so a freshly added server transitions from
"Unknown" on the next page load rather than waiting out the TTL. It and
`GET /api/mcp/probe` do the same for a server whose config was edited since its
last probe, once per edit: the arming is keyed on the stale entry's fingerprint,
so a row `probe_all` never re-probes (a quarantined server) cannot re-arm the
fan-out on every request.

`_fix_stale_managed_command()` rewrites a managed server's `command` and `args`
on every `list_servers()` call, because the stored absolute path goes stale after
an update. The invocation comes from `agent._kirocrew_mcp_invocation`, the one
source of truth for every install layout. It is cached per server and resolved
again only when the cached absolute command no longer exists on disk.

A managed server that keeps running after an update pruned the install it was
imported from checks for that before each `tools/call`, because anything it
imports later would fail. It refuses the call with JSON-RPC error `-32000` and a
SEL `rejected_install_pruned` record. When the gateway pool can respawn it
(`KIROCREW_MCP_POOLED_BACKEND` is set and `KIROCREW_MCP_POOLED_RESPAWN_COMMAND`
still resolves), the refusal asks for a retry, every queued call gets the same
answer, and the process exits with status `75` so the pool relaunches it from the
current install. Otherwise nothing would bring it back, so it keeps its transport
and refuses each call with a message to restart Kiro Crew.

## Shareability verdicts

`GET /api/mcp-gateway/servers` returns a `recommendation` per row: whether the
server looks safe to stub, and separately whether its backend looks safe to
share. The verdict is derived on this host from evidence ranked
observation > measurement > declaration, and a server the gateway has WATCHED
behave per-client while shared is never offered again. Nothing about which
servers a machine runs ships with Kiro Crew and nothing leaves the host.

Full contracts — the two on-disk records, the reason-code vocabulary, what the
pre-flight can and cannot decide, and the seed-once rule — live in
[`docs/system-specs/modules/mcp-shareability.md`](../system-specs/modules/mcp-shareability.md).

## Dashboard MCP management

The Connections page's MCP panel aggregates the scope files into one view with per-scope
badges. Clicking a badge **stages** an intent; the page accumulates staged
changes and exposes Apply / Discard. Only Apply performs writes.

**Add Server search reports every attempted provider.**
`GET /api/mcp/discover` keeps its existing `results` and `providers` fields and
adds `provider_outcomes: [{name, status}]`, where `status` is `ok`, `timeout`, or
`error`. The additive field keeps older clients compatible. A short availability
probe attempts no provider and returns an empty outcome list. The Add Server
modal keeps results from responsive providers and shows one inline incomplete
notice when any sibling provider fails. When every attempted provider fails, it
shows the shared `ErrorNotice` instead of the zero-match empty state. Retry runs
the whole search again; per-provider retry and provider-cache refresh are not
part of this contract.

`POST /api/mcp/apply` takes a batched payload and applies it in a fixed order:

1. **Uninstalls first.** `_purge_server_config()` removes the entry from
   `~/.kiro/crew/mcp.json`, the Kiro global, every seam-contributed scope, and
   directly from `~/.kiro/agents/kirocrew.json`. That last targeted delete is
   required: the rebuild uses the existing agent file as its merge base, so
   without it the additive merge would resurrect the server. Every step is a
   read-modify-write that no-ops when the entry is already absent, so re-running
   the purge changes nothing.
2. **Scope adds** write the spec into the target scope file.
3. **Scope removes** strip it. If the server would no longer be inherited into
   Kiro Crew but the user kept the Kiro Crew badge on, the full spec is first
   copied into `~/.kiro/crew/mcp.json` (the **preservation rule**), which is why
   "I removed it from the Kiro global and it came back" is correct behavior.
4. **Per-tool overrides** update `disabledTools` on the entry.
5. **One rebuild** at the end re-renders the agent file from the new on-disk
   state.

Each change is applied to the one raw config key its row stands for: the row's exact
key, else the single raw key whose alias it is (`npm:@playwright/mcp` for the row
`playwright-mcp`). A name that is the alias of several raw keys is refused with `409`
and `"code": "mcp_server_name_ambiguous"` before any write, both by `POST /api/mcp/apply`
(uninstalls included) and by `POST /api/mcp/toggle`.

Listing and probe rows say why a server is off. `disabledIn` is `"kirocrew"` for a
disable in Kiro Crew's own store, which the Kiro Crew badge plus Apply lifts, and
`"shared"` for a disable in a config this panel does not write, such as the Kiro
global `mcp.json` or a provider global; it is `null` when the row is enabled.
`disabledInFile` names the file to edit when the shared flag is the Kiro-global one.
`disabledReason` is `"invalid"` when the mute comes from a non-boolean `disabled`
value, so the table asks for the value to be repaired rather than toggled.

No scope metadata is persisted. Apply does one-shot edits and forgets; state is
re-read from disk on the next page load, so external edits (`kiro-cli mcp
remove`, hand-edits) are picked up naturally.

Apply does **not** restart sessions. Scope changes take effect at the next
session spawn; the Connections page header's Apply & Restart calls `POST /api/sessions/restart`
to drain the warm pool of pre-spawned processes carrying the old config, so a
freshly installed server is mounted on the next session rather than the one
after it. The response carries `mcp_sync_ok`, and `RestartButton` READS it: a
reconcile that failed is reported in the danger tint instead of the usual
"sessions restarted, config applied". Honesty that lives only in a JSON body no
user sees is not honesty — the sessions did restart, but against a config that
may not match the sources, and that is the one thing the caller needs told.

### Live reconcile: when no restart is needed at all

kiro-cli 2.10.0+ watches `~/.kiro/agents` and `mcp.json` and reconciles a
RUNNING session against an edit: only the changed servers restart, the
conversation is kept, and the change applies at the next turn boundary. That
makes the restart above redundant on that harness — it exists only to make
kiro-cli re-read a file it already watches. `kiro_crew/mcp_hot_reload.py` owns
the gate, and it is keyed to the processes actually running, never to the
binary on disk: every live provider — the registered sessions plus the warm
pool the reset would drain — must declare `LLMProvider.mcp_config_hot_reload`
as a literal `True` (default `False`, harness-parity H14). `AcpProvider`
answers it from membership in `ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD` (kiro-cli
only — KAS gets its servers injected on `session/new`, claude reads no agent
file) AND the `agentInfo.version` its own process reported at `initialize`
being at or above `MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION`. That floor is
2.21.0 — the release the reconcile semantics were observed on — not 2.10.0,
where the watcher first shipped: a release in between keeps the always-correct
reset until it is verified, because granting the skip to one that reconciles
differently fails invisibly, and lowering the floor later is one line. The
handshake version
is what each process runs; after an in-place kiro-cli upgrade the file on disk
is newer than every process spawned before it, so probing the file would answer
for a version nothing is running. The gate fails CLOSED: one provider that has
not handshaked, is below the floor, sits on another harness, or has not
declared the capability at all resets every session. `POST
/api/mcp/sync` consults it and, when it holds, touches no session —
`sessions_reset: 0` is the observable outcome. With no live process at all
there is nothing a reset could reach, so the answer is also to skip.
`POST /api/sessions/restart` stays the unconditional manual path.

The skip is inferred from that declaration, not observed: kiro-cli's
`_kiro.dev/mcp/server_initialized` notification is the signal that a reconcile
actually spawned the server, and the gate does not yet consult it. A watcher
that fails at runtime therefore reads as success with nothing red; the
troubleshooting entry below and the manual restart are the recovery, and
confirmation-based hardening is a named follow-up.

What the reconcile keys on shapes what the dashboard writes. An added, removed,
or edited `mcpServers` entry is acted on; `disabled: true` stops the server's
process and `disabledTools` hides a tool. A `@server` ref ADDED to `tools` is
honoured from the next turn — so the sync's write order (entry, then ref) is
fine. But a ref REMOVED while the server keeps running leaves its tools mounted,
so dropping the ref is not a disable. The dashboard's disable path
(`_sync_mcp_to_agent`, single and batch) therefore writes `disabled: true` onto
the agent-file entry as well as removing the ref, and the enable path lifts it.
The `disabled` in the kiro-global file cannot stand in: `includeMcpJson` is
pinned false, so kiro-cli never reads that file. The marker also helps a cold
session — a disabled server is no longer spawned only to sit unmounted.

A probe that FAILS after the sanitizer stripped a declared env key names the key
in its error (`_note_denied_env`). The sanitizer's own WARNING goes to the
gateway log, which is not where someone staring at a red badge is looking: a
Python server configured through `env.PYTHONPATH` fails the probe while working
in a session, and unexplained that reads as a probe bug rather than the
launcher boundary it is.

### Stub launch approval

A stub roster entry selects routing, while
`~/.kiro/crew/mcp-launch-approvals/approvals.json` authorizes the exact resolved
command and arguments together with the derived effective-environment hash that
gatewayd may run outside a session sandbox. Command and environment hashes are
stored and checked as one pair, so values from two approvals cannot be combined.
The approved fingerprint hashes the declared environment with its `${VAR}`
references unexpanded. When that declared launch is approved and only an
expansion changed, a rewrite pass whose environment sidecars all publish records
the new pair and drops the older expansion of the same launch: the values come
from the gateway environment (the operator's shell and the crew `.env`), which
no agent can write. A sidecar publication failure drops every rebind from that
pass, so gatewayd refuses the changed expansion and the next boot retries. A
changed command, argument or declared env text is still refused.
Both halves of the fingerprint fold exactly three gateway-computed spellings
(`hashing.install_aliases`), each in the ONE slot the gateway's own writer
(`apps/bridges.py`) emits it in: `sys.executable` (what the rewriter substitutes
for a manifest's bare `python3` and for the `kirocrew` host CLI) folds only as
the command, the absolute path of the `deps_boot` shim folds only as an argument
(`hashing.launch_token_bytes`, which takes the slot's role from the caller), and
the directory holding the `kiro_crew` package folds only as an `os.pathsep`
segment of the declared `PYTHONPATH` (`hashing.env_value_bytes`, which takes the
key). Each hashes by role rather than by the versioned directory a release places
it in, so the approval survives the upgrade that moves that install. Matching is
raw-string equality -- no containment, no case folding, no path parsing -- so an
agent-written token is folded only when it is byte-identical to a string the
gateway computed from its own interpreter and package location AND sits where
the gateway puts it; any other spelling, including a case variant or a sibling
file under the same prefix, and the same spelling in any other slot (the package
directory as a `--plugin-root` value, or under another env key), hashes
literally and is a changed launch. Every process that hashes a launch (the rewriter, gatewayd, the broker stub) computes the table from itself and they agree because they run from one install; the one spelling they can disagree on is Windows' 8.3 short name, which the stub is launched through when the install path holds a space (`rewriter._cmd_safe_command`), so on Windows each of the three also enters under its long name (`GetLongPathNameW` of the process's own path, never of a token; no link is followed). An approval whose digest was recorded without this encoding
reads as `changed_needs_reapproval` and needs one re-approve in
**Developer → MCP Management** (shown with developer mode on). No digest without
the encoding is accepted. Two installs sharing one data home (the desktop app and a
dev venv, say) therefore admit each other's approvals of a `python3`-pinned
launch, since each reads the alias as its own interpreter; that widens nothing,
because each gateway already runs as the user on that interpreter.
An absent or empty store approves nothing. A stub
without a matching fingerprint stays on the session's unpooled, sandboxed launch
path. A queued cold spawn reloads the store after admission and resolves the same
command and environment identity again immediately before the fork, so revoking
an approval while it waits prevents the process from starting. A stub with no
recorded approval needs one in **Developer → MCP Management** before it pools. Cached recommendation
seeding may add a stub route, but it grants no launch approval; the seeded
row stays on the session-sandboxed path until the operator reviews it.

The MCP Management toggle resolves the launch at the time of the operator's
choice. Every `stub: true` is a compare-and-set on a displayed launch, the first
approval of a name included: the UI reads
`GET /api/mcp-gateway/servers/launch?name=<name>`, which returns `name`,
`commands`, `envs`, `complete` and, only when the display is complete,
`expected_launch`. This display-safe, bounded GET follows the other dashboard
read routes and does not require owner identity; the POST that records the choice
remains owner-only. `POST /api/mcp-gateway/servers/stub` refuses a missing token
with 409 `expected_launch_required`, a truncated display with 409
`launch_display_incomplete`, and a token that no longer matches the resolution
with 409 `launch_changed_since_display`. A batch `stub: true` returns 400
`batch_stub_requires_individual`; batch `stub: false` is accepted. Turning a stub
off revokes the approval first, and restores it when the config write does not
commit. The approval also stores the redacted, bounded display of the approved
launch, so a `changed_needs_reapproval` refusal carries `approved_commands` and
`approved_envs` beside the new `commands` and `envs`; an approval recorded
without a display omits both fields. A refused row exposes each display-only command argv with its redacted
declared environment. Both displays are scrubbed and bounded before they enter
the refusal record, and status polling only reads that record; it does not rebuild
or resolve agent specs. Every identity serialized into `expected_launch` has
exactly one displayed command and one displayed environment, so an approval
covers only launch content the operator saw. Selecting re-approve sends back the
stored command/effective-environment pair for the row. The handler compares it
with both the stored refusal and a fresh resolution before any write; a changed
launch returns 409 and preserves both stores. A matching launch replaces the
fingerprint and clears the refusal. A changed command, arguments, or declared
environment requires the same re-approve flow. The gateway-owned `mcp/resolved/`
store is sealed read-only in
session sandboxes because its record chooses the entry point substituted for an
approved npm launcher. Resolve-once therefore gives sandboxed npm a private
prefix below the sealed runtime parent, using the existing validated carve-out
that accepts only self-derived runtime directories. After validating the
completed tree, the gateway process renames it into `mcp/resolved/` before
committing the record. The carve-out cannot cover the sealed store.

### Which `mcp_gateway.*` knobs a config write reaches

Two knobs are resolved per use rather than captured at boot, so a `config.json`
write applies with no broker restart:

- **`resolve_once_refresh_hours`** — `_mcp_resolve_refresh_secs()` reads the live
  snapshot (falling back to a fingerprint-cached load before the watcher has
  primed), so the pre-resolve loop's per-iteration re-read is a real re-read: a
  longer or shorter window takes effect on the next pass. It is read in the
  GATEWAY process, which is where the config watcher runs.
- **`apps_enabled`** — retired as a preference (nothing writes it; see
  [mcp-apps.md](../../src/kiro_crew/docs/mcp-apps.md)), but a stored `false` is
  still an opt-out. The MCP Apps gate reads it on every call through the
  fingerprint-cached `KiroCrewConfig.load()`, so the opt-out applies with no
  daemon restart, and an unreadable config fails closed.

**`enabled`** is neither per-use nor boot-only. `POST /api/mcp-gateway/enable`
writes it and applies it in-process: the broker is stopped and, while any server
is still stubbed, restarted so every stub is re-emitted with or without
`--poolable`, and sessions are relinked. A write that bypasses that endpoint takes
effect at the next gateway start.

`response_spill_threshold_bytes` is read in the BROKER process
(`gatewayd`), which runs no config watcher — `config.live.snapshot()` is always
`None` there — so a per-use read of the live snapshot would be inert and the
field stays boot-only (`RESPONSE_SPILL_THRESHOLD_BYTES`, resolved once at import:
env pin `KIROCREW_MCP_SPILL_THRESHOLD` → config key → built-in 256 KiB), marked
`restart=True` like the rest of the section. `read_buffer_limit_bytes` is boot-only
for a second reason as well: it is handed to asyncio readers as `limit=` when they
are CONSTRUCTED and cannot be changed afterwards. The stub reads no config: the
rewriter resolves the config value (ignoring the environment) and stamps it onto
each stub's argv as `--read-limit`, and the stub resolves its ceiling as
`KIROCREW_MCP_READ_LIMIT`, then the flag, then the config key, then the 64 MiB
default. Because the value rides the overlay, `read_buffer_limit` is an input to the
rewrite fingerprint, so changing the key regenerates the overlays.
`test_mcp_gateway_stub_import_budget.py` pins the module set a running stub holds,
which is what keeps the config package out of it. `socket_path`, `overlay_dir`,
`idle_timeout_secs`, `max_backends`, `prewarm_count`, `stub_servers`,
`poolable_servers`, `stub_overrides`, `pool_identity_env`,
`forward_declared_env`, `spawn_concurrency_initial`, `spawn_concurrency_min`,
`spawn_concurrency_max`, `spawn_queue_wait_secs`, `initialize_timeout_secs`,
`host_budget_max_procs`, `host_budget_max_rss_mb` and `host_budget_max_fds` ride
the daemon's command line or size structures built once at spawn, so they too
are marked `restart=True` in the config schema and apply to a broker started
after the change.

### What partitions the pool, and what deliberately does not

`PoolKey` (`mcp_gateway/pool.py`) is the sharing boundary: two stubs whose keys
hash equal get one backend process. Its rule is that **every attribute which
changes backend behaviour must be a dimension**, or two sessions can see
cross-tenant state. The eight dimensions are `server_name`, `agent_name`,
`command_args_hash`, `effective_env_hash`, `work_dir`, `binary_version`,
`os_uid` and `config_snapshot_hash`.

The rule has a second edge that matters just as much: **an attribute which does
NOT change backend behaviour must stay out.** The hash is injective over every
field, so a difference in one that isolates nothing still forks a process.

Read the cost carefully before tuning the pool from it. A field is only as
expensive as the number of distinct values that reach it, and `agent_name` is
itself a dimension — so two agents never share a backend regardless of what
else the key holds. Dropping a field that does not isolate anything removes a
split that is real but narrow on its own (one agent across a config change, or
its own `autoApprove` list) and is a precondition for the broader sharing that
dropping an identity dimension would unlock.

Four fields a stub reports under a "security boundary" label are in that class
and are **not** dimensions: `sandbox_mode`, `autoapprove_set_hash`,
`approval_mode`, `trust_all_tools`.

- **The sandbox is not applied to a pooled backend at all.** `gatewayd` spawns
  backends outside any mount namespace, as an accepted and documented risk
  (`backend.spawn_backend`'s security-boundary note, and `security.md` under MCP
  Gateway). The per-session sandbox wraps kiro-cli, not a gateway-spawned
  server. Two sessions configured for different sandbox tiers therefore get
  processes confined identically, so splitting them buys a second unsandboxed
  process rather than a second sandbox. The compensating controls that do the
  real work here — the target command coming only from the rewriter's
  `KIROCREW_MCP_TARGET_<SERVER>` vars, credential-env scrubbing before `gatewayd`
  inherits, and backends running as the invoking user — are all per-host, not
  per-key.
- **Approval is decided before a call reaches the gateway.** Tool visibility, the
  `autoApprove` list, the approval mode and trust-all are kiro-cli's own
  per-agent decision, taken against that agent's overlay entry. What arrives at
  the stub is a `tools/call` kiro-cli has already authorised. A backend never
  receives these values and cannot act on them, so they cannot change its
  behaviour. The rewriter deliberately **preserves `autoApprove` on each wrapped
  entry** (`_build_stub_entry`) precisely so that per-agent surface survives
  pooling; the stub's `--auto-approve` / `--approval-mode` / `--trust-all` flags
  feed only the register payload.

`os_uid` stays a dimension. The cross-OS-user boundary is real, and it is
independently enforced as well (the broker socket is `0600` and the daemon
checks the peer uid), so the dimension costs nothing it does not also deliver.

The four fields are still **accepted on a register payload and ignored** — the
same wire-compat treatment `user_identity` and `channel_id` get. A stub and a
daemon are upgraded separately, and an adopted daemon predating the change runs
a `PoolKey.from_register` that hard-requires all four, so a stub omitting them
would have every register rejected as malformed and the whole install silently
un-pooled. `test_mcp_gateway_poolkey.py` pins both halves: the field set is
asserted explicitly, so adding or removing a dimension is a deliberate test
change, and a payload carrying any value for the four still registers to the
same key.

### Admission before allocation

The daemon bounds how many backend processes it FORKS AND INITIALISES at once,
and how many it is answerable for in total, before anything is allocated. Two
objects, built once in `run_gatewayd` beside the pool (`mcp_gateway/admission.py`,
`mcp_gateway/host_budget.py`) and threaded into every spawn path -- pooled,
connection-private, mid-call respawn and prewarm -- through `_acquire_backend`:

- **`HostBudget`** charges a fixed per-backend estimate (`procs`, `rss_mb`,
  `fds`) BEFORE the fork and releases it only when `process.wait()` returns, so a
  backend that survives SIGKILL stays charged until it is really gone. Pooled,
  private and fallback backends are charged identically: exclusivity is a
  topology property, not a budget exemption, and a stub's per-session exec after
  a `compat`/`isolation` rejection is one more process on the host, charged by
  the daemon when it sends the rejection and released when that connection
  reaches EOF (a new stub keeps the socket inheritable across its exec so EOF is
  the backend exiting). Ceilings come from `mcp_gateway.host_budget_max_*`; `0`
  derives processes from the available-memory sample the supervising gateway
  passes on argv (never below `max_backends`) and descriptors from the daemon's
  own `RLIMIT_NOFILE`, and leaves memory unbounded.
- **`SpawnGate`** is one daemon-wide count of spawn+initialize windows in
  flight, FIFO past that. Fixed capacity from `spawn_concurrency_initial`
  (default 4), clamped to `[spawn_concurrency_min, spawn_concurrency_max]`
  (1/8, the ceiling raised on the daemon's argv to the subagent ceiling when that
  is higher, so a fan-out the subagent cap admits is not queued behind eight
  backend initializations); `set_capacity(n)` is the seam the adaptive
  controller plugs into. A
  `Permit` covers the fork and the backend's first `initialize`: `ready` is sent
  to the stub before the handshake arrives (the stub forwards kiro-cli's first
  frame), so the spawn path never awaits it inline -- a detached watcher on
  `_init_done_event`, bounded by `Backend.initialize_timeout_secs` (a
  constructor field fed from `mcp_gateway.initialize_timeout_secs`, not a module
  setter), settles the permit `success` (ready), `failure` (handshake failed;
  the permit is then held until the process is reaped) or `neutral` (no
  handshake ever arrived -- an unused prewarm or a client that never
  initialised is not congestion). `settle` is exactly-once and separate from
  `release`, which is idempotent and settles `neutral` on its own; cancellation
  at any boundary is neutral. Prewarm settles neutral the moment the fork
  returns; it also re-reads the queue depth BEFORE EACH KEY and stands that key
  down while any stub is queued (`_PrewarmStoodDown`, logged and skipped like any
  other prewarm failure), and takes its wait with a `_PREWARM_SPAWN_WAIT_SECS`
  deadline. Both because a pass lasts as long as its spawns: a once-per-pass
  check leaves a stub that arrives during one queued behind the rest of it, and
  the gate has no priority lane, so what bounds a stub sitting behind a prewarm
  that already enqueued is that deadline. An unbounded prewarm wait would also
  park under `_prewarm_lock`, which the credential-rotation re-warm has to take.
- **`BackendPool.reserve_resident_slot`** moves the `max_backends` check in
  front of the fork: a slot is claimed (or `PoolAtCapacity` raised with nothing
  to reap) before `spawn_backend`, and `add` consumes it. Private backends take
  none.

Acquisition order inside `_acquire_backend`'s spawn closure -- after the per-key
`_spawn_locks` dedup and after `CircuitBreaker.allow`, so a permit is never held
during a breaker cooldown and no pool lock is held while queued -- is
**SpawnGate → HostBudget → resident slot → fork**, released in reverse on any
failure before the fork. Deadlock argument: the budget and the slot never wait
(they succeed or raise), so the only wait is the gate's FIFO, and a gate waiter
holds nothing another waiter needs; a holder of resource k only ever waits for
resource k+1, so the wait-for graph is acyclic. **That is why the gate is first
and not last.** A charge taken before the wait prices a process that does not
exist for as long as the wait lasts -- up to `spawn_queue_wait_secs`, 600 s by
default -- so a queue of ten reaches the ceiling with nothing running and the
eleventh stub is refused `capacity` on an idle host, a refusal that deliberately
authorises no fallback. Taken after the permit, the budget and the slot are read
against the host as it is at the moment of the fork. The daemon's drain closes
admission FIRST -- queued waiters fail with `SpawnGateClosed`, watchers are
cancelled (releasing their permits neutral), charges are dropped -- then
proceeds with the existing teardown.

**Wire.** The `registered` reply carries the daemon's `code_fingerprint`.
A stub whose `--server` names one of Kiro Crew's own MCP servers and whose
target argv names its managed subcommand also puts its `stub_code_fingerprint`
on the Register frame and requires the two values to match exactly before it
keeps the broker connection. A missing value identifies
a pre-fingerprint daemon; a different value identifies another installed code
generation. Either condition closes that connection. At cold start the stub
takes its normal per-session `fallback_exec`, so a package upgrade cannot leave
current MCP servers consuming stale caller-identity or directive frames. On a
mid-session reconnect no exec remains (`initialize` is long consumed), so the
stub refuses that generation terminally -- the same exit as the `poolable_ack`
and `tenant_nonce` refusals -- rather than retrying a daemon that is up and will
keep answering the same way for the whole reconnect budget. This is a protocol
compatibility check, not an authorization proof. Third-party targets send no
`stub_code_fingerprint` and retain their existing binary-version pooling and
old-daemon compatibility.

**Fallback identity.** `fallback_exec` keeps the stub's session token
(`KIROCREW_STUB_SESSION_TOKEN`) only when the target passes the same
`_spawns_own_control_plane` vetting gatewayd applies before it hands a pooled
backend the token: control-plane name, managed binary by real path, exact args,
no loader or `PYTHON*` overlay in the child env (Kiro Crew's own pinned UTF-8
values excepted), and no import root shadowing `kiro_crew`. A kept token also
gets `PYTHONSAFEPATH` (and `PYTHONNOUSERSITE` unless user-site holds this
package). Every other target has the token removed, so a third-party binary
never inherits it. Without the token a fallback control plane relies on the
kernel peer check alone, which cannot name the session on a runtime hosting
several sessions or over TCP, and every policy read is refused
`identity_unattested`.

`REGISTERED_CAPABILITIES` carries `spawn_queue`. A stub that saw it
sends `{"type": "ensure_backend", "wait_budget_secs": N}` and the daemon queues
the spawn for `min(N, spawn_queue_wait_secs)` LESS
`_QUEUE_REFUSAL_MARGIN_SECS` (capped at half, so the subtraction is strict for
any N), writing
`{"type": "queued", "position": p, "capacity": c, "in_flight": i, "waited_secs": w}`
every 5 s; a stub that did not negotiate it sends the bare frame, never sees
`queued`, and its gate wait is bounded at 20 s so its own 25 s pre-flight timer
still governs. The margin is the same property as that 20 s: **the daemon has to
give up first.** The stub starts its timer before it writes the frame and the
daemon starts its own only after reading it, so equal budgets -- which is what
the shipped defaults are, 600 s on each side -- expire on the stub first, and a
stub whose budget expires runs the per-session `fallback_exec` that a `capacity`
refusal exists to withhold, charged to nothing. Raising `spawn_queue_wait_secs`
above the stub's constant is harmless for the same reason: what the stub asked
for caps the answer before the margin is taken off it. While any acquire or respawn waits, the connection handler keeps
reading (`_await_answering_pings`): bridge pings get their `pong` at once and
any other frame is parked and processed in order afterwards, so the stub's
liveness monitor never declares a queued daemon dead -- bounded in both
dimensions by `_MAX_PENDING_FRAMES` / `_MAX_PENDING_BYTES`, the same guard class
as `backend._STUB_INBOX_MAXSIZE` in the reverse direction, because only the main
loop drains the park and it cannot run until the wait returns; past either bound
the pending acquire is cancelled and that ONE connection is dropped so
co-pooled sessions survive. `rejected` frames carry
`class`: `capacity` (resident pool full, host budget exhausted, wait budget
spent, breaker OPEN, a fork refused for memory/descriptors) with
`retry_after_secs`; `compat` (a pooled target this daemon cannot run or map);
`isolation` (a private target it cannot launch). `fallback: true` rides
`compat`/`isolation` and NOTHING else, on every wire shape: it authorises the
stub's own per-session exec, and a stub that never negotiated `spawn_queue`
closes its socket before exec'ing, so the charge `_reply_rejected` takes is
released at that EOF -- before the process it pays for exists -- and N
simultaneous `capacity` refusals would leave N backends the host budget never
sees, which is the unbounded fan-out admission exists to end.
**The compatibility cost is deliberate and falls on the pre-`spawn_queue` stub
alone.** It cannot read `class`, so it reads the untagged refusal as terminal and
exits 1 with `initialize` unanswered: kiro-cli reports that server as failed and
that ONE session loses its tools until the retry, where before it would have run
an unaccounted copy. **The bound is on the refusal ARRIVING inside that stub's own
pre-flight window**, which is 25 s in the pre-upgrade binary: a refusal later than
that reaches a stub which has already given up waiting and exec'd on a path that
reads no frame, so the exec is unaccounted however the frame is tagged. What keeps
the daemon inside the window is `_LEGACY_SPAWN_WAIT_SECS`, pinned strictly under
25 s. It bounds the GATE wait only, so a refusal raised after the permit — a full
pool or an open breaker, past up to `_MAX_SPAWN_DRAIN_RETRIES` spawn-and-initialize
rounds — can still exceed it; that residual is the reason the pin exists rather
than a claim it removes. The refusal is recorded on both sides -- `_audit_pool_rejected`
on the daemon, a `terminal:` line in `stub_fallback.jsonl` that
`fallback_counts()` keeps separate from real fallbacks in the `stats` reply -- so
the degradation is observable rather than silent. A stub that DID negotiate
`spawn_queue` pays nothing: it keeps its transport and answers `-32001` below.
`compat` is not refused with it, because there the host is fine and the exec is
the topology the connection asked for; refusing it would strand every session
behind a daemon whose target map drifted. The stub (`stub.py`) negotiates
`spawn_queue` on all three of its paths -- cold-start `ensure_backend`, the
`_reconnect` replay (which pre-flights before replaying `initialize`, and
retries a `capacity` answer within its remaining budget) and the bridge (where
a `queued` frame during a respawn counts as proof of life like a `pong`) -- and
renews a 25 s SILENCE timer on `queued`/`keepalive`/`pong` rather than running a
fixed deadline, bounded by its 600 s reconnect budget -- inside which the
daemon's own wait always ends, per the margin above. The `capacity` rejection
that ends the wait is answered to kiro-cli as JSON-RPC error `-32001` with
`data.class` and `data.retry_after_secs` on every request, the stdio transport
left open (`_serve_capacity_refusal`): the session is told the gateway is full,
not that the server crashed, and no exec is run. The `stats` frame carries an
`admission` snapshot (gate capacity/in-flight/queued/outcomes, budget counters).

### Windows target command spelling

A bare Windows command resolved through `shutil.which` has its on-disk basename
restored before it is spawned or persisted. `which` can append uppercase `.EXE`
from `PATHEXT` even when the file is named `demo-mcp.exe`. Windows can open
that path, but a launcher that dispatches by its own basename with a
case-sensitive lookup can reject it. The repair
(`kiro_crew.env.resolved_command_casing`) scans the resolved path's parent
directory and substitutes the unique case-insensitive basename match instead of
canonicalizing the full path.

The three MCP server command resolvers share it, next to the `mcp_search_path`
they already share: the agent-config resolver (`agent._resolve_command`), the
dashboard probe (`mcp_discovery`) and the rewriter. Resolvers of Kiro Crew's own
binaries stay outside it: the `kirocrew` lookup in
`agent._resolve_kirocrew_bin`, and the kiro-cli resolver, whose discovery
in `kiro_cli.py` does its own Windows `realpath`. So do the ACP adapter
ladders in `acp/launch.py` and `acp/harness/{claude,pi}.py`, which call
`acp/launch.py`'s `_normalize_exe_casing`. The agent-config resolver is the one that
matters most: its result is written as the spec's absolute `command`, and an
absolute command is accepted verbatim on every later pass, so an uppercase
spelling persisted once would look operator-authored to the rewriter forever.
Repairing at the resolver rather than at the write site also keeps the
provenance record's `emitted` value repaired, so `command_is_ours` still
recognises the entry and re-derivation stays enabled.

That narrow lookup preserves the lexical parent route (including a directory
junction) and a file symlink's own name. Explicit absolute commands did not pass
through `PATHEXT` and retain the operator's spelling unchanged. Empty or
unresolvable commands remain unwrapped; an `OSError` while reading the parent,
or an ambiguous case-insensitive match in a case-sensitive directory, keeps the
`which` result. POSIX paths are unchanged. The normal cache-hit and
transient-keep checks compare the same normalized bare-command probes, so a
case-only rename invalidates a cached resolution without changing its alias
route. The normalized spelling is a rewriter fingerprint input, so a cached overlay
carrying another spelling is regenerated. Stub argv and the daemon target map therefore consume the same
command string.

### Stub argument transport

Generated overlays carry backend arguments as a base64url-encoded UTF-8 JSON
string list in `--target-args-b64`. Pool-identity environment names use the same
codec in `--pool-identity-env-b64`. The encoded alphabet contains no shell
metacharacters: a Windows CLI can reparse the launch through `cmd.exe` without
turning a delimiter into a pipeline. Empty arguments, Unicode and literal pipes
retain their original boundaries. This is encoding, not encryption; arguments
remain visible on the command line, and the longer representation still counts
against the host's command-line length limit. On Windows, generation warns
with the server name and command length when the base stub command reaches
cmd.exe's 8,191 UTF-16-unit limit; it never logs the argument payload or rejects
a launcher that can use a longer command. Per-session additions can grow the
command further.

The stub decodes before both fallback launch and command hashing. The rewriter's
daemon target map decodes identically, preserving the hash used to select a
backend. Encoded flags take precedence when present; malformed payloads fail
rather than falling back to different arguments. Legacy `--target-args`,
`--pool-identity-env` and `--target-args-sep` remain readable, and a cached
delimiter-based overlay fails the rewriter fingerprint and is regenerated. The
rewriter and stub must come from the same package; a stub that predates the
encoded flags cannot consume them.

Encoding the backend arguments alone leaves the rest of the stub's metadata raw:
the target executable path, work dir, socket, env sidecar path, server and agent
names and the `autoApprove` JSON. `cmd.exe` expands `%NAME%` for any NAME set
in its environment inside any of these, quoted or not, and offers no escape for
it on a `/c` command line -- measured natively, `python%X%.exe` reached the stub
as `pythonexpanded.exe` and the tool name `read%X%` as `readexpanded`, so the
stub launched a different executable and registered a different approval hash
than the daemon computed from the operator's spec. The overlay therefore carries
the stub's whole flag list as ONE envelope, `--stub-flags-b64`, using the same
codec; inside it the flags keep their plain spelling. `stub._parse_args` and the
rewriter's `_collect_target_env` splice the envelope back through
`hashing.expand_stub_flags` before reading, so a plain-flag overlay written by an
older rewriter parses through the same path and hashes identically. The
per-session `--channel-id` appended by `session_servers` rides its own envelope.
A cached plain-flag overlay fails the rewriter fingerprint and is regenerated. The
interpreter path in the entry's `command` is the one value the codec cannot
cover: the CLI runs it, not the stub.

### Overlay scope: user-level agents only

The rewriter reads `~/.kiro/agents/` and writes one overlay per agent NAME, so the
overlay directory describes user-level agents and nothing else. kiro-cli also
resolves `--agent` against `<project>/.kiro/agents/`, which means a session can be
running an agent of that same name from a different file. `session_servers`
therefore takes the session's checkout: an agent the checkout declares has no
overlay, the lookup answers nothing, and the session launches the servers its
project spec declares.

Injecting the user-level stub instead handed that session servers the project
never declared, and left the project's own declaration of a same-named server
unlaunched, because a session-injected entry outranks the spec entry it shadows.
The scope reaches `pooled_session_servers` and `injection_server_names` through
one guard, because the second is what a mirror withholds from its own projection:
a name withheld but not injected costs the session that server entirely. Which
names a checkout declares is read from `agent_discovery.project_agent_files` and
`project_agent_name`, the same pair `acp/session_mcp.py` resolves the session's
agent spec through, so the two cannot disagree about which file the session runs.

Those servers run unpooled -- outside the pool, outside caller-identity
attribution, outside broker governance -- the same direction every other
unvouchable stub takes here, and the lookup says so at WARNING rather than debug:
this is an ordinary configuration rather than an error path, so at debug the
governance downgrade would be exactly as silent as the defect it replaces. Brokering them instead is not an overlay change:
`gatewayd` resolves a backend command from `KIROCREW_MCP_TARGET_<SERVER>` in its
OWN process env, written at daemon launch from the rewriter's `target_env`, and a
stub never tells the daemon its target. An overlay written for a project agent
after launch therefore has no target the daemon can resolve, so closing that half
needs a channel for a target the daemon was not started with.

One host is the exception, and for it the scope must NOT be applied. KAS projects
the agent spec itself from `paths.kiro_agents_dir()` alone
(`acp/kas_agents.load_agent_spec`), refusing a project-only agent at session start
rather than projecting it, so the user-level agent IS the one a KAS session runs
even when the checkout declares that name. Scoping its lookup would collapse the
stub set to empty, the projection would declare the user-level servers
un-subtracted, and they would run outside the broker while the operator has the
gateway switched on -- the governance loss inverted. Membership lives in
`ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY` and is read through
`agent_sdk.backends.overlay_project_scope` rather than as "is KAS", so a host added
later that reads the user level alone joins the set instead of needing a branch.
The KAS harness's own call passes `work_dir=None` for the same reason, which keeps
both halves of that session -- what is injected and what is withheld -- on one
answer.

The checkout is only half of that scope. `project_agent_files` scans the `*.json`
and `*.md` spec forms alike, because whether a checkout's spec may be projected is
a governance question for its consumers rather than a question of form; this
lookup is narrower, since it is deciding whether the agent the session RUNS came
from the checkout. kiro-cli discovers `*.json` in a checkout, so a project
`foo.md` with no JSON twin must not suppress a user-level `foo.json`'s stubs: that
would leave the servers kiro-cli does activate running with no pool, no
caller-identity attribution and no governance, which is the same loss this scope
exists to prevent, reached from the other side. `overlay_project_scope` therefore answers
in keywords -- the checkout together with `markdown_specs` -- and every call site
splats that one mapping, so no site can take the checkout without its format rule.

`markdown_specs` is `has_mirror`, the same registry read both call paths already
use to decide whether a projection happens at all, because the hosts genuinely
disagree and each answer is right for the host holding it. A MIRRORED host's array
is composed by Crew from a spec `acp/session_mcp.py` resolves through
`project_agent_files`, which honours the markdown form: a project `foo.md`
declaring `gitlab` comes back from `_agent_spec_for` with that server. For those
hosts the markdown file IS the agent running, so its shadow must suppress the
overlay -- otherwise the user-level `foo.json`'s stub survives, outranks the
project's own declaration, and mounts that stub's command and credentials under the
checkout's agent. A host with no mirror resolves no project spec through Crew at
all, so it takes the JSON-only answer for the same reason kiro-cli does.

`ACP_BACKENDS_MARKDOWN_AGENT_SPECS` -- a host reading the markdown form from a
checkout itself -- is deliberately not OR-ed in, because its only member also reads
the user level alone and leaves with no checkout, so the term would have no caller
able to reach it. `test_agent_sdk_capabilities` pins that containment, so a host
which breaks it fails there naming the decider.

That kiro-cli discovers project `*.json` and not project `*.md` is MEASURED on the
shipped binary rather than read off a document, because the documents disagree: the
vendored upstream reference describes the IDE 1.0 / CLI 3.0 schema and says the
filename without `.json` or `.md` becomes the agent name, while
`agent_spec_format`'s own header records markdown as the v3 engine's form. On
kiro-cli 2.22.0, `kiro-cli agent list` run inside a checkout holding both
`probe-json.json` and `probe-md.md` lists exactly one workspace agent, `probe-json`;
`probe-md` does not appear. A newer CLI that does discover project markdown would
make this backend a member of the markdown set, which is the condition the
containment pin names. That measurement is not a one-off: the
`KIROCREW_E2E_REAL_KIRO_CLI`-gated suite pins it beside the session/new precedence
guard, and its failure message names the set to join, so an upgrade that adds
project markdown discovery reports the remedy rather than silently reopening the
defect.

The parse requirement travels with the form set, because both answer one question:
WHICH RESOLVER decides this session's spec. A mirrored host's spec comes from
`session_mcp._project_spec_path_for`, which scans both forms and matches on
`project_agent_name` -- filename fallback included -- and which, once it matches a
project file, returns that file's read without falling back to the user level. So a
malformed project spec leaves a mirrored session with NO spec: no `tools` allowlist,
no project servers. The overlay must not fill that gap with the user-level agent's
stubs, so a mirrored host takes `dispatchable_only=False` and suppresses on the
malformed file. kiro-cli resolves the checkout itself, reports a malformed spec as an
error and runs the user-level agent instead, so it takes `dispatchable_only=True` and
keeps the stubs that belong to the agent it is actually running. The two hosts take
OPPOSITE answers on the same file, and `overlay_project_scope` computes both facets
from one `has_mirror` read so neither can be set without the other.

A project spec that does not PARSE is not a shadow for a kiro session either.
`project_agent_name` falls back to the filename stem for a malformed file, so a
broken `foo.json` matched `foo` and withheld a good user-level agent's stubs, while
kiro-cli reports that file as an error and offers no such mode -- measured the same
way: a malformed project spec yields `Error: Json supplied at ... is invalid` and
zero workspace agents. `_project_shadow_of` takes `dispatchable_only` for that, and
its default is unchanged so the governance refusal in `agent_materialization/worker_agent.py`, for which a file
in any state is a claim on the name, keeps refusing.

## How app agents reach MCP servers

An app declares MCP servers in its manifest, and
`apps.bridges._register_mcp_servers()` writes them into Kiro Crew's agent config
under a `{app}:{server}` namespace rather than into the shared Kiro global,
because that global is read by Kiro IDE and every other kiro-cli agent, so an
app's private tools would leak into surfaces that never installed it.

An HTTP MCP server whose backend port cannot be resolved live is **not written
at all**, and any stale entry for it is scrubbed. A manifest's illustrative
fixed port written verbatim while the backend is down is a reachable-looking but
dead URL, and kiro-cli connects to every server in the agent config on each
request, so one dead entry surfaces as a transient 5xx and then a hard error for
**all** requests, not just that app's. The enable path re-registers with the
real port once the backend is up.

Connection and tool exposure are separate. An entry in `mcpServers` is still
connected even when it has no matching `@server` reference in `tools`; omitting
the reference hides that server's tools from the agent but does not avoid the
process or connection cost. Isolation and feature gates that must avoid that
cost therefore remove the server entry itself as well as its tool reference.

An app agent that references a host-managed server (`@kirocrew-core`,
`@kirocrew-cron`) in its `tools` gets the launch spec copied in by
`_materialize_managed_refs()`. kiro-cli resolves a `@server` ref against the
agent's own `mcpServers` plus the global `mcp.json`, and managed specs live in
the host agent's config only, so without that copy the ref dangles and the tool
silently never mounts.

Containment for app agents has three layers:

| Layer | Mechanism | Where enforced |
|-------|-----------|----------------|
| Agent config | `managedToolPolicy` renders as `disabledTools`; a `neutralize` entry re-declares a server with every tool disabled and does not add it to `tools` | Written at registration, no network |
| kiro-cli | Reads `disabledTools` and filters before the model sees the list | In-process, no network |
| MCP server | `GET /api/session-tool-policy` returns the calling session's `managedToolPolicy.exclude`, and the server filters `tools/list` and `tools/call`. When that read fails the policy is `unresolved`: `tools/call` refuses with an audited error, `tools/list` still lists everything | Gateway round-trip |

Which session that third layer asks for is resolved by `mcp_shared._policy_session_key`
in the same order the strict resolver uses — the gateway's per-call caller, then the
protected member binding, then the signed per-session token, then
`KIROCREW_SESSION_KEY` and the pid sources. The RESOLVED session is what rides the
request's `X-Session-Key` and what keys the per-session policy cache, so the answer
returned and the answer stored are the same session's. Keyed on the gateway identity
instead, every caller the gateway could not name shared one entry, and the cache has no
TTL: two sessions on one process took each other's policy and a rekeyed session kept its
predecessor's for the life of the process. The 5s startup-race window is keyed the same
way: it remembers the identity it was opened for (`""` for "no key yet", or the key the
gateway answered 404 for) and answers only for that identity, so a `tools/call` that
resolves a real key mid-window is asked for, not told `no_session_key` — a reason
`tools/call` does not refuse on.

`managedToolPolicy` and `includeMcpJson` are in
`bridges._FRAMEWORK_OWNED_AGENT_KEYS`, so they are refreshed from the template on
every boot rather than preserved as user preferences. Preserving them is wrong in
both directions: a template that later tightens `exclude` would never reach an
already-enabled install, and anything that edits the agent file could drop the
exclude list, which the framework would then faithfully preserve forever.

`neutralize` uses explicit tool lists rather than a wildcard, because the app
discovers the real tool names, so a server that grows a tool cannot quietly slip
past a stale pattern.

The third layer is defense in depth for hosts that ignore `disabledTools`. When the
policy cannot be read, what it does depends on WHY, because the reasons differ in
kind and its two consumers carry different risk.

`tools/call` fails **closed** on the four reasons in `_UNRESOLVED_REFUSES_CALL`
(`mcp_shared.py`): `policy_unreadable`, `identity_unattested`, `identity_unattestable`
and `resolution_failed`. Each means "an operator exclusion may exist and this process
could not read it". `policy_unreadable` is the gateway's `409`
whose body carries `"code": "policy_unreadable"`: a spec for this session exists and its
policy could not be determined. `identity_unattested` is the gateway's `409` whose body
carries `"code": "member_identity_unavailable"`: the gateway could not establish the
execution identity behind the declared `X-Session-Key`. Usually the request carries no
`X-Session-Token`, or one that vouches for another key; an attested key whose execution
record cannot be read, or a `cron:` key the scheduler has no record for, answers the
same way. In every case the spec was never consulted, so whatever it excludes is unknown
here. The two
share a status, so the MCP side tells them apart by the body's `code`; a `409` whose body
cannot be read or carries no `code` takes the `policy_unreadable` arm, the status's
meaning for the unreadable-spec condition, so an unknown `409` is never read as anything
narrower. The call is refused with an error naming the reason -- the `identity_unattested`
text names the missing token, its usual cause, the `policy_unreadable` text the agents
directory, and says the gateway answered so the cause is not a connection problem -- and
audited as `rejected_policy_unresolved`; the read that produced `identity_unattested` is
itself audited as `tool_policy.unattested`, whose resources carry the asking process's
`pid` and `ppid`. A control-plane backend
(`CONTROL_PLANE_BACKENDS` in `mcp_gateway/daemon/control_plane.py`) is handed the token per frame in
the caller block and the policy read sends it, so its calls do not land there. The test
for admitting a reason here is that it means ONE thing, because a refusal derived from an
ambiguous reason is wrong for half the callers it hits.

`identity_unattestable` is decided before any request. A session key resolved locally
-- no gateway `caller_session`, no `X-Session-Token` to send with it, and no
`KIROCREW_SESSION_KEY` in the environment -- carries nothing the gateway could attest,
so the dial is skipped, the read is audited as `tool_policy.unattestable_key`, and the
call is refused with text that names the missing session token without claiming a
gateway read. It is a separate reason from `identity_unattested` only for that text.

One missing identity reaches a reader through three refusals -- this
`identity_unattested` text when a key is declared, the strict-identity diagnosis behind
`memory_recall` and every other reflexive tool when none is, and the lesson writers'
(`learn_add`, `learn_remove`) answer to the gateway's `400 missing_session_key` (mapped
onto the same established-session refusal instead of echoing the header name). When the
process carries neither the session token nor a launcher host pid
(`mcp_shared.spawned_without_gateway_identity`), nothing the gateway does at spawn
happened to it, and all three append the same `mcp_shared.external_client_identity_note`:
a server started outside a Kiro Crew session (an editor's own MCP config) has no identity
channel and its identity-bearing tools are not supported; `KIROCREW_SESSION_KEY` is a
gateway-injected fallback, not a credential, and set by hand it turns the partial
refusals into `identity_unattested` on every call; the read-only tools work without it;
the supported editor direction is connecting into a Kiro Crew session rather than
spawning the server, and that server's entry in `~/.kiro/settings/mcp.json` is the
leftover state the design invariant at the top of this document names. The note
decorates a denial and never grants: which calls are refused is unchanged, and a server
the gateway did spawn keeps its existing wording (token present, or the quoted spawn
denial below).

`resolution_failed` -- no usable answer, meaning nothing came back, a `5xx` said the
gateway is broken, or the resolve itself raised -- passes that test and refuses. Every
`4xx` returns before that arm, decided by status class, so the reason means the policy
could not be READ and never that the gateway made a decision about this caller. An
operator exclusion may exist while the process holding it cannot answer for it, so the
empty set that arrives with this reason is unknown rather than permissive, and serving it
as a permission is exactly how an excluded tool runs. The refusal is bounded at both
ends: a session that resolves its policy once is served from the per-session cache and
never reaches a failure path again, and for one that has not, the refusal lasts at most
the 60s negative-cache window.

The other reasons stay permissive, each because no operator exclusion is known to exist
for that caller or because refusal would be permanent rather than a window that closes. `agent_not_resolved` is the `404`, returned both for a session still
registering (a policy may exist) and for a caller the gateway can never map to an agent
(no policy can exist); refusing denies the second class forever. `no_session_key` is
the same gap inside the MCP process, where no agent is named at all. `policy_forbidden`
is ANY `4xx`: the gateway answered and made a decision about this caller, which for
`403 member_session_unverified` is the steady state of a session claiming a private
memory store without a verifiable proof. A boundary the gateway is enforcing is not a
boundary it failed to read. The test is deliberately the status class and not a list of
codes -- a list is only as complete as its author's knowledge of the endpoint, and a
status it never learned would be refused as though it were an outage. Each call through one of these windows is audited as
`tool_policy.unenforced_call`, so they are visible instead of silent. Closing the `404`
needs the endpoint to distinguish registering from unmappable.

`tools/list` never filters on an unresolved policy at all, whatever the reason:
kiro-cli calls it once at session start and caches the answer, so hiding tools on a
transient failure would leave that session permanently believing the server has no
tools, unrecoverable without a restart. A listed tool that refuses when called is
not a hole; an unlisted tool that runs is. Each unfiltered listing is audited as
`tool_policy.unfiltered_listing`.

A session that has ever resolved its policy is served from the per-session cache and
never reaches these paths again, so a refusal only affects a session whose policy has
never been read once.

A missing session key is not cached as a policy (a startup race must be
retryable, and it clears in milliseconds); a resolved key whose policy call fails
gets a 60s negative cache so a persistently unreachable gateway does not add a 5s
timeout to every tool call. That negative cache reports the reason of the clock it
hit -- the short window an identity race, the long window `resolution_failed` -- so
a cached read lands in the same class its live form would. Serving one shared reason
would repeat the conflation the resolver exists to undo, one level down. The gateway
side is deny-by-default in the same sense: a caller that cannot prove its identity
gets a 400/404, never an empty policy.

An empty body from that endpoint means one thing only: this agent genuinely
declares no exclusions. A spec that EXISTS and cannot be read -- unparseable,
valid JSON that is not an object, a `managedToolPolicy` of the wrong shape, or two
specs declaring one agent name -- answers `409` with `"code": "policy_unreadable"` in the
body and a SEL `denied` record. A `.md` file with no opening `---` fence is plain
markdown, not a spec (`agent_discovery.plain_markdown_document`): it cannot declare a
policy, so a fence-less `<agent>.md` answers `{}` and a fence-less file elsewhere in the
directory is skipped. A fenced document that fails to parse, and a head that is not
UTF-8 (a UTF-16 or UTF-32 BOM included) or carries a NUL, still answer `409`. Sharing the empty body with those
cases would make an unreadable deny indistinguishable from no deny on the wire, so
no caller could tell them apart however carefully it fails closed. The MCP side
maps a `409` carrying that code to `unresolved="policy_unreadable"` and does NOT
negative-cache it, nor its `identity_unattested` sibling: the answer is immediate so
there is no timeout to debounce, and both negative
clocks are process-global, so caching one session's malformed spec there would
refuse calls for every sibling session in a pooled backend.

The `409` body's `reason` names the file. Every `policy_unreadable` refusal the
gateway writes -- the directory-wide guard (`_refuse_if_any_spec_is_unreadable`),
the direct `<agent>.json`/`.md` read, the two shape checks on a direct read and the
duplicate-name arm (`_ambiguous_spec_reason`, built from
`AmbiguousAgentSpecError.paths`) -- carries the offending spec's filename (`repr`'d: it is untrusted input from a
user-writable directory and the text reaches a terminal), the failure kind in
words (`not valid JSON`, `not UTF-8 text`, `markdown frontmatter the spec parser
refuses`, `an AppleDouble sidecar`, `larger than the spec size cap`, `resolves to a
path the spec reader refuses`, `not a plain readable file` for the pinned open's
refusal, `unreadable (...)` for any other `OSError`) and one remedy
sentence: move, fix, remove or rename that file in the agents directory, no
restart needed. Never `str(exc)` verbatim, and never the directory's path: the
strict reader's own messages and the duplicate-name exception's message quote
the full path, and this text crosses the wire into the model-visible refusal.
Two arms have no file to name: the directory walk itself raised (class-name-only
text, as before), and a wrong-shape `managedToolPolicy` in a spec the declared-name
scan resolved -- the scan returns a parse, not a path, so that refusal names the
agent (`managedToolPolicy for '<agent>' is <type>, not an object`), which identifies
the spec since exactly one declares the name, and carries the same remedy. The SEL `denied` row carries the same `reason` (the
duplicate-name arm's row keeps the exception's full-path message: the audit
trail is local); the gateway log carries the FULL path at `WARNING`, once per
`(path, mtime)` -- the client re-asks on every `tools/call`, so a line per
refusal would repeat for as long as the file stays broken. The MCP side reads
`reason` off the body into `ToolPolicy.detail` (text only; the decision is the
status and `code`, unchanged); the one client-side `policy_unreadable` -- a 200 whose
`exclude` is malformed -- fills `detail` with its own shape diagnosis. The refusal
appends `detail` to the `policy_unreadable` refusal
as `Gateway reason: ...` after `neutralize_markers`, `redact_via_context` and
`redact_local_paths` (the credential redactor has no path rule, and an older
gateway's duplicate-name `reason` quotes full paths) -- then bounds the scrubbed
result at `_POLICY_DETAIL_MAX_CHARS`. The bound comes AFTER the scrubbers: a cut
made first can land inside a token, and the fragment left behind fails the
length-floored credential patterns and would be echoed. With no `reason` in the
body -- an older gateway -- the refusal is byte-identical to what it was.

## The MCP-first rule

**A new LLM-facing capability MUST ship as an MCP tool, not only as a CLI
command.** kiro-cli calls MCP tools reliably and may refuse to run a CLI command
via bash. CLI commands stay for human use; the model uses the MCP twin.

Do NOT add regex to match natural-language variants of a command. The LLM does
the interpreting. Handler keywords are only for instant user-typed commands that
need no model round-trip (`cron list`, `spawn list`).

### Server and tool inventory

Managed servers, registered by `agent._MANAGED_MCP_SERVERS` and installed into
`~/.kiro/agents/kirocrew.json`:

| Server | Process | Tools |
|--------|---------|-------|
| `kirocrew-cron` | `kirocrew mcp-cron` (`mcp_cron.py`) | `cron_add`, `cron_list`, `cron_update`, `cron_remove`, `cron_remove_all`, `cron_pause`, `cron_resume`, `cron_trigger`, `cron_secret_request` |
| `kirocrew-core` | `kirocrew mcp-core` (`mcp_core.py` + `mcp_tools/`) | spawn/subagent, learn, task, messaging, artifact, workflow, knowledge and session-directive tools (see below) |
| `kirocrew-computer` | `kirocrew mcp-computer` (`mcp_computer.py`) | `computer_list_apps`, `computer_launch_app`, `computer_get_state`, `computer_click`, `computer_drag`, `computer_type_text`, `computer_press_key`, `computer_set_value`, `computer_scroll`, `computer_perform_action`, `computer_end_turn` |
| `kirocrew-dashboard` | `kirocrew mcp-dashboard` (`mcp_dashboard.py`) | `chat_folder_tree`, `chat_folder_create`, `chat_folder_move`, `chat_folder_update`, `chat_folder_move_session`, `chat_folder_delete`, `chat_folder_file_self`, `chat_tag_list`, `chat_tag_create`, `chat_tag_update`, `chat_tag_assign`, `chat_tag_column_list`, `chat_tag_column_create`, `chat_tag_column_move`, `chat_session_pin`, `session_create`, `session_fork`, `session_stop`, `session_end_wait`, `session_retry`, `session_set_model`, `session_reload`, `session_close`, `session_revive`, `session_send`, `session_broadcast`, `session_status`, `session_adopt`, `session_release`, `session_read_message`, `session_summary` |
| `kirocrew-work` | `kirocrew mcp-work` (`mcp_work.py`) | `work_brief`, `work_report`, `work_ledger_read`, `work_ledger_rebuild`, `work_ledger_record` |
| `kirocrew-crew-log` | `kirocrew mcp-crew-log` (`mcp_crew_log.py`) | `crew_log_list`, `crew_log_read`, `crew_log_projection` |
| `kirocrew-debug` | `kirocrew mcp-debug` (`mcp_debug.py`) | `debug_gateway`, `debug_refusals`, `debug_threads`, `debug_processes`, `debug_snapshots` |
| `kirocrew-panel` | `kirocrew mcp-panel` (`mcp_panel.py`) | `panel_publish`, `panel_templates` |

`kirocrew-panel` is opt-in and reaches a crew member's DM session the way
`kirocrew-dashboard` does: as a session-level `mcpServers` entry carrying that
session's identity (`members.member_panel_session_server`), plus
`@kirocrew-panel` in `tools` and `_MEMBER_PANEL_GRANTS` in `allowedTools` on the
KAS projection. Neither the Capabilities editor nor any emitted spec can grant
it -- that list is built from configured connections, and an opt-in managed
server is not one -- so this mount is the only path to it. The operator ceiling
is `agent.crew_panel`; see the session-control module spec for the grant
reasoning and the fail-closed behaviour.

`kirocrew-dashboard` is one transport carrying **two** authorization models, which is
what makes its assignment decision larger than its name suggests. The
`chat_folder_*` verbs are bounded by RESOURCE ownership — a folder created by an app
carries it in `owner_app`, and an app may reshape only its own. The `session_*` verbs
are bounded by CALLER class instead, and one of them (`session_send`) writes a turn
into another session's conversation; [session-control.md](../system-specs/modules/session-control.md)
is their spec and carries that reasoning.

The consequence to know before granting: an agent handed the whole server for folder
organization has the session verbs too. Whether they prompt depends on how the grant
is spelled — `_mcp_pattern` maps a bare `@kirocrew-dashboard` entry to a one-level
glob, so it auto-approves every tool the server advertises, while naming tools individually leaves the rest
to `hooks.on_tool_call`. `_CONDUCTOR_DASHBOARD_GRANTS` and
`_MEMBER_DASHBOARD_GRANTS` (`agent.py`) are the shipped examples of the individual
form, and they differ from each other on exactly this axis: the member's list
includes `session_send`, `session_broadcast` and `session_stop` because `authorize_target` refuses a
member caller on any session it did not create, and the conductor's withholds them
because it has no such fence.

CLI commands and their MCP twins:

| CLI command | MCP tool | Server |
|-------------|----------|--------|
| `kirocrew cron add` | `cron_add` | `kirocrew-cron` |
| `kirocrew cron list` | `cron_list` | `kirocrew-cron` |
| `kirocrew cron update` | `cron_update` | `kirocrew-cron` |
| `kirocrew cron remove` | `cron_remove` | `kirocrew-cron` |
| `kirocrew cron pause` | `cron_pause` | `kirocrew-cron` |
| `kirocrew cron resume` | `cron_resume` | `kirocrew-cron` |
| `kirocrew cron trigger` | `cron_trigger` | `kirocrew-cron` |
| `kirocrew spawn run` | `spawn_run` | `kirocrew-core` |
| `kirocrew spawn list` | `spawn_list` | `kirocrew-core` |
| `kirocrew learn add` | `learn_add` | `kirocrew-core` |
| `kirocrew learn list` | `learn_list` | `kirocrew-core` |
| `kirocrew learn remove` | `learn_remove` | `kirocrew-core` |
| `kirocrew run TASK.md` | `task_run` | `kirocrew-core` |
| `kirocrew computer apps` | `computer_list_apps` | `kirocrew-computer` |
| `kirocrew knowledge dedup` | `knowledge_dedup` | `kirocrew-core` |
| `kirocrew knowledge stats` | `knowledge_list_sources` | `kirocrew-core` |

`cron_remove_all` and `cron_secret_request` have no CLI twin: `kirocrew cron
remove` takes one job id, and no `kirocrew cron` subcommand files a secret request.

The last row is the one place a twin does not share its command's name, and it is
a placement decision rather than an oversight. A tool in `kirocrew-core` costs
context in every request of every session for as long as the session lives, so a
fifth knowledge tool would be advertised forever to answer a question
`knowledge_list_sources` was already 90% of: it opens the same store, over the
same active-items rule, to serve the same "what is in this library" purpose. The
aggregate is a strict superset of what its own query already computed, so it
lands as a leading totals line on that tool instead, and the CLI verb and the
tool render ONE `aggregate_stats()` call.

The rule the MCP-first section states is that the model must get a structured
tool rather than a bash-shaped CLI command — the model has one. Two conditions
have to hold for that reading to be honest, and both are checked in
`test_knowledge_stats.py`: the tool must actually surface the numbers (its
descriptor advertises them, so deferral still selects it), and the capability
must not be one an agent should be granted SEPARATELY, which would make it a
`kirocrew-dashboard`-shaped opt-in server instead. It is not: anyone who may list
sources already reads this data from that store, so the counts add no reach.

`kirocrew-core` tools with no CLI twin, grouped by concern (authoritative list:
`kiro_crew.mcp_tools.build_tool_list()`, which is what `mcp_core._list_tools`
answers `tools/list` from):

- **Subagents:** `spawn_status`, `spawn_continue`, `spawn_steer`,
  `spawn_release`, `spawn_sub_agents`, `wait`
- **Messaging and notification:** `send_message`, `send_notification`,
  `delete_message`, `update_message`, `file_send`, `read_slack_profile`.
  `send_message` is the agent's only proactive egress to a NEW destination —
  `update_message` rewrites a Slack message the bot itself already posted, on the
  same gate ladder (strict identity, channel-agent containment,
  `capabilities.messaging` and the `channels` scope for `"slack"`), because an
  edit publishes new text to an audience rather than retracting what it has.
  `send_message` names its destination rather than inferring one:
  `session="slack"` / `channel` / `user` / `thread_ts` are the
  Slack fields, and `channel_type` is the non-Slack one — the transport of the
  conversation the calling session already belongs to. Exactly one of the two
  families may appear per call. The routing ladder and the fail-closed contract
  behind `channel_type` are in
  [messaging](../system-specs/modules/messaging.md) § Proactive sends. Two
  things to know before adding a destination to it:
  - **The governance gate must name the transport the message actually leaves
    over.** The `channels` scope is a per-transport allowlist, so vetting
    `"slack"` for a Telegram send evaluates a Telegram denial against Slack's
    rule — and refuses a permitted Telegram send whenever Slack is denied.
  - **`channel_type` is the one `send_message` argument that requires strict
    identity (via `require_strict_session_key`).** It posts into one specific conversation,
    which is the "targets a specific session" case below; the lenient walk
    climbs process ancestors, so a sub-agent would resolve to its parent and
    deliver into the parent's chat window. An unresolvable identity refuses the
    call rather than guessing.
- **Session-bound directives** (`session_directive.DIRECTIVE_TOOLS`):
  `ask_question`, `nothing_to_do`, `suggest_followup`, `monitor_start`,
  `monitor_watch`, `monitor_update`, `monitor_stop`, `autonudge_stop`,
  `set_project`, `reset_conversation`, `chat_tag`
- **Memory recall (V1 and V2):** `memory_recall` resolves authenticated session identity
  once through `require_strict_session_key` and passes that same identity to the gateway.
  Missing identity returns the shared gate's refusal and installation diagnosis.
  It accepts a task query,
  never a store selector. The gateway resolves the caller's bound V1 or private V2 memory
  and returns bounded context with evidence; unavailable identity or memory
  refuses the call. The MCP response keeps each selected body once in a trusted
  reference context, with body-free evidence carrying stable references, scores,
  provenance and truncation flags. UI previews are not model output. A call-local
  projection and final serializer enforce the 3,000-character context limit and
  16 KiB memory-result budget after redaction, including nested JSON escaping and
  TextContent overhead with a 1 KiB reserve for normal RPC framing/IDs. This
  projection retains no caller or session state in the MCP process. Arbitrarily large caller-supplied IDs are outside that bound.
  Prompt construction does not perform embedding search;
  the tool is called when earlier facts or experiences are needed. Owner-selected copying is a dashboard action, not an MCP
  capability. The full contract is in
  [memory](../system-specs/modules/memory-skills-hooks.md#member-memory-experience-and-lifecycle).
- **Monitor read:** `monitor_inspect` (strict authenticated session
  identity only; no ancestor fallback; reports a structured monitor or a legacy
  timer loop's presence reading, whichever the session holds)
- **Crew routing:** `route_crew`, `select_crew`
- **Session work ledger:** `session_ledger_read`, `session_ledger_record`
- **Sessions and history:** `list_sessions`, `get_chat_session`,
  `search_chat_history`
- **Artifacts:** `artifact_list`, `artifact_get`, `artifact_save`,
  `artifact_update`, `artifact_delete`, `artifact_move`, `artifact_versions`,
  `artifact_revert`, `artifact_folder_list`, `artifact_folder_create`,
  `artifact_folder_rename`, `artifact_folder_move`, `artifact_folder_delete`,
  `artifact_get_comments`, `artifact_post_comment`, `artifact_reply_comment`,
  `artifact_delete_comment`, `artifact_mark_review`, `deploy_artifact`
- **Knowledge and skills:** `local_knowledge_search`, `knowledge_add_document`,
  `skill_discover`, `skill_search`, `skill_fetch`. (`knowledge_dedup` and
  `knowledge_list_sources` have CLI twins — see the table above.)
- **Workflows and hooks:** `workflow_author`, `workflow_run`,
  `workflow_library_list`, `workflow_status`, `workflow_result`, `workflow_list`,
  `workflow_cancel`, `workflow_rerun_subtree`, `register_hook`
- **App bridges:** `issue_radar_record_investigation`,
  `ops_mission_control_api`, `design_tweak_update_thread`, `pod_up`, `pod_down`,
  `pod_status`, `pod_ls`, `issue_radar_crew_read`, `issue_radar_crew_record`.
  Listed only while the app each one reaches is enabled — see § An app's tools
  follow the app's enablement below.
- **Browser:** `browser`
- **Diagnostics:** `resource_status`, `kiro_cli_logs` — a redacted tail of kiro-cli's own mcp/lsp protocol logs, so
  the agent can self-diagnose a rejected turn. Reads log files only: never the
  fenced identity/token stores, and never the conversation-bearing sources
  (`kiro-chat.log`, session transcripts), each of which is one shared host file
  per gateway that would disclose another session's conversation. That scope
  holds only while mcp.log / lsp.log record protocol traffic rather than full
  frame bodies, since they share the chat log's single-fixed-path,
  all-sessions-interleaved shape and an MCP `tools/call` frame carries
  conversation-derived arguments. Measured on kiro-cli 2.21.1: mcp.log is empty
  across a session of continuous MCP tool calls, every lsp.log record is a
  single-line `<timestamp> ERROR <module>: <message>` with no JSON-RPC envelope
  and a longest line of 313 bytes, and sentinel strings passed as tool-call
  arguments appear in neither file. Because that measures one version of a
  component this repo does not pin, a source whose text carries serialized frames
  is REFUSED whole and visibly, so a kiro-cli that starts logging payloads
  surfaces as a refusal instead of a silent widening

  Member scope does not prohibit these ordinary diagnostic reads. Credential
  redaction, source allowlisting and refusal of conversation-bearing logs remain
  independent data-retention and credential protections.
  Protocol-log and chat-history tools resolve ordinary strict session identity
  once before reading, independently of memory version or database availability;
  workspace filtering, audit attribution and summary requests reuse that key.

- **App bridges (credentialed):** `ops_mission_control_api` — the MCP server
  process holds the gateway's internal secret and forwards only a frozen
  (method, path) allowlist of Ops Mission Control routes; the agent never
  sees a credential (same shape as `issue_radar_record_investigation`).
  `design_tweak_update_thread` — the same shape for the Design Tweak app: the
  server forwards a single route (`POST /apps/design-tweak/api/thread`, the
  gateway's app reverse-proxy path) so
  the agent can mark a batched visual-edit comment in progress or done as it
  applies it. `status` is restricted to `done` (forward progress only — no
  clear/dismiss), and the gateway admits only that one path for internal-secret
  callers, never the app's state-mutating routes

### A `kirocrew-core` tool has two halves

Each tool is declared twice in the same per-domain module under
`kiro_crew/mcp_tools/` (`spawn.py`, `artifacts.py`, `workflows.py`, …), and
nothing at runtime notices when only one half lands:

- Its **descriptor** — name, model-facing description, JSON Schema — is returned
  by that module's `schemas()`. `build_tool_list()` concatenates every domain's,
  and `mcp_core._list_tools` answers `tools/list` from it. A domain may narrow
  what it ADVERTISES with an `advertised()` function, which `build_tool_list()`
  reads in place of `schemas()` on the full build when it exists; `schemas()`
  stays the full declaration, and `advertised()` may only omit a declared
  descriptor, never add or reshape one
  (`test_a_domain_advertises_only_what_it_declares`). The names-only build
  (`build_tool_names`, the in-process discovery read) takes `schemas()` and never
  consults `advertised()`. `apps` is the one domain that defines one, below.
- Its **handler** is an entry in that module's `HANDLERS` map, called as
  `handler(name, args)`. `dispatch()` finds it by name and
  `mcp_core._call_tool_inner` delegates to that.

A descriptor with no handler advertises a tool that answers with the
dispatcher's fallthrough; a handler with no descriptor is unreachable, because
the model is never told the name. `test/test_mcp_tool_registry.py` fails when
either half is missing, when the two halves land in different domains, or when a
name is claimed twice. Parity is read from `schemas()`, the declaration — not
from `build_tool_list()`, which may legitimately omit a declared tool whose app
is switched off.

### An app's tools follow the app's enablement

The nine `apps`-domain tools each reach one built-in app — Issue Radar
(`issue_radar_record_investigation`, `issue_radar_crew_read`,
`issue_radar_crew_record`), Dev Fleet (`pod_up`, `pod_down`, `pod_status`,
`pod_ls`), Ops Mission Control (`ops_mission_control_api`) and Design Tweak
(`design_tweak_update_thread`) — through gateway routes that refuse the call with
HTTP 403 while the app is disabled, on `kiro_crew.apps.manager`'s read of the
app's `installed.json`. The refusal body differs per app — Dev Fleet's routes and
the app reverse proxy Design Tweak sits behind (`handle_app_api_proxy`) name it
`app_not_enabled` (#10742), Ops Mission Control `app_disabled`, Issue Radar
carries no `code` — so nothing here keys on a code; the predicate behind all four
is the same `is_app_enabled` read. `tools/list` advertised every one of them
regardless (#16099), so a
session with none of the apps enabled was told about tools that could only
refuse — and because kiro-cli reads `tools/list` once per session and Crew's own
servers are exempt from `tool_search` deferral, each one cost its full schema in
every request of that session.

`mcp_tools.apps.advertised()` therefore emits a tool's descriptor only while its
app is enabled, and the decision reads **the same record the call refusal reads**:
`TOOL_APPS` names the installed app behind each tool (pinned to the apps'
own `APP_NAME` constants by `test_mcp_apps_tool_gate`), and the listing reads
that app's `installed.json` through `kiro_crew.apps.manager`, the module the
route's `is_app_enabled` reads it through. One file, one module, two readers:
the route's binary `is_app_enabled` treats an unreadable record as disabled and
refuses, while the listing's tri-state `app_enabled_state` hides only on a
readable `False`. So a hidden tool is always one whose call would be refused,
and a listed tool is not always one whose call would succeed — an unreadable
record lists tools the route refuses, for the reason the fail-open bullet below
gives. The MCP server
process reads the data home directly here as it already does for the computer-use
keystone, the skills tree and the knowledge store; the sandbox masks specific app
data leaves, not `apps/<name>/installed.json`.

Four details of the shape:

- **Hiding is a product decision, not an enforcement point.** The route stays
  the gate, as it does for the computer-use tools (`mcp_computer._list_tools`
  returns `[]` while the keystone is off). `dispatch()` of a hidden tool is
  unchanged: the handler runs and surfaces the route's own refusal — a hidden
  tool is not an unknown one.
- **Fail-OPEN on a read fault, as the per-session policy filter does.** The
  listing reads the tri-state `app_enabled_state` and hides only on a definite
  `False` (not installed, or installed and switched off); `None` — corrupt JSON,
  a dangling link, a directory in the file's place — lists the tools, and so does
  a read that raises: `advertised()` guards each app's read, because an exception
  escaping the descriptor build would withdraw every tool of the pooled server
  for every session (`spawn._agent_roster_hint` and `control.schemas()` keep the
  same rule for their own reads). The
  once-per-session `tools/list` cache that motivates hiding would make hiding on
  a transient fault permanent for the session's whole life, while a listed tool
  whose call the gateway refuses is not a hole. `is_app_enabled` cannot express
  this (it collapses unreadable into False), which is why the listing reads the
  tri-state twin in the same module rather than a third reader.
- **Enabling an app mid-session does not surface its tools until the session
  restarts** — kiro-cli caches the listing — and disabling one mid-session leaves
  them listed and refusing, both exactly as the computer-use tools behave.
  `schemas()` and `HANDLERS` keep naming every app tool whatever the enablement,
  so a disabled app never reads as registry drift.
- **The names-only build skips the gate, so nothing is charged to the gateway's
  loop.** `mcp_discovery._managed_tools_in_process` reads tool names in-process
  on a host whose sandbox refuses the probe spawn, on the gateway's event loop,
  through `build_tool_names()` — the `names_only` path of `build_tool_list()`
  that already keeps `spawn.schemas` and `control.schemas` from their live reads.
  That path takes each domain's `schemas()` and never consults `advertised()`, so
  no `installed.json` is read there and a disabled app's tools still appear by
  name in the discovery inventory (rendered under the "Declared" badge). The
  stdio server that answers a model's `tools/list` takes the full build, so the
  gate applies where the listing is observed.

Handlers reach the server's shared plumbing — `_post`/`_get`, the identity
resolvers, the governance vets — as **attributes of `mcp_core`**, not as direct
imports. That is deliberate: an attribute lookup resolves at call time, so a test
that rebinds one (`patch("kiro_crew.mcp_core._post")`, `setattr(mcp_core, "sel",
…)`) still intercepts the handler. A direct import would bind at import time and
silently escape every such patch. `mcp_core._HANDLER_SURFACE` names the bindings
that exist only for this purpose, so an import cleanup cannot quietly delete one.

The remaining upward dependency is known: the plumbing could move to a module the
handlers own, which would make `mcp_tools` a leaf. That is a separate change —
it has to retarget every patch site, which is mechanical but touches far more
test code than moving the handlers did.

Descriptors carry no per-caller state and are rebuilt per call, not cached: some
quote a live value (the concurrent sub-agent cap), and a cache would pin the
first reading for the life of the server process.

### A `kirocrew-dashboard` tool is one row

`kirocrew-dashboard` is built on `mcp_tools.table.ToolTable`: each tool is one
`Tool` row in `mcp_dashboard.TABLE` carrying its descriptor, its identity
(`"strict"` for the session-control verbs, `"attribution"` otherwise), the
dashboard routes it may reach (`"METHOD /api/path/{id}"`), and its `_run_<tool>`
body. `TABLE.list()` answers `tools/list`; `TABLE.call(name, args, ctx)` is one
`tools/call` frame: argument validation against `MCP_DASHBOARD_SCHEMAS` and the SEL
invocation record (`call_tool_with_logging`), the unknown-tool reply, the strict
gate for a `"strict"` row, a second validation pass (a second sanitize pass can
still compose a string the first exposed), then the body.

A body reaches the gateway only through `ctx.client`, the `DashboardClient` port in
`mcp_tools/dashboard_client.py`. `LoopbackDashboardClient` is production: each verb
is the matching `mcp_core` helper, read at call time. `InMemoryDashboardClient`
answers from a route table and records every request; it is what the dashboard
tool tests drive. Both raise `DashboardError` for a reply carrying a truthy `error`,
and the client the table hands a body refuses, before sending, any route its row
did not declare. The caller is a port too: `ctx.caller` is `GatewayCaller`
(`mcp_core`'s resolvers) in production and a fixed `Caller.strict(key)` /
`Caller.unverified(...)` in tests.

A new dashboard tool is one more row in the family function it belongs to
(`_folder_tools`, `_tag_tools`, `_board_tools`, `_session_tools`), a body taking
`(args, ctx)`, and its validation schema in `MCP_DASHBOARD_SCHEMAS`.
`test_mcp_tool_registry.py` holds the rows to those three, and
`test_mcp_call_site_auth_coverage.py` checks every declared route against the
internal allowlists.

External servers a user may install (a Slack server, anything else) are ordinary
user-added servers: they live in one of the scope files and are merged into the
agent config at render time. They are not managed, so a `mcp_server_alias`
normalization pass rewrites slash-containing keys to kiro-safe aliases: kiro-cli
splits an agent `@server` reference on `/`, so a slash-containing key is
mis-parsed as `@server/tool` and exposes none of the server's tools.

**Browsing has one core MCP entry point.** The `browser` tool forwards one
operation to the dashboard's native Browser panel. It accepts public HTTP(S)
navigation only; localhost, private, and link-local targets stay on the
approval-gated `playwright-cli` path. When no native panel is attached, or the
operator disables it, the tool returns fallback guidance rather than starting a
second browser. Full CLI snapshots and accessibility trees still stay on disk.
See [browser](../system-specs/modules/browser.md).

## What belongs in `kirocrew-core`, and what does not

`kirocrew-core` is the surface EVERY session carries. kiro-cli reads `tools/list`
once per session, so a tool listed there spends context in every request of every
session for as long as the session lives — whether or not that session will ever
use it. With `agent.tool_search` on (the default), kiro defers MCP specs once they
cross `agent.tool_search_min_pct` or `agent.tool_search_min_tokens`, and Crew's own
servers defer like any other when the spawn runs the pinned kiro-cli install or its
`kiro-cli-chat`, and both are >= 2.27.0. For any other executable (a pod bundle, a `kiro-cli`
found on `PATH`), below that floor, or when the version is unknown, Crew keeps its
servers resident to avoid the thinking-signature
"tools list differs" rejection that bricks a session. A deferred tool costs a name
plus a description rather than a full JSON schema; it is smaller, not zero, and it
scales with the tool count. An operator's `ASBX_KIRO_MANDATORY_MCPS` value
(comma-separated server names) in the gateway's environment always wins, including
an explicit empty value; per-session overlays cannot change it.

That makes the placement question a real one rather than a matter of taste:

- **Core** is for capabilities a session may need *without being asked* —
  subagents, messaging, memory, artifacts, session-bound directives.
- **Its own server** is for a capability an agent is granted on purpose. Give it
  the `kirocrew-dashboard` shape: an **assignable set**, marked `opt_in` in
  `_MANAGED_MCP_SERVERS` so neither spec writer adds it to the default agent.
  kiro-cli loads a server only when `tools` names it, so an unassigned set costs
  a session literally zero — which an always-refusing tool in core cannot
  achieve, since it still ships its description every turn.

**Assignment is the mechanism; a config bool is not.** Which agents get a set is
decided by their own specs: the entry in `mcpServers` plus the matching
`@<server>` ref in `tools`. Only `kirocrew.json` is rewritten on install, and a
refresh keeps an existing grant's command current without ever introducing one,
so a hand-granted set survives upgrades and an ungranted one does not come back
behind the user's back. Adding a second boolean in `config.json` on top of that
gates nothing an unreferenced server was not already denying.

**Granularity: the set, not the tool.** A spec that references a server gets
every tool in it. So a capability that must be grantable *separately* belongs in
a server of its own, not alongside a set someone might want for other reasons.

**A grant is not authority over everything the tools can name.** Assignment says
which agent may call a set; it does not say what that agent may reach. The
dashboard set resolves the calling session strictly — only a gateway-injected
per-call caller context, an injected session key, or an HMAC-verified host pid
counts, never a `/proc` ancestor walk, which would resolve a subagent to its
parent slot — and then bounds itself by what that caller owns:

| Caller | Sees | May file | May reshape the tree |
|--------|------|----------|----------------------|
| the person's own agent | every session | any session | yes |
| an app agent | only its own app's sessions | only its own app's sessions | no |
| a delegated caller whose slot cannot be located | nothing | nothing | no |
| a `dashboard:` caller whose named slot is absent | nothing | nothing | no |
| unverifiable | nothing | nothing | no |

A delegated caller gets its own row because absence of a slot means different
things for different callers. A Slack thread or a channel session has no
dashboard slot and never had an app to be confined to, so it is unscoped. A
subagent or a scheduled job also matches no slot, but it runs on behalf of
whatever created it — and a cron can be created by an app — so reading absence
as "no app" would let an app that may not touch a foreign session gain that
reach by spawning a helper or scheduling a job. Delegated callers inherit
authority; they never mint it.

A `dashboard:` caller is refused for a different reason, and it is deliberately
not on that delegated list. It is not delegated work — it *names* a slot. So
absence is never the "never had a slot to be confined to" case that makes a
Slack thread unscoped; it means the named slot is not there, which happens when
the tab was closed while the call was still in flight (slot removal is
synchronous and does not drain in-flight MCP calls) or when the key is wrong. An
app-owned session going through that race would otherwise hand its agent
authority the app itself does not have.

Note this refusal is strictly narrower than inverting the default for every
caller: Slack threads, channel sessions and crons do not carry the `dashboard:`
prefix, so it costs them nothing.

**The delegated list is knowingly incomplete.** It enumerates the delegated key
forms that exist today, so a key form added later reads as unscoped until it is
added to it. The sound shape is the inverse — grant authority only on positive
confirmation that the caller is the person, refusing everything unplaceable —
but that also removes these tools from callers who legitimately have no slot and
no app, including the person's own crons. Until that inversion is taken, the gap
is documented here rather than hidden.

The asymmetry in the last column is not an oversight. Sessions carry an owning
app, so "yours" is a decidable question and an app is confined to its own.
Folders answer the same question: a folder created by an app carries it in
`owner_app`, and an absent key reads as the person's, so a folder with no such key
needs no migration. An app may create at the top level or inside a folder it owns, and may
rename, reparent or delete only what it owns; the top level is not a folder row
and so has no owner to violate, which is where an app's own tree starts. A
reparent is refused when the folder's SUBTREE holds one the caller does not own,
because a move takes the subtree with it and would relocate the person's folder
under cover of moving the app's. A rename, colour or collapse is not gated that
way -- it relocates nothing. The person is never confined by any of this, and an
app keeps what it always had: reading the whole tree, and filing its OWN sessions
into any folder that exists.

**An app cannot delete a folder at all.** Not even an empty one it owns. A delete
relocates everything the folder contains, and those contents live in a DIFFERENT
store from the folder -- sessions are in the slot table and the session archive,
neither sharing a lock with it -- so "is this folder empty?" cannot be established
atomically with the removal. Successively narrower rules each leaked through
another seam: a session filed while the archive scan awaited, a child created while
the lock was acquired, a session closing after the scan and writing its `folder_id`
on the way out. Each was closable alone; the class was not, so the verb is withheld
from apps. An app organizes its work by creating, renaming and reparenting its own
folders and filing its own sessions. The person deletes a full folder exactly as
before.

The person's own sessions can delete an EMPTY folder through `chat_folder_delete`,
and only one the calling session created that the person has not touched since.
An agent create stamps the creating session's key on the row as
`created_by_session`; a browser write that renames, moves, restyles or hides the
folder, files a session into it, opens a chat in it, or nests a folder under it
removes the field for good. A person's folder, a reused same-name folder and
every older row carry none, so an agent's cleanup can never remove a folder the
person relies on. The field lives on the folder row in `folders.json`, so it
survives a restart with the folder. The delete sends `?if_empty=true`, which
never unfiles a session or lifts a subfolder: the endpoint reads the mark, counts
archived sessions, then re-checks the mark, subfolders and live slots in
the same locked folder-store step that removes the row. A slot PATCH and a child
create both re-check the folder under that lock, so neither can slip between the
check and the removal. One seam stays open: a session filed and then closed while
the archive scan runs is in neither store at the locked check, so the delete
proceeds and that archived transcript keeps the gone folder's id. Every reader
already renders such a session as unfiled, which is the same state the person's
full delete leaves on every archived session it does not touch, and nothing in the
transcript is lost. Closing that seam means registering every in-flight close
across all of the slot-removal paths, which this verb does not justify.

The policy lives in the endpoints, not in the MCP server. Only the endpoint holds
the store lock and sees the authoritative tree, so a second copy of the rule in
the tool layer could only drift or race. What the tool layer still decides is the
one question the endpoint cannot: whether the caller can be placed at all, since
an unverifiable or delegated caller has no scope to bound a write to.

**Filing your own session is a separate verb, `chat_folder_file_self`, because
the grant is name-scoped.** `chat_folder_move_session` takes its target from an
argument, so it is the one dashboard tool that writes a session OTHER than the
caller's, and the conductor agents keep it behind an approval: they ingest
untrusted content on unattended cycles, and `allowedTools` can name a tool but
not an argument. That left the conductor unable to file ITSELF without a prompt,
so it floated at the top level while its workers sat in the goal's folder.
`chat_folder_file_self` has no `session` argument — the target is resolved from
the verified caller key (`_own_chat_slot`: only a `dashboard:<slot>` key
qualifies, because the slot key is stable for the tab's life while a
channel- or cron-bound slot's `linked_session_key` can be rebound between the
read and the write, so matching on it could file a different conversation;
a private session and a crew member's pinned DM thread are refused too) — so the one
placement it can write is the caller's own, which is exactly the placement the
conductor grant invariant (create or read, never mutate what is not your own)
admits. It resolves `folder` with `mkdir -p` semantics behind the same
tree-shaping gate as `session_create`'s `folder`, and PATCHes the same
`/api/chat/slots/<slot>/folder` route under the gate's verified key, carrying
the slot's `created` stamp as `expected_created`: the endpoint's own identity
re-check covers its own awaits, but the tool resolves the slot in an EARLIER
request, and a tab that closes and is recreated under the same key in that gap
would share the `dashboard:<key>` transcript key — so the endpoint refuses (409
`session_gone`) when the token does not match the live slot's `created_at`. The
conductors call it once in their first turn, then create every worker with
`folder="<goal>/<agent>"`, giving one heading per goal with the conductor
directly under it and one subfolder per agent kind.

**Position is the one folder write the tool layer has to compose.** A folder's
place among its siblings is an `order` int the endpoint stores verbatim and never
renumbers, so there is no single value a caller could compute — which is why
`chat_folder_move` takes `before`/`after` naming a SIBLING rather than a number.
Most placements are still ONE write: when the store already has a free integer
slot at that position — ahead of the first sibling, past the last, or in a gap a
delete left behind — only the moved row is written, so the reposition cannot land
half-applied. Only two neighbours holding adjacent integers, which is what a
sidebar drag leaves behind, force the siblings to be renumbered; that renumber is
contiguous from 0, the same values `computeReorderedFolders` writes, so the two
paths leave one convention in the store instead of two.

An anchor may stand in for `new_parent` because an omitted `new_parent` means the
top level: without that, ordering a folder inside a folder would be
inexpressible, since every call would drag it out to the root as the price of
positioning it.

Composing writes is where an app's confinement needs a rule the endpoint cannot
state. The endpoint judges each PATCH on its own, so an app whose placement needs
a renumber would have its first write accepted and a later one refused, leaving
the person's sidebar in an order nobody chose and nothing to roll it back with.
So the tool layer checks the WHOLE renumber against the caller's ownership before
the first write and refuses the call intact — the only folder rule this layer
decides, and it decides it because atomicity across several endpoint calls is a
property only the caller can hold. A one-write placement is not gated: it touches
the app's own row only, and the endpoint judges that write as it judges any other.

The renumber path keeps one accepted residue. Several single-row writes cannot be
made atomic from here, so a transport failure partway through leaves the
destination's siblings carrying a mix of old and new numbers until the call is
re-run — a display sequence, reported to the caller, over a field whose duplicates
and gaps are already legal and already tie-broken by name. The sidebar's own drag
has the same shape today, firing one `updateChatFolder` per changed folder with no
transaction. Closing it for both paths needs a bulk order write that applies under
the folder-store lock, which is a change to the store's API rather than to this
layer.

A second accepted residue sits on the read side. `GET /api/chat/folders` returns
stored rows verbatim — the store's loader validates `id` and nothing else — so both
readers of that response separately coerce `order` to a clamped integer and `name`
to a string before comparing. That value-cleaning is duplicated in two languages
because it happens in two consumers rather than once in the producer, and
normalizing on the way out would delete it from both. Deferred rather than done
here: this endpoint is shared by more consumers than the two that sort with it, and
the change belongs with the store's API alongside the bulk write above. What such a
normalization would NOT remove is the comparison itself — Python orders strings by
code point where JavaScript orders by UTF-16 code unit, so the tool encodes
`utf-16-be` to compare as the sidebar does, and no shape of producer output makes
one language's comparison operator equal the other's.

Neither side folds case. `str.lower()` reads the interpreter's Unicode tables and
`String.prototype.toLowerCase` reads the browser's, so a character whose case
mapping differs between those two versions folds differently on each side, and
neither side owns both tables — the skew is unclosable in this layer rather than
merely unlikely.

`A`-`Z` folds anyway, through a literal 26-entry table in the tool and `+32`
arithmetic on the code unit in the sidebar. That range's mapping is fixed in every
Unicode version published, so folding it adds no version dependency, and it is worth
folding because the name is not always merely a tie-break: a store written before
`order` existed carries no `order` on any row, so every sibling ties at 0 and the
name decides that whole sidebar. Raw code-unit order would render those
uppercase-first until the first drag renumbered them.

`chat_folder_tree` lists folders in that same stored order rather than by path,
because it is what an anchor is picked from — an alphabetical listing would show a
sequence the person never sees and make every `before`/`after` a guess. The
comparator is `order` then name, and both sides read a missing `order` as 0:
`folderTree.bySidebarOrder` for every surface that draws siblings, and
`_chat_folder_order` for the tool. A folder written before the field existed
carries no `order` at all, so a comparator without that coercion would compare
`NaN`, fall through to its tie-break, and show the agent a different sequence than
the person sees.

That stored order is one of three the sidebar can draw. `dashboard.folder_sort`
(config.json, written through the config PATCH allowlist from the sidebar's
sort-and-filter menu; read back by the sidebar through its `GET /api/config/kirocrew`
query and by the tool from the same file through the loader — that route is
cookie-only, in neither internal-secret allowlist, and admitting it would open the
whole config surface to secret-bearing callers to read one enum) selects `custom` —
the stored order above, the default,
so nothing changes for a person who never picks a mode — `name`, an ASCII-case-insensitive
natural order in which `01.` < `02.` < `10.` (only `A`-`Z` fold), or `created`, newest first on the
`created_at` epoch stamp every folder creator writes (a row from before the stamp
sorts as older than every stamped one). The two view modes are layered on the custom
key — a pair they cannot separate keeps its stored order — and choosing a mode
rewrites no `order`, so switching back to `custom` restores the manual arrangement
exactly. `folderTree.folderComparator(mode)` is the sidebar's comparator and
`_chat_folder_sort_key(mode)` the tool's; the shared fixture
`test/fixtures/chat_folder_sibling_order.json` carries a `mode` per case so the two
are checked against one artifact. Digits are ASCII `0-9` on both sides (never
`str.isdigit`, which reads the interpreter's tables) and a digit run compares by
value without ever becoming a number, for the same reason the name compare never
folds outside `A`-`Z`.

The tool's header line names the active mode (`folder order: name`). A POSITION is
still a stored-order concept: the placement helpers compute a `before`/`after` gap in
the custom order whatever the mode, so when the mode is not `custom` the listing says
so on its second line and states that an anchor sets the stored position without
changing the order shown — otherwise an agent would move A after B, re-read the tree,
and see nothing move. When the config read itself fails the tree is still listed, in
the stored order, with the header saying the order is assumed rather than known.

Moving the decision to the endpoint makes the write's IDENTITY load-bearing, so
the gate returns the key it verified and every folder write sends that key
unchanged. The write helpers default to `_resolve_session_key`, whose `/proc`
ancestor walk can resolve to a different slot than `_resolve_session_key_strict`
did; letting them re-resolve would check one identity and write under another,
and for an app-owned session the walk landing on an ancestor makes the write
arrive looking like the unconfined person -- which would let an app reach the
folders the ownership rule exists to protect. `chat_folder_move_session`, create
and move all send the verified key, including for each intermediate folder a
`mkdir -p` parent path creates.

The same reasoning covers the OTHER way an app's write can arrive unattributable.
An empty scope reads as the person, which is correct for a caller that never had a
slot -- a Slack thread, a channel session, the person's own cron -- but not for a
`dashboard:` key, which NAMES a slot: absence there means the app it would have
been confined to is exactly what got popped, which is what a tab closing mid-call
produces. The folder mutations therefore refuse a caller matching
`caller_names_a_missing_slot`, the predicate that exists so a route outside the
MCP tool set can apply the rule `_caller_app_scope` already applies inside it. It
stays per-route rather than in the middleware on purpose: a popped slot no longer
says whose tab it was, so refusing centrally would also refuse the person's own
in-flight calls on every internal route at once.

**Tags ride the same server, with the same shape.** `chat_tag_list` reads the
vocabulary (`GET /api/chat/tags`), `chat_tag_create` adds to it
(`POST /api/chat/tags`, which dedups on the lowered name so a repeat call is a
no-op), `chat_tag_update` renames, recolors or flips the status flag of one
(`PATCH /api/chat/tags/<id>`, metadata only — every session keeps the tag), and
`chat_tag_assign` labels a live session (`PUT /api/chat/slots/<slot>/tags`). No
delete: removing a tag from the vocabulary strips it from every session and
column at once, which is the one operation in the set with real blast radius.
Two differences from the folder verbs follow from tags having no owner. First,
`POST` and `PATCH /api/chat/tags` refuse an app-scoped caller outright (403
`app_forbidden`, which the tools surface): a folder an app makes is the app's
own, but a tag lands in the person's one shared list with nothing to tell it
apart, so
there is no boundary to bound that write to. The rule sits in the endpoint, on
the middleware's validated claim, for the same reason the folder policy does —
a tool-layer copy could only drift. `POST /api/chat/tags` also draws on the same
per-caller create budget as `api_chat_folder_create` (`create_rate_limit`,
`TAG_CREATE`), keyed by the validated `X-Internal-Caller` component, so a
granted agent loop cannot grow `tags.json` without bound; the browser is exempt.
Second, `chat_tag_assign` takes `add` / `remove` DELTAS rather than a list to
replace, and sends the slot's `tags_revision` as `base_tags_revision` so the
endpoint applies the write compare-and-set: a tag the person toggles between the
tool's read and its PUT is not silently dropped by a wholesale replace — the call
fails 409 `stale_base` and re-reads. The session is resolved through the same
scoped `_visible_chat_slots` as `chat_folder_move_session`, and the PUT route
applies the same App Kit ownership check `api_chat_slot_folder` does, so an app
agent cannot reach a foreign session's tags from either side. The route also
applies the agent tag-grants policy for every non-person caller — the same
protected-store authority the `chat_tag` `set_state` directive enforces, so
`chat_tag_assign` cannot be a bypass of it: a tag the person reserved (a
protected grant row) is refused `tag_policy_denied`, a workflow-status tag with
no protected row is refused `status_identity_unprotected` on add as well as
strip, a replace that would leave two status tags is refused
`status_tag_requires_set_state`, and
a degraded store fails closed with `tag_grants_unavailable`. A rowless ordinary
label is not a reservation and is left assignable. The person (browser) is never
gated — the whole store constrains agents, not the owner.

**Pins follow the same shape.** `chat_session_pin` sets one live session's
`pinned` flag (`PATCH /api/chat/slots/<slot>/pin`). It resolves the session
through the same scoped `_visible_chat_slots`, verifies the caller strictly, and
writes nothing when the session already has the requested state. The endpoint
applies the unattributable-caller refusal, the crew-member `member_owns_slot`
fence and the App Kit ownership check that `api_chat_slot_folder` applies, and
the member chat-route gate admits `PATCH` on that path for the same reason it
admits the folder and tag writes.

**Board columns follow the tag rules.** `chat_tag_column_list` reads the board
(`GET /api/chat/tag-columns`), `chat_tag_column_create` appends a column that
filters on one existing tag (`POST /api/chat/tag-columns` with `ensure: true`,
which makes the endpoint return an existing column with the same name, tag and
mode under its write lock instead of appending a twin), and
`chat_tag_column_move` places one column before or after another
(`PUT /api/chat/tag-columns/order` with the full id list plus `base_ids`, the
order the tool read; the endpoint refuses 409 `stale_base` when the board changed
in between, so a person's reorder is never overwritten). The board UI sends
neither field and keeps its append and last-write-wins behaviour. The board is the person's own layout with no owner,
like the vocabulary, so all four column write endpoints apply
`_refuse_vocabulary_write` (apps and crew members get 403 `app_forbidden`), the
create endpoint draws on its own `TAG_COLUMN_CREATE` budget, and the member
chat-route gate admits only `GET` on the list. There is no delete or retag tool:
either would remove a view the person built. The two writes are blocked for
channel agents, at the permission prompt and again at dispatch.

**Assignment is still not authorization.** Being unreferenced by default keeps a
capability cheap and deliberate; it does not prove the user consented to reach the
agent does not otherwise have. For that, `config.json` is the WRONG home —
`security.py` spells out why, and the keystone leaves (`computer_use.json`,
`browser-mode-enabled`, the Ops Mission Control mode) exist because each grants
something outside Kiro Crew (desktop input synthesis, the operator's logged-in
browser, writes against production incident tooling) or is the security floor
itself.

The test is blast radius, not wording: ask what the agent gains that it did not
already have. Folder tools grant no new read (`list_sessions` already returns every
session's title and key) and cannot delete, so assignment alone is the right
ceiling for them. Driving or stopping another session is not, and would need a
keystone leaf on top of its own server. Ratchet each set so the next capability
cannot arrive inside one whose grant was never meant to cover it.

Per-agent scoping composes on top and needs no new mechanism: an agent spec's own
`mcpServers` map decides which agents see a server at all
(`agent_discovery.py`), so a server can be handed to an orchestrator-class agent
without every agent inheriting it.

Adding a managed server is a **parity tax** — the name must appear in
`agent._MANAGED_MCP_SERVERS`, `mcp_discovery._MANAGED_SERVER_SUBCOMMANDS` and
`_MANAGED_SERVER_TOOL_MODULES`, `mcp_cleanup.KIROCREW_BIN_MCP_SERVERS`,
`onboarding_sources._CORE_MANAGED_MCP_NAMES`, and the hidden `cli.py` subcommand.
`test_computer_use_registration.py` asserts set equality where it can:
`agent._MANAGED_MCP_SERVERS`, `mcp_discovery._MANAGED_SERVER_SUBCOMMANDS` and
`mcp_cleanup.KIROCREW_BIN_MCP_SERVERS` must be the same set. The other registries
are checked per name, so the next server is caught there only once a test names it.
The onboarding import's managed names are an intentional superset: they include the
predecessor brand's `openclaw-*` names, so an imported config's foreign copy is not
kept as a user-installed server.

### The one deliberate exception

`kirocrew computer call <tool>` has **no MCP twin, on purpose.** It is not a
capability; it is a human debug and repro harness that runs the eleven existing
`computer_*` tools through the same gated chokepoint (optionally a JSON array of
them in one process, so `element_index` values stay resolvable across calls). The
MCP-first rule exists so the model gets a structured tool instead of shelling
out, and the model already has all eleven. A tool that runs other tools would let a
model launder one per-call gate decision into many, so do NOT add
`computer_call`.

## MCP tools MUST be stateless

The direct `kirocrew-cron` server sends member jobs through
`POST /api/crons/tools` using ordinary internal transport and strict session
identity. The gateway captures the canonical execution record and applies the
same argument validation, cron ownership, governance and deterministic-job
checks as its regular dispatcher. A request-local `CallerContext` is restored
even on failure. Transport failure never triggers a local file-write fallback
or blind mutation retry; callers inspect `cron_list` after an uncertain response.
The ONE exception is not a transport failure: a dial that was **refused**
(`_post` returns `refused=True` — no gateway is listening, so nothing was
executed and nothing can be replayed) from a process with NO gateway-injected
caller whose resolved session key is POSITIVELY the attended CLI's own
(`validation.infer_use_case(key) == "cli"`, the `cli_chat` key `kirocrew chat`
presents everywhere it is identified) dispatches to the direct host store, which
is the behaviour that runtime always had before the transport was required. The
absence of an injected caller alone is not enough: the non-pooled stdio gateway
topology also has none, and a gateway-minted key (`dashboard:`, a channel, a
cron, a subagent) with a refused dial is the validating gateway being down — an
outage to report, never a licence to write around it. A gateway-injected caller
likewise proves a gateway exists, so its refused dial is reported as an outage.
Global V1 direct runtimes retain their existing local cron dispatch.

**A new `kirocrew-core` or `kirocrew-cron` tool MUST NOT keep per-caller or
per-session state in the MCP-server process. Resolve the caller's identity on
every call and keep authoritative state in the gateway.**

### Why: the shared-backend invariant

The managed servers are long-lived stdio subprocesses, and **one server process
serves many sessions.** In the pooled topology a single warm backend is reused
across sessions, and a sub-agent spawned via `spawn_run` runs inside the parent
slot's process tree and talks to the same MCP server. Anything the process
remembers is therefore shared by every session and sub-agent that touches it.
Three failure modes follow.

**1. Identity is not the process, it is the call.** `KIROCREW_SESSION_KEY` and
`os.getppid()` identify the *process*, which is wrong by construction in a shared
backend: the warm pool spawns with an empty key, and a sub-agent inherits its
parent's tree. `mcp_core.py` offers two resolvers:

- `_resolve_session_key_strict` for **anything that mutates or targets a
  specific session** (post to a slot, change its state, deliver a callback) —
  reached ONLY through `require_strict_session_key()`, the shared fail-closed
  gate every reflexive tool module routes through (`REFLEXIVE_TOOL_MODULES`
  enumerates them; a ratchet test rejects direct calls outside `mcp_core`). It
  accepts only the gateway-injected caller context (`mcp_caller.current_caller()`,
  which gatewayd stamps on every forwarded frame after stripping any
  client-forged `kirocrew.caller` block), the signed per-session token
  (`session_token_sig.session_key_from_env_token`), the injected
  `KIROCREW_SESSION_KEY`, or
  a `KIROCREW_HOST_PID` lookup whose HMAC sidecar verifies against the
  keystone-protected `sel_hmac.key`. It deliberately **drops** the `/proc`
  ancestor walk and the bare `session_pid_<pid>.txt` fallback: the `.txt` file is
  agent-writable and therefore forgeable, and a sub-agent walking ancestors from
  its own MCP child resolves to its **parent** slot, which would let it mutate
  the wrong conversation.
- `_resolve_session_key()` (lenient, still walks ancestors) is only for read-only
  and telemetry callers where misattribution is harmless. `skill_search` is
  read-only but NOT harmless to misattribute: its gateway route returns the
  session project's confined skill bodies, so it resolves through the strict
  gate and degrades to the global-only search when no signed identity exists.

**What names a session on the stub path: the stub session token.** The injected
caller context above is the only identity channel a pooled backend has, and every
input gatewayd had for building it answered per RUNTIME, not per session: the
stub's own self-report (its `KIROCREW_SESSION_KEY` or `session_pid_<pid>.txt`
walk), gatewayd's own SO_PEERCRED `/proc` walk, and claim-push, which re-targeted
every connection indexed under a runtime PID. One kiro-cli process hosts N ACP
sessions, so all three answered with the parent slot for a `spawn_run` subagent's
stub, and a parent re-claim overwrote whatever a subagent had. So Kiro Crew mints
one unguessable token per ACP session (`claim.mint_stub_session_token`), puts it
in the `env` of that session's injected stub entries
(`session_servers.attach_stub_session_token`), and remembers it on the session
handle. The stub returns it on its `register` frame as a sibling field — never a
`PoolKey` dimension, which would make every session's backend private and turn
pooling into a no-op — and `claim` frames carry it too. gatewayd then keys claims
by `(pid, token)`: a claim re-targets the connections carrying its token, plus any
connection carrying none, and a claim with NO token re-targets every connection
under the PID, which is what a stub launched from a
hand-written config or an older overlay still needs.

**The token narrows within a runtime; the runtime bounds who may present the
token.** A claim binds the token together with the PID it named, and a register is
answered from that binding only when the same PID is in the chain gatewayd walks
from the **SO_PEERCRED peer pid** — never the stub's self-reported
`ancestor_pids`. That distinction is the whole value of the second factor: the
register frame is peer-supplied in full, so an actor who has read another
session's token out of `/proc/<pid>/environ` (readable at the operator's own uid)
can equally name that session's runtime in its own chain, and one actor would then
satisfy both halves. The peer pid comes from the kernel and the walk is gatewayd's
own, so the registrant cannot author it. The self-reported pids stay in the claim
INDEX, where they are harmless: a claim only ever narrows to connections carrying
its own token or none. A connection whose ancestry the kernel did not attest loses
the register-time shortcut, not its identity — claim-push still reaches it through
the index. (What a same-uid process can assert on the register frame itself is a
separate question — a tokenless register's self-reported `session_key` is believed.)

Two details keep that binding sound (`mcp_gateway/daemon/identity.py`). The register
path asks for the token's binding exactly once, after its last await and immediately
before it indexes the connection, so a claim cannot bind between the answer and the
index that lets the claim land. And a binding records the start token of the runtime
PID its claim named; register snapshots the start token of every PID it indexes, and
resolves the binding only when the two do not definitely differ. A mismatch means
the OS recycled the PID for another process. A missing start token never denies.

**A token nothing has claimed is refused, not resolved.** A binding outranks both
process-tree sources at register time, and where no claim has named the token yet
the connection stays identity-less — the stub's own self-report and the peer walk
are not consulted, and neither is the stub-initiated `recaller` frame, whose key
comes from the same walk. The refusal cannot be made conditional on some sibling
session happening to be named: an empty binding table is the state a fresh daemon,
a respawn and an evicted binding all share, and in each of those the tree answer
would hand a subagent its parent's session. What repairs a deferred identity is
the owning session's own claim — pushed before `session/new` where the owner is
known, at `rekey()` on a pooled claim, and after `new_conversation` re-launches a
worker's stubs.

**And re-pushed at the start of every turn**, which is what re-binds a token whose
daemon restarted under it and bounds that outage to the turn it happened in. Two
places do it, because no single one sees every session: the shared identity
publisher (`messaging/identity.py`, the same boundary that rewrites
`session_pid_<pid>.txt`) covers each surface that drives a user turn, and
`AcpSessionProvider.stream` covers the sessions no surface publishes for — which
is where a subagent's turns live, the session type the token exists to protect.

A claim carries a session key or gatewayd discards it, so every provider is told
which session it serves when it is CONSTRUCTED rather than only at `rekey()`: that
is a warm-pool event, and a cold start (pool miss, pooling off, a subagent's own
session) reaches it never. A provider that learned its key there would re-claim
with an empty one, which is rejected as malformed before the binding is recorded —
so the token could never be re-bound and the session would stay identity-less for
the rest of its life rather than for one turn.

The token is a bearer name for a session's identity, so it is never logged, never
in `stats()`, and stripped from the register payload before the prewarm recorder
can persist it.

**The one place the token is forwarded: Kiro Crew's own pooled control planes.**
gatewayd spawns a pooled backend from its OWN environment, so the per-session
token the stub carries never reaches the backend's `os.environ`. That is right
for a third-party server, which has no business proving a session to anyone.
`kirocrew-core`, `kirocrew-cron` and the opt-in Crew servers (`kirocrew-dashboard`,
`kirocrew-work`, `kirocrew-crew-log`, `kirocrew-debug`, `kirocrew-panel`) are
different: they
post back to the gateway over loopback (`/api/crons/tools`, the memory routes,
the session and folder routes) on behalf of the session they act for, and every
one of them reads the session's tool policy through `mcp_shared`, a read the
gateway answers only behind the attestation; the gateway requires `X-Session-Token` on that
transport when no kernel peer attestation is present — which a gatewayd child
never has. So for exactly `gatewayd.CONTROL_PLANE_BACKENDS` -- every managed
Crew server, read from `mcp_cleanup.KIROCREW_BIN_MCP_SERVERS` -- the connection
handler copies `conn.stub_session_token` onto the injected `CallerContext`
(`session_token`), `build_caller_meta` emits it as `sessionToken` only when set,
and `mcp_core._session_token_header` reads the gateway-injected caller's token
before falling back to `KIROCREW_STUB_SESSION_TOKEN`. The name alone does not
earn the token: the server name arrives in the stub's register frame while the
spawn target resolves separately from the spec-derived
`KIROCREW_MCP_TARGET_<NAME>` mapping. That mapping is not the spec's word for a
reserved name, though. The rewriter re-derives every stubbed `kirocrew-*` entry,
whether an agent spec declares it or `settings/mcp.json` injects it, from
`agent.managed_mcp_spec_entry` before it resolves, hashes or bakes anything
(`rewriter._repair_control_plane_entry`), and holds the entry's
declared `env` to the managed-entry ownership rule the disk writer
(`agent._enforce_managed_mcp_ownership`) and the ACP element
(`session_mcp._managed_element_env`) apply -- reserved `KIROCREW_*` keys, loader
channels, home-deriving and launcher-exec keys dropped. Of the spec's other keys the
repaired entry keeps only `type`, `timeout`, `disabled` and `disabledTools`
(`rewriter._RESERVED_ENTRY_SPEC_KEYS`), which can only narrow the grant. `autoApprove`
is dropped, because kiro-cli would honour it ahead of the PreToolUse gate, and so is
every other declared key; each drop is named in a gateway WARNING. So a spec that spells the
launcher the only way a hand can (`"command": "kirocrew"`, which resolves to the
shared Toolbox dispatcher, not the versioned binary) or pins a path an upgrade
has since reaped still runs our binary, and a third-party command declared under
a reserved name runs our binary too, never its own. The gate below is unchanged;
the repair is what makes an honest spec pass it. At spawn, `gatewayd._spawns_own_control_plane`
compares the command actually exec'd (by real path) and its args against the
invocation `agent.managed_mcp_spec_entry` emits for that name and records the
verdict as `Backend.control_plane`; the handler forwards the token on that flag
only. The invocation being ours is still not proof of what runs: the managed
spec falls back to `<python> -m kiro_crew <sub>` when no launcher resolves, and
the child's CWD could carry foreign code under our name. Python's `PYTHON*`
environment namespace is an extensible interpreter control surface: entries can add roots, execute hooks, select an executable, or move
user-site without changing the command, and the dynamic-loader `LD_*` / `DYLD_*`
namespaces replace code in every binary of the spawn chain. For a control-plane
backend, the
gateway's pooled-backend resolver removes all three namespaces
(`env._SPEC_ENV_DENIED_PREFIXES`) from the operator
environment. Any non-empty entry in them present at the verdict therefore came
from a hand-declared overlay or another resolver and denies the token without
value inspection. The prefix rule fails closed when Python adds a variable; it
cannot drift into a false grant through an incomplete list. Third-party pooled
backends never receive the token and keep operator Python settings outside the
four keys in `sandbox._PYTHON_ENV_PREFIXES`; `PYTHONUNBUFFERED` is one such
setting. With those environment controls excluded from a control plane, two roots
remain. The first is the fixed local root: the CWD for module form or the
launcher's directory for script form. The second is per-user site-packages,
which needs no variable to take effect — the interpreter adds it from a default
location ahead of the install's own site-packages, so removing `PYTHONUSERBASE`
relocates it rather than disabling it, and `PYTHONSAFEPATH` does not cover it.
It is read from the gateway's own process, which is sound because the spawn is
already pinned by realpath to the managed spec's launcher, so the child runs this
install's interpreter. Inspecting that directory cannot cover everything it does:
its `.pth` entries execute code at interpreter startup under any filename. So when
nothing there is load-bearing -- user-site does not hold the package this process
is running -- an accepted control plane is launched with `PYTHONNOUSERSITE=1`,
removing the surface rather than inspecting it. A `--user` install is the case that
keeps user-site enabled, because its own package lives there; an unresolvable
user-site is treated the same way, leaving the child's import behaviour unchanged
rather than guessing. The verdict is false when either root holds an importable
`kiro_crew` other than this process's package. A `--user` install therefore keeps
its token: its user-site holds the very package this process is running, which
matches. Disabling user-site instead would break that install shape outright. A namespace directory
without `__init__.py` does not count, and a dev gateway whose project CWD is its
own `src/` remains valid. Interpreter flags such as `-P`, `-E`, and `-I` never
relax this bearer-token fence: an inert root may cause a safe false-deny, but a
Python version change cannot create a false-grant.

The verdict is taken BEFORE `spawn_backend` forks, under `asyncio.to_thread`
(it imports `kiro_crew.agent`, reads config and stats those roots), so foreign
code cannot erase its shadow before a post-spawn check. An accepted backend is
still launched with `PYTHONSAFEPATH=1` as defense in depth. After the verdict,
and for every pooled backend, the spawn site re-applies Kiro Crew's own UTF-8
pinning (`platform_compat._UTF8_PROCESS_ENV`: `PYTHONUTF8` and
`PYTHONIOENCODING`) to the child environment, so a pooled interpreter builds
its stdio from UTF-8 rather than a Windows ANSI codepage. The order is the
guarantee: the classifier sees an environment free of every denied namespace, and the pinning
lands on a child whose verdict is already fixed -- the same pair present before
the verdict would deny every control plane its own token.
`Backend.control_plane` is set from that pre-spawn verdict and never recomputed.
The token joins a caller in one place, `daemon/control_plane.py::_caller_for_backend`, which
reads the flag off the backend that receives THAT frame and returns a
token-bearing copy; the connection's base `CallerContext` stays tokenless for
its whole life. A transparent respawn is therefore judged on its own: the
handler hands the tokenless base to `_respawn_backend_for_stub`, whose
tool-surface probe and subscription replay decide against the replacement, and
the frames the session forwards afterwards decide against whatever backend now
serves it — so a control plane that died is not a warrant for the fresh process
spawned under its name. A denial for a reserved name is logged at spawn
(`_deny_control_plane`) naming the backend and the condition that failed — no
spec entry, a different binary, different args, a non-empty `PYTHON*` / `LD_*` /
`DYLD_*` variable,
or a root that shadows `kiro_crew` (named in the message, so per-user site-packages
is distinguishable from the local one) — so an
install that trips the check has more to read than every cron tool answering
403. The same reason rides to the denied backend itself: the spawn site records it
as `Backend.control_plane_denial`, `_caller_for_backend` copies it onto the frames
it forwards to that backend as `CallerContext.identity_denial` (`identityDenial`
in the caller block, emitted only when set), and the backend's
`identity_unattested` refusal quotes it. Without that channel the only record was
`logs/mcp-gatewayd.stdout`, which no session surfaces, and the refusal's generic
text pointed at the token and the spec when the cause was neither.

`CONTROL_PLANE_BACKENDS` is named in the daemon itself, in `mcp_gateway/daemon/control_plane.py` (importing
`acp.session_mcp` would put `kiro_crew.agent` on the daemon's boot path; the
set is read from the `mcp_cleanup` leaf instead). It is
a superset of `acp.session_mcp.CONTROL_PLANE_SERVERS`, not a mirror, because the
two answer different questions: `CONTROL_PLANE_SERVERS` decides which servers
every session mounts and which survive a `disabledTools` entry;
`CONTROL_PLANE_BACKENDS` decides who is handed the token. Containment holds in
one direction: a server mounted in every session posts back for that session,
so it needs the token. The opt-in Crew servers are the reverse case --
`kirocrew-dashboard` posts back for the CALLING session (`session_create`,
`session_send`, the folder and tag tools), so it needs the token, but it is
`opt_in`, so naming it in
`CONTROL_PLANE_SERVERS` would mount it in every session and make an operator's
decision to switch its tools off unenforceable. A ratchet test pins that
relationship rather than equality with `CONTROL_PLANE_SERVERS`: it asserts
`CONTROL_PLANE_SERVERS` is
contained in `CONTROL_PLANE_BACKENDS`, pins the token-only extras to exactly
`mcp_cleanup.OPT_IN_BIN_MCP_SERVERS` plus the spec-gated `kirocrew-computer`,
requires every extra to be a managed server that is not unconditionally mounted
(`opt_in`, or behind a `spec_gate`), and pins the whole set equal to
`acp.session_mcp.IDENTITY_BOUND_SERVERS` -- the kiro-backend element list that
carries the same token per element -- so the two identity paths grant the same
servers and a new recipient has to update both in the same commit. Because the
opt-in servers are `opt_in`, `_spawns_own_control_plane` asks
`agent.managed_mcp_spec_entry` for the invocation with `include_opt_in=True`,
which skips only the `opt_in` emission disqualifier; a closed `spec_gate` still
yields no invocation, and therefore no token. The stub-strip in
`backend._strip_caller_meta` removes the whole caller block, so a stub cannot
forge a `sessionToken` either. A gateway-injected caller also marks the backend
as gateway-hosted: when its dial to the gateway is refused it reports the
outage, whereas the attended CLI's own identity (`kirocrew chat` with no gateway
— no injected caller AND a session key `infer_use_case` classes as `cli` — whose
`_post` returns `refused=True` meaning nothing was executed) falls back to the
direct host store the cron tools always had before the transport was required;
a gateway-minted key with no injected caller is the non-pooled topology and
reports the outage instead.

`register_hook` resolves through `require_strict_session_key`. Member hooks
capture their originating execution record before provider allocation; Global
hooks retain their existing no-conversation behavior. Hook context and display
labels do not retarget that record. Direct and pooled member MCP use the same
ordinary transport contract.

Gatewayd validates the accepted stub's ordinary peer ownership and session
claim, strips client-supplied caller metadata, and injects the admitted caller for
each invocation. Shared backends use a request-local `CallerContext`, including
concurrent tool calls and listings; they never keep a mutable current member on
the process or connection. The gateway resolves that session's canonical
execution record once for each memory request and forwards the frozen binding to
background work.

Member memory requires no additional PID-ancestry proof, HMAC capability or
`X-Member-Session-Proof` header. Direct and pooled MCP follow the same routing
contract. Ordinary transport authentication, broker claim validation and signed
PID-sidecar identity remain intact. An execution record that declares a member
but cannot be resolved fails explicitly instead of becoming a Global V1 call.

An unresolved key is not automatically a refusal. `mcp_computer.py` forwards a
namespace-only key (`unresolved:<shim pid>`, plus the gateway's per-connection
nonce when there is one) and lets the call proceed, because neither strict source
exists for a
GUI-launched kiro-cli on macOS, so gating on identity would make the feature
unusable on its only supported platform. What is lost there is audit
*attribution*, not a control: the trail records a key the prefix marks as
unresolved, which is honest,
where the lenient walk would have recorded a forgeable one.

**2. State belongs in the gateway.** The tool should be a thin forwarder: resolve
the session, then `POST` to a gateway HTTP endpoint that owns the state (usually
in `DashboardState`), addressed by session key plus a per-request id, blocking on
that round-trip if it needs a result.

**3. One stray line costs that line, never the reader.** A shared process makes
its stdio readers shared too, so a line that is not valid UTF-8, not JSON, not a
JSON object, or nested past the decoder's ceiling is dropped and the next line is
read. The line-oriented JSON-RPC readers on either side of a stdio MCP server
(the stdio loop, the auto-improvement app's server, the gateway's stdout pump and
pre-init reader, the stub's and gatewayd's frame readers, the app-call
round trip, a script cron's `McpToolClient`) parse through
`kiro_crew.json_line.parse_json_object_line`, which catches `ValueError` (that
covers an integer literal past the int-string digit limit too) and
`RecursionError`, the case a `JSONDecodeError` arm misses, and returns `None` for
anything that is not an object. `None` there never means end of stream. The
discovery probe's stdio reader is the one deliberate difference: it counts only
lines that are not JSON toward its banner cap, so a server printing a JSON
progress counter before it answers is bounded by the probe's timeout like a
notification, not mistaken for a flood. The stub's admission wait skips a frame
that is JSON but not an object; text that is not JSON, or `null`, still ends it
as closed (the daemon is not speaking the protocol), and the handshake falls
back on either.

A dropped line may still be a request someone waits on. Every reader that serves
requests (the stdio loop, the auto-improvement app's server, gatewayd's
connection reader) answers a dropped REQUEST, one whose head shows a top-level
`method`, that carries a recoverable top-level id with a `-32700` parse error
under that id, instead of leaving its caller to its own timeout while pings keep
the connection looking healthy. A dropped response is never answered: gatewayd's
reader also carries kiro-cli's answers to a backend's own requests, under the
backend's ids, and an error sent under one would answer whichever kiro-cli
request shares the number. The id comes from
`json_line.recover_line_id(line, requests_only=True)`, which reads only the
first and last 512 bytes (`ID_PROBE_BYTES`) for the TOP-LEVEL object's `id`,
never one nested in `params` or `result`: a writer puts the id first or (the
MCP TypeScript SDK) last, and a bounded probe costs the same on a multi-MiB line
as on a short one. A number the probe's edge cuts in half is not read as an id,
and a line that fits in the probe is read by the head scan alone. An id in the
middle of a long line, or a `method` past the head, is not found, and that line
is just dropped.

On the server side `mcp_shared._read_message` returns `SKIP` for each dropped
frame, which is distinct from `None` (EOF), so a `null` line cannot end the
server, and returned per frame, so a busy loop goes back to delivering a
finished tool's result instead of blocking on the next line. Before the busy
loop polls stdin it reads any complete line the buffered reader already holds:
`select` sees only the descriptor, so a ping or a cancel that arrived in the
same read as the line before it would otherwise wait for the client's next
write. On a stream already read as bare JSON a `Content-Length:` line is skipped
as noise. A declared body over
`MAX_CONTENT_LENGTH_BYTES` (the gateway's default read limit) is drained in
bounded reads and answered `-32600` under the id its top-level request object
carries, found at either end. A header whose length cannot be read (a word, a
negative number) ends the stream, logged at ERROR: a client that writes a header
writes its body after it, and with no length that body cannot be delimited, so
every later frame would be read joined to it and answered as unparseable. A
declared length past any drainable size ends the stream the same way, because
draining to it would swallow every later request.

Every message is validated once, before the busy/idle split, so it is served
or refused the same way under load as at rest. `validate_jsonrpc_request`
reports `params` that are present and not an object (absent or `null` read as
`{}`) as a `JsonRpcEnvelopeError` carrying the request's id: such a request is
answered `-32602`, and a malformed envelope (a non-string `method`, a
`jsonrpc` other than `2.0`) `-32600`; a notification is never answered. A
`tools/call` without an object `params` and a non-empty string `name` is
answered `-32602` and audited as a refused call (SEL `tool_call.invalid_params`,
outcome `rejected`, recording the type it got in place of a name), never
dispatched and never audited as an invocation of a tool. Any other exception
while dispatching one message is logged, an id-bearing request is answered
`-32603`, and the loop goes on; a worker thread that cannot start (the scope's
task ceiling) costs that call the same way and leaves nothing behind for the
next pass to join.

On the gateway side the start path is the stdout pump: `Backend.run_stdout_pump`
isolates each line's handling, so a raise anywhere in it (budgeting an image,
spilling, routing, failing a request) costs that line, logged at WARNING; a
raise that ended the pump would fail every co-pooled session with backend-gone
in its `finally`. A line that does not parse is dropped at `DEBUG`, unless it
opens like an object and `json_line.recover_line_id` finds the id of a pending
request at either end of it, which is then failed (WARNING) rather than left to
hang. The probe is the bounded one above and runs inline: bounded, it cannot
hold the shared pump, and through the GIL the whole event loop, on a multi-MiB
line. Only the top-level `id` counts, so a server logging the call
it serves cannot fail that call; an oversize line keeps its first and last 512
bytes for the same probe. A non-string `method` is dropped. When routing a response raises
after it took its pending request, that request still gets an error. Every
failed request is settled by `Backend._settle`, the same code a real response
runs, with a synthetic error: queued `initialize` waiters are answered through
`_fail_init`, an MCP Apps fetch is resolved at once, lease riders and release
waiters are answered and the lease bookkeeping cleared, and the forwarding stub
gets the error. That error is the gateway's, not the server's verdict, so a
`resources/subscribe` settled this way is an unknown verdict, as a malformed
reply is: with nobody routed, the lease is released upstream (as the caller that
took it on an identity-capable server) instead of being read as a refusal and
left live. A `capabilities` that is present but not an object is cosmetic,
so the handshake reads it as `{}` (logged) instead of failing the shared backend
over it, on both the lazy path and `send_initialize`. `prime_initialize` refuses
a backend that is no longer alive even when its handshake state reads ready.

### Reference implementations

Two shapes are both correct; pick by whether the tool needs a value back inside
the same turn.

**`POST` to a gateway endpoint that holds the pending future.** The
`/api/ask-question` handler (`dashboard/handlers/ask_question.py`) is the model:
the pending question lives in `DashboardState._pending_questions` /
`_question_futures`, keyed by `ask_id`, and is addressed to one slot resolved from
the posted `session_key`. The handler refuses an unknown slot with 404 rather
than blocking for the full window on a card nobody will render, and the answer is
routed back by `ask_id` from `POST /api/ask-question/{ask_id}/answer`. A stateful
version, parking the pending question in a module global and trusting env-var
identity, would hand the answer to whichever session the shared process last saw
and let a sub-agent's card land in its parent's slot.

**Return a session directive and let the session-aware consumer apply it.** This
is what the `ask_question` MCP tool itself does, along with `nothing_to_do`,
`monitor_start`, `monitor_watch`, `monitor_update`, `monitor_stop`,
`autonudge_stop`, `set_project` and `suggest_followup`, `reset_conversation` and
`chat_tag` (`session_directive.DIRECTIVE_TOOLS`). The tool validates its arguments and
returns a human-readable confirmation plus a marker line carrying the validated
payload and **no session key**. `dashboard/chat_runner`'s tool-result handler
decodes the marker, applies the effect against **its own** `slot.key`, then
strips the marker from the stored transcript. Sub-agent isolation is therefore
structural rather than cryptographic: a sub-agent's tool result flows through the
sub-agent's own runner, so it can only bind to the sub-agent's session. There is
no walk to get wrong.

**Terminal directives carry a structured turn-end signal.** Two directives are
the turn's intended LAST act — a shown `ask_question` card and a recorded
`nothing_to_do` quiet end (`session_directive_apply.TERMINAL_DIRECTIVES`). Their
applier returns a `DirectiveOutcome(text, ends_turn=True)` through
`apply_session_directive_outcome`, and only on the path where the effect landed
(a card a client will render; a quiet step recorded): a refusal, an error text or
a dropped card leaves `ends_turn` False. Both consumers read that flag — the
dashboard runner skips its empty-response ladder, the channel driver owes no
empty-turn notice — and neither derives it from the outcome prose (the
`QUESTION_CARD_SHOWN_PREFIX` match the first fix used was the thing #9324 asked
to replace). The string-returning `apply_session_directive` remains for callers
that need the text alone. `nothing_to_do` exists for the turn-end contract the
base prompt states: after its tool calls a turn ends with a closing text or with
`nothing_to_do`, never by stopping bare after an ordinary tool — a bare stop
keeps the recovery ladder, which is what makes the directive the only sanctioned
silent exit. Its ONE gate is who opened the turn: it applies on a monitor wake
(`producer_is_self_wake`) or a headless producer (cron, crew runtime, app or
task-runner injection), and is REFUSED (`QUIET_END_REFUSED_USER_TURN`,
`ends_turn` False) on a turn a person opened — a user-facing dashboard turn or
any channel turn, since the channel driver cannot yet tell a human's message
from a loop's wake. The refusal is what keeps a model misfire a visible failure:
the ladder, the Resume control and the channel notice run exactly as before. No
slot or surface gate otherwise (it mutates nothing a wrong identity could
misdirect); isolation from a sub-agent is the same structural isolation every
directive has. The runner stamps `meta.ends_turn = True` on the applied
directive's tool row (persisted, and patched live over `chat_message_update`),
which is the structured fact the interrupted-turn scan reads.

The directive marker is model-visible, since it comes back as tool-result text,
so the consumer defends against forgery by honoring a directive only when the
tool call it arrived under was recorded, from kiro-cli's out-of-band `_meta`
channel, as an MCP-served call whose canonical name (`_meta.kiro.toolName`, with
`_meta.kiro.mcpServerName` equal to `kirocrew-core`) is in `DIRECTIVE_TOOLS`. The
LLM-authored `title` is explicitly not accepted, because a shell command titled
`monitor_start` whose stdout forges the marker must not be honored. The gate fails
closed when `_meta` identity is absent, and refuses native-sub-agent tool calls,
which surface as flat events in the parent's loop but have no independently
bindable slot. The marker is ASCII-only: an earlier invisible-separator prefix was
destroyed by `validation.build_tool_response`, which strips Unicode category `Cf`
from every tool response, so every directive silently failed. A machine-facing
framing token must not depend on characters that sanitizers and normalizers
legitimately rewrite. `encode()` refuses above `MAX_DIRECTIVE_CHARS` (3800), under
the ACP tool-result truncation bound, so an oversized payload fails loudly
instead of losing its trailing marker.

A tail-anchored marker must survive delivery, and a rejection must not be able to
carry one. `validate_tool_args` reports an unknown field by echoing the argument
NAME, which the model chooses, so that name is the injection point for both
problems. A name carrying the sentinel plus a JSON payload plus a newline would make
the REJECTION string decode as a genuine directive under the real tool's
authenticated identity, applying the arguments validation had just refused; the
vouch gate below is what stops that. A very long name would make the transport cut
remove the result's refusal tag, so the decline would read as a lost marker:
`tag_refusal` elides the middle of an over-long text against
`MAX_TOOL_RESULT_CHARS`, the single constant `acp/_dispatch.py` slices on.
That bound alone is necessary but not sufficient, because the cut runs AFTER
redaction and redaction GROWS text (a credential becomes a longer placeholder,
measured 7,999 chars in and 8,755 out), so `preserve_tail_marker` re-attaches a
marker the cut removed — the same re-injection the MCP App render marker already
gets at that seam, for the same reason: a control token that decides how a frame is
interpreted must not be a casualty of a length cut applied to the frame's prose.

A marker is honoured at that gate ONLY if the emitter vouched for it on the same
dispatch: `_emit_directive` is the one producer of a real marker, so it records a
digest of what it built, `_call_tool` clears that record before every dispatch, and
`refuse_if_markerless` defangs any marker nobody vouched for. "Does this look like a
directive?" is not a safe question — a rejection echoing a model-chosen argument
name can imitate one, and a rejected call would then apply the very arguments
validation had refused. Every place `mcp_shared` builds an `"Error: …"` result also
passes the interpolated text through `session_directive.neutralize_markers`, kept as
defense in depth because those strings reach the audit row and the four other
servers' outputs, where no vouch gate runs. That defanging is applied only where the
caller KNOWS the string is not a directive: doing it centrally over every tool
result would defang the real marker too.

`_emit_directive` classifies its OWN output the same way — by the marker's presence,
not by content. Testing `is_refusal(out)` there would match any payload that merely
CONTAINED the refusal token, so a stop whose reason quoted that token would be filed
as a refusal, skip both the publish and the vouch, and have its genuine marker
defanged downstream: the stop would be lost. A gate against imitable content cannot itself be
built on imitable content.

### An `Error:` prose result can also be framed as an MCP error

`build_tool_response` is the single exit point for every tool result. The
`"Error: …"` prefix is what `mcp_shared` derives the SEL audit `outcome` from
(`failed` when the text starts with the prefix, `completed` otherwise), but a
prefix alone says nothing in the wire frame, and a client would have to
pattern-match prose to tell a refusal from an answer.
`cron_script.McpToolClient.call_tool` raises `RuntimeError` on
`result["isError"]`, so a refused cron write must carry the flag or it reads back
as a completed one.

`build_tool_response` takes a keyword-only `is_error`, which adds MCP's
`"isError": True` to the frame. The prose is untouched: the flag is computed from
the RAW result text before sanitization, so a refusal is byte-identical whether it
is flagged or not, and the audit `outcome` derivation does not move.

**The flag is per-server opt-in, not a global.** `run_mcp_stdio_loop` takes
`error_prefix_is_error`, default off, and every `tools/call` result on that loop —
the POSIX worker path, the Windows synchronous path, and the two tool-policy
refusals (unresolved policy, excluded tool) — goes through one nested
`_tool_response` helper so a server cannot be half-converted. `kirocrew-cron` is
the only server that sets it. The other servers keep their frames unflagged
deliberately: each has in-tree callers and model-facing prompts that read
`"Error: …"` prose on purpose, so flipping them together would change what those
callers see.

A server that opts in owes its failure answers the prefix, and the audit half
already held it to that: an answer without the prefix is filed `completed`. The
sharp edge is a REFUSED durable write, because its answer sits one line below the
committed one: a cron mutation whose store call comes back falsey — the row the
ownership gate just saw is gone, a concurrent delete between check and write —
frames beside `"Removed job: <id>"`, so an unprefixed answer there reads as a
completed delete. All four such returns (`cron_update`, `cron_remove`,
`cron_pause`, `cron_resume`) are `"Error: job not found: <id>"`, which is what
`AUTOSDE.yaml`'s `a-refusal-is-not-a-commit` requires. Naming the row there leaks nothing: the gate
has already proved this caller owns it, which is why `_not_found`'s
anti-enumeration wording is deliberately NOT reused post-gate.

Cron's `"Unknown tool: …"` fall-through is the apparent second exception and is not
one — `_call_tool` rejects an unknown name at its argument validation, ahead of
that return, with prose that does carry the prefix, so the unreachable string needs
no marker.

**A directive tool's result either carries the marker, or it is a tagged
refusal — nothing in between.** The consumer cannot otherwise tell a decline from
a marker destroyed in transport: both decode to "no directive", but only the
second is a bug, and the diagnostic for the second is a WARNING
(`session-directive decode FAILED … effect dropped`) whose whole purpose is to
catch a rawOutput-envelope escaping regression. So every marker-less return is
stamped with `_REFUSAL_SENTINEL` and reported at INFO as
`session-directive REFUSED`: `encode()` stamps its own oversized-payload refusal,
and `mcp_core._call_tool` stamps the rest via `refuse_if_markerless()`. That
second producer sits at the OUTERMOST return because argument validation runs in
the dispatch wrapper *ahead of* the handler, so a schema rejection never reaches
code inside the tool that could tag itself. Tagging is diagnostic only and keys on
the tool name alone: the token carries no payload and grants no effect, so it can
change how a line is logged and never what is applied. Two consequences for a tool
author — a directive tool must RETURN its declines rather than raise them (an
exception escapes this return path and reads as a lost marker), and a caller that
sees `decode FAILED` is looking at a transport bug or at a handler that crashed --
the line names both, because a crash also drops the effect and asserting only the
transport cause sends an operator hunting a regression that is not there.
That first rule is enforced rather than left to convention: a parametrized test
drives every name in `DIRECTIVE_TOOLS` with a hostile call and asserts the result
is a marker or a tagged refusal, and its companion asserts the table covers the
frozenset, so a new directive tool fails until it is added. The one raising
dependency these handlers share, `monitoring.targets.normalize_pull_request_target`,
is reached only through one guarded seam (`_parsed_pull_request_target`) for the
same reason: one guard cannot be missed at a second call site.

`FieldSpec.clamp_to_max` is the other half of that: an over-long argument must not
defeat the request it is only describing. `autonudge_stop` and
`monitor_stop` both take a `reason` that selects no behaviour — the applier just
interpolates it into the outcome text and the persisted stop record — so their
`reason` is TRUNCATED to the cap instead of rejecting the stop, and the truncated
value carries a `[... truncated, dropped N chars]` note so the cut is visible
wherever the value travels. `N` counts what THAT cut dropped, including the note's
own cost — deliberately not the caller's original length, which the clamp cannot
know because the field is sanitized before the cap is checked, so one number
cannot honestly stand for both removals. Opt-in per field, and never for a field
the handler acts on: a truncated control input is a wrong control input, and
rejecting is the only safe answer there.

Structured monitoring deliberately splits mutation from inspection.
`monitor_watch`, `monitor_update`, and `monitor_stop` are directives: the
consumer applies them to its authoritative session, so their payloads contain
neither a session key nor a loop id. `monitor_inspect` needs a result in the same
turn and therefore calls the session-bound read route only after
`require_strict_session_key()` resolves, passing that exact key to `_get`.
It projects that response into bounded agent-oriented state (check counts and
accepted wake count, plus only a small failed/pending/unknown name sample),
omitting wake instructions and browser/persistence internals. Inspection reports
unavailable when strict identity is absent; it never falls back to the
process-ancestor resolver. Every structured-monitor tool refusal caused by a
missing or unsupported session binding returns an `Error:` result and writes an
explicit `denied` SEL event; the shared MCP wrapper therefore records the call as
failed rather than completed. The structured tool exposes no caller-declared
evidence scope: requests that need comments or advisory findings route directly
to the finite legacy tool whose agent turn can inspect them.
Structured creation is admitted only from dashboard, Slack, and Discord sessions,
the surfaces with typed wake dispatch and completion correlation. Webex retains
finite legacy prompt loops but refuses `monitor_watch` at both the stateless tool
and authoritative consumer boundaries.
The legacy `monitor_start` descriptor routes supported pull-request readiness
through `monitor_watch`, and which of the two it names as the DEFAULT is the
installation's choice: with `monitoring.prefer_structured_arming` off (the
shipped position) the structured path is offered only when typed provider facts
fully determine the objective, and with it on the structured path is the default
and the legacy loop is the exception. Neither position refuses either tool.
Objectives that require interpreting comments or advisory review evidence keep a
finite legacy loop in both positions instead of claiming the structured probe
observes those facts.

`monitor_watch.kind` is the closed set `github_pull_request`,
`gitlab_merge_request`, `azure_devops_pull_request`, and
`bitbucket_pull_request`, all with the `review_ready` objective. Its target is a
canonical provider URL and is parsed against that kind before a directive is
emitted. A later target-only update is revalidated against the retained kind by
the authoritative session consumer.

### The one allowed exception: caller-agnostic process caches

A module-level cache is fine when it is keyed on an **external** signature and is
identical for every caller. `mcp_core._KNOWLEDGE_CACHE` is keyed on the
knowledge-DB and config file signature and is shared safely across calls. Never
key a cache, or any retained object, on caller identity, session, or "the last
request I saw".

### Checklist for a new tool

- No module global holds per-call or per-session data.
- Identity never comes from a bare env read. Anything mutating or targeting a
  session resolves it through `mcp_core.require_strict_session_key()`, and the
  module is listed in `mcp_core.REFLEXIVE_TOOL_MODULES`
  (`test_identity_topology.py` enforces both). Read-only paths may use the lenient
  `_resolve_session_key()`.
- A tool on `kirocrew-core`, `kirocrew-cron` or `kirocrew-dashboard` declares its
  display title in `src/kiro_crew/data/mcp_tool_titles.json`. The server serves it
  as the MCP `Tool.title`, and the dashboard and channel task labels read the same
  file for the tool-call row's title. `test_mcp_tool_titles.py` fails when a listed
  tool and the table disagree.
- Durable state lives behind a gateway endpoint keyed by session.
- The tool behaves identically whether it is the only caller or one of many
  sharing the backend.

## Troubleshooting

**MCP tools not working.** Check that `~/.kiro/agents/kirocrew.json` contains
`kirocrew-core` and `kirocrew-cron`, that `includeMcpJson` is `false`, then run
`kirocrew doctor` (which checks probe status) and read the live probe results in
the dashboard MCP panel.

**Status stays "Unknown".** The handler auto-triggers a probe for a server it has
no cache entry for, but the result only appears on the next refresh. If it stays
Unknown, the server is failing its handshake: read the dashboard error text or
the gateway log.

**Tools present in Kiro Crew but absent in interactive kiro-cli.** That is correct.
`kirocrew-core` / `kirocrew-cron` / `kirocrew-computer` are agent-scoped and must
not appear in interactive kiro-cli or Kiro IDE sessions. If they do, something
wrote them into a provider global.

**A newly added server does not appear in sessions.** On kiro-cli 2.21.0+ the
running sessions reconcile the agent file themselves and the dashboard skips its
reset (see "Live reconcile" above); a server that stays absent there means the
entry never reached `~/.kiro/agents/kirocrew.json` — check `kirocrew doctor` —
or the watcher failed at runtime, which the skip cannot see: `POST
/api/sessions/restart` is the recovery. On an older kiro-cli, or another
harness, the warm pool holds pre-spawned processes carrying the old config. Use
Apply & Restart or `POST /api/sessions/restart`. `kirocrew config set` restarts
nothing: a config write only changes the defaults new sessions adopt, and MCP
servers are not `config.json` keys.

**`-32602 Invalid request parameters` with empty data, only under the gateway.**
That frame is the Python MCP SDK refusing a request on a session that never got
`initialize` + `notifications/initialized`; the arguments were not lost. It
happens when a pooling multiplexer behind the pipe (the configured command is
its thin client) respawns the real server cold while its own connection stays
up: the gateway sent
`initialize` once for that backend and answers every later stub from its cache,
so nothing re-handshakes the new process. A standalone kiro-cli run works
because it is a fresh connection with a fresh handshake. The backend
recovers by itself (`Backend._retry_after_rehandshake`): on that exact frame, for
a method in `_REHANDSHAKE_RETRY_METHODS`, it re-sends the cached `initialize`
and `initialized` and the request, in one write, once. The gateway log line
`refused ... as not initialized; its MCP session was lost behind the pipe` marks
each recovery.

**Finding a failing tool call.** Every failed `tools/call` through the gateway logs
one WARNING to the gateway log, `mcp tool call failed: server=<s> tool=<t>
session=<k> error=<text>`. Both failure shapes count: a JSON-RPC `error` reply and a
`result` carrying `isError: true`, where the tool ran and reported its own failure.
The same call scores `ok: false` in the call metrics
(`MCP_GATEWAY_CALL_METRICS_PATH`).

## Workflow execution identity

Workflow writes resolve the current strict MCP session and pass that same key
to HTTP. This includes authoring, saved-definition runs, ad-hoc `source` and
`intent` runs, cancellation and subtree reruns. Missing strict identity refuses
the write before HTTP; a lenient ancestor-session fallback cannot authorize it.
Ordinary transport authentication identifies the caller; the captured execution
record selects its member and retention mode. Workflow IDs and template names
do not retarget memory. Run/detail/list/cancel/rerun preserve the original run's
member while applying ordinary execution permissions. Direct and pooled MCP use
the same rule. The deterministic workflow E2E model executes
these real MCP transports through `sandboxed_spawn_argv` and `popen_limited`,
which applies resource limits after exec rather than running Python in a fork
child. Temporary launcher profiles are cleaned up even when spawning fails;
synthetic tool events are not memory-access evidence.

### Compact skill discovery descriptions

Crew-owned `skill_search`, `skill_discover` and `skill_fetch` descriptions keep
local versus public-registry scope, read-only semantics, result limits, explicit
load instructions and the untrusted-content/sibling-file caveat. Full procedures
stay in the skill files rather than in repeated discovery prose. This affects
only the descriptors Crew owns; external MCP descriptions, Tool Search thresholds
and native serialization are unchanged and outside the measured assembly boundary.

Session-bound `skill_search` uses the already-admitted route
`/api/skills/-/discover`, delegating to `/api/skills`' local search branch. Search
and list are a `GET /api/skills/-/discover?scope=installed&q=...`; the exact-key
read POSTs a JSON body (`scope`, `q`, `action`, `key`, `capacity`, plus `offset`
and `limit` when the caller pages) to the same path, so a long or escaped key
never hits the HTTP request-line limit. No authentication paths or policy controls are expanded. The
server resolves only that session's project, applies the catalog's repo-scope
filter and identical-content deduplication, and loads confined project bodies
through `SkillsLoader.load_skill` with a shared 24,750-byte read allowance.
Those results contain safe names and content, not live project paths. Sessionless
CLI search remains global-only. Mixed CJK/English keywords use memory's existing
CJK-pair tokenizer. Native tool schemas and Tool Search thresholds are unchanged.

`skill_search` supports scoped `search`, paginated `list` and exact-key `read`. The
gateway resolves scope from the signed session, never a model-supplied agent name.
Without signed identity it uses the global installed catalog. Incremental indexing
reports incomplete recall explicitly; list/read remain available during refresh.
A read delivers at most one response's worth of body; a larger body is refused
with its size and read in whole-line pages through the same `offset`/`limit`
parameters, each page stateless and sized by the gateway to the capacity the
tool derives from its own response framing, and a refused read names which of
three reasons stopped it (outside the scope, unreadable, over the capacity).
See [memory, skills and hooks](../system-specs/modules/memory-skills-hooks.md).

### Codex session-control delivery

Eligible Codex sessions receive the dashboard server through their verified
per-session projection on create and resume; see [providers](../system-specs/modules/providers.md#codex-dashboard-session-mount).
The managed launcher is reconstructed before identity is attached, and broker
claims retain their per-session ownership checks. A global dashboard entry has
no verified session identity and cannot grant access by its name alone.
