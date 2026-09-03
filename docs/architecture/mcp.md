# MCP Server Architecture

How MCP (Model Context Protocol) servers are configured, merged, probed and
loaded, plus the two invariants every new Kiro Crew MCP tool must satisfy: it
ships as an MCP tool (not only a CLI command), and it holds no per-caller state.

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
> or run `kirocrew cli-setup`, which calls the narrowly-scoped
> `mcp_cleanup.clean_stale_managed_mcp()` helper.

## Config file hierarchy

| File | Owner | Purpose | Read by |
|------|-------|---------|---------|
| `~/.kiro/agents/kirocrew.json` | Kiro Crew gateway (`agent.rebuild_agent_config`) | The rendered Kiro agent: model + tools + merged `mcpServers` | kiro-cli, when spawned as the `kirocrew` agent |
| `~/.kiro/settings/mcp.json` | User | Kiro global MCP servers | kiro-cli for all agents; merged into Kiro Crew's agent file at render time |
| `~/.kiro/crew/mcp.json` | User, via the dashboard MCP panel | specific to Kiro Crew additions and per-server tool disables | Kiro Crew gateway only |

`rebuild_agent_config()` writes exactly **one** file, `~/.kiro/agents/kirocrew.json`.
There is no second rendered agent file and no agent-file renderer for any other
provider: Kiro Crew is KiroACP-only.

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
2. **`~/.kiro/settings/mcp.json`** (Kiro global) via `setdefault`.
3. **Seam-contributed provider globals** via `setdefault`, so they can only fill
   gaps the Kiro global did not. Empty in this build.
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
inherited PATH, deduped — before it is written out. Consequence to know about:
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

`agent._MANAGED_MCP_SERVERS` holds the three servers the gateway owns end to
end: `kirocrew-cron`, `kirocrew-core`, `kirocrew-computer`. Each is refreshed on
every rebuild by `_refresh_dynamic_fields()`, which rewrites `command`/`args`
from the live `kirocrew` binary, strips stale remote-transport fields (`url`,
`headers`) left by older builds, and re-pins `env.KIROCREW_HOME` to the home the
gateway is actually running under while preserving the user's own env keys.
User customizations such as `autoApprove` are preserved.

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
absence reads as informational rather than as a missing entry — the drift where
doctor demanded what emission deliberately omitted was #6548.
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
`"type": "registry"` marker on these three entries — added when
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

### The final auto-approve pass

`allowedTools` is kiro-cli's blanket auto-approve list, and it is the one path
that never reaches the PreToolUse gate. Builtin grants (`fs_read`,
`execute_bash`, …) arrive straight from the shipped agent template, so no
per-writer path re-touches them. The last thing `rebuild_agent_config` does
before writing is filter the whole assembled `allowedTools` list through one
predicate: a ref the governance ceiling has an opinion about loses its blanket
grant and its calls go through the gate, where the per-argument rule actually
applies; a ref the ceiling is silent about is kept. `mcpServers[*].autoApprove`
gets the same treatment on the final map. `tools` is deliberately left intact,
because mounting a tool is not auto-approving it. Withheld grants are recorded
in SEL as `mcp_auto_approve_withheld` so an operator can see why a template tool
now prompts.

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
state after the merge minus explicit `disabled: true` overrides in
`~/.kiro/crew/mcp.json`; the other badges are raw membership in that scope's
file.

Probes run from `POST /api/mcp/probe`:

- **stdio** servers are spawned and driven through an MCP `initialize` handshake
  followed by `tools/list`.
- **HTTP** servers get the same two JSON-RPC calls over POST.
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
- The handshake response is kept, not just the tool names: advertised
  `capabilities`, the `protocolVersion` the server ANSWERED with, `serverInfo`,
  and per-tool `annotations`. These feed the shareability verdict (below); the
  probe already paid for the round-trip, so reading them costs nothing.
- `client_info` overrides the identity sent in the handshake. The shareability
  pre-flight uses it to ask one server under two identities; such a run is
  excluded from the shared per-name probe cache, because a synthetic-identity
  handshake is a diagnostic and not the canonical observation the dashboard
  renders.
- A remote server that answers the handshake with `401` — or with `403` carrying a
  `WWW-Authenticate` challenge — and whose config has no static `Authorization`
  header gets status `needs_auth` and an empty `error`, not `error`. The probe
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
  3.10` with no ceiling, so a build on 3.14 would silently collapse the middle
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
  fields before it runs: the probe cache is keyed by NAME, so a row whose url was
  edited would otherwise inherit the previous endpoint's verdict.

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
  and names the `agent.sandbox_allow_unsandboxed_exec` remedy. Because the cause is
  the HOST, it recurs identically for every server on every discovery cycle, so the
  remedy paragraph warns once per server name
  (`_warn_probe_sandbox_unavailable_once`) and demotes repeats to DEBUG.
  - **A managed server FALLS BACK to its declared tool list when — and only when —
    the sandbox refuses.** `kirocrew-core` / `-cron` / `-computer` declare their
    tools statically in this package (`mcp_core._list_tools()` and friends, the very
    functions the stdio shim answers `tools/list` from), so
    `_managed_tools_in_process` can serve the listing with no subprocess at all.
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
      would beat the isolation the spawn provides — which is why an earlier revision
      that made this the primary path was wrong. Reaching the fallback means the
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
"Unknown" on the next page load rather than waiting out the TTL.

`_fix_stale_managed_command()` re-resolves the `kirocrew` binary on every
`list_servers()` call, because the stored absolute path goes stale after an
update: first `agent._resolve_kirocrew_bin()`, then `shutil.which("kirocrew")`
on the augmented PATH.

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

The Integrations page aggregates the scope files into one view with per-scope
badges. Clicking a badge **stages** an intent; the page accumulates staged
changes and exposes Apply / Discard. Only Apply performs writes.

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

No scope metadata is persisted. Apply does one-shot edits and forgets; state is
re-read from disk on the next page load, so external edits (`kiro-cli mcp
remove`, hand-edits) are picked up naturally.

Apply does **not** restart sessions. Scope changes take effect at the next
session spawn; the header's Apply & Restart calls `POST /api/sessions/restart`
to drain the warm pool of pre-spawned processes carrying the old config, so a
freshly installed server is mounted on the next session rather than the one
after it. The response carries `mcp_sync_ok`, and `RestartButton` READS it: a
reconcile that failed is reported in the danger tint instead of the usual
"sessions restarted, config applied". Honesty that lives only in a JSON body no
user sees is not honesty — the sessions did restart, but against a config that
may not match the sources, and that is the one thing the caller needs told.

A probe that FAILS after the sanitizer stripped a declared env key names the key
in its error (`_note_denied_env`). The sanitizer's own WARNING goes to the
gateway log, which is not where someone staring at a red badge is looking: a
Python server configured through `env.PYTHONPATH` fails the probe while working
in a session, and unexplained that reads as a probe bug rather than the
launcher boundary it is.

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
| MCP server | `GET /api/session-tool-policy` returns the calling session's `managedToolPolicy.exclude`, and the server filters `tools/list` and `tools/call` | Gateway round-trip |

`managedToolPolicy` and `includeMcpJson` are in
`bridges._FRAMEWORK_OWNED_AGENT_KEYS`, so they are refreshed from the template on
every boot rather than preserved as user preferences. Preserving them is wrong in
both directions: a template that later tightens `exclude` would never reach an
already-enabled install, and anything that edits the agent file could drop the
exclude list, which the framework would then faithfully preserve forever.

`neutralize` uses explicit tool lists rather than a wildcard, because the app
discovers the real tool names, so a server that grows a tool cannot quietly slip
past a stale pattern.

The third layer is defense in depth for hosts that ignore `disabledTools`, and it
fails **open** by design: kiro-cli calls `tools/list` once at session start, so
returning an empty list on a transient gateway failure would leave that session
permanently believing the server has no tools, unrecoverable without a restart.
A missing session key is not cached (a startup race must be retryable); a
resolved key whose policy call fails gets a 30s negative cache so a persistently
unreachable gateway does not add a 5s timeout to every tool call. The gateway
side is deny-by-default in the opposite sense: a caller that cannot prove its
identity gets a 400/404, never an empty policy.

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
| `kirocrew-cron` | `kirocrew mcp-cron` (`mcp_cron.py`) | `cron_add`, `cron_list`, `cron_update`, `cron_remove`, `cron_remove_all`, `cron_pause`, `cron_resume`, `cron_trigger` |
| `kirocrew-core` | `kirocrew mcp-core` (`mcp_core.py` + `mcp_tools/`) | spawn/subagent, learn, task, messaging, artifact, workflow, knowledge and session-directive tools (see below) |
| `kirocrew-computer` | `kirocrew mcp-computer` (`mcp_computer.py`) | `computer_list_apps`, `computer_launch_app`, `computer_get_state`, `computer_click`, `computer_drag`, `computer_type_text`, `computer_press_key`, `computer_set_value`, `computer_scroll`, `computer_perform_action`, `computer_end_turn` |
| `kirocrew-dashboard` | `kirocrew mcp-dashboard` (`mcp_dashboard.py`) | `chat_folder_tree`, `chat_folder_create`, `chat_folder_move`, `chat_folder_move_session` |

CLI commands and their MCP twins:

| CLI command | MCP tool | Server |
|-------------|----------|--------|
| `kirocrew cron add` | `cron_add` | `kirocrew-cron` |
| `kirocrew cron list` | `cron_list` | `kirocrew-cron` |
| `kirocrew cron update` | `cron_update` | `kirocrew-cron` |
| `kirocrew cron remove` | `cron_remove` | `kirocrew-cron` |
| `kirocrew cron remove-all` | `cron_remove_all` | `kirocrew-cron` |
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

`kirocrew-core` tools with no CLI twin, grouped by concern (authoritative list:
`kiro_crew.mcp_tools.build_tool_list()`, which is what `mcp_core._list_tools`
answers `tools/list` from):

- **Subagents:** `spawn_status`, `spawn_continue`, `spawn_steer`,
  `spawn_release`, `spawn_sub_agents`, `wait`
- **Messaging and notification:** `send_message`, `send_notification`,
  `delete_message`, `file_send`, `read_slack_profile`. `send_message` is the
  agent's only proactive egress, and it names its destination rather than
  inferring one: `session="slack"` / `channel` / `user` / `thread_ts` are the
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
  - **`channel_type` is the one `send_message` argument that requires
    `_resolve_session_key_strict()`.** It posts into one specific conversation,
    which is the "targets a specific session" case below; the lenient walk
    climbs process ancestors, so a sub-agent would resolve to its parent and
    deliver into the parent's chat window. An unresolvable identity refuses the
    call rather than guessing.
- **Session-bound directives** (`session_directive.DIRECTIVE_TOOLS`):
  `ask_question`, `suggest_followup`, `monitor_start`, `monitor_watch`,
  `monitor_update`, `monitor_stop`, `autonudge_stop`, `set_project`,
  `reset_conversation`
- **Structured monitor read:** `monitor_inspect` (strict authenticated session
  identity only; no ancestor fallback)
- **Crew routing:** `select_crew`
- **Sessions and history:** `list_sessions`, `get_chat_session`,
  `search_chat_history`
- **Artifacts:** `artifact_list`, `artifact_get`, `artifact_save`,
  `artifact_update`, `artifact_delete`, `artifact_move`, `artifact_versions`,
  `artifact_revert`, `artifact_folder_list`, `artifact_folder_create`,
  `artifact_folder_rename`, `artifact_folder_move`, `artifact_folder_delete`,
  `artifact_get_comments`, `artifact_post_comment`, `artifact_reply_comment`,
  `artifact_delete_comment`, `artifact_mark_review`, `deploy_artifact`
- **Knowledge and skills:** `local_knowledge_search`, `knowledge_dedup`,
  `knowledge_list_sources`, `skill_discover`, `skill_search`, `skill_fetch`,
  `browse_outline`, `browse_search`
- **Workflows and hooks:** `workflow_author`, `workflow_list`,
  `workflow_cancel`, `workflow_rerun_subtree`, `register_hook`
- **Diagnostics:** `resource_status`, `issue_radar_record_investigation`
- **App bridges (credentialed):** `ops_mission_control_api` — the MCP server
  process holds the gateway's internal secret and forwards only a frozen
  (method, path) allowlist of Ops Mission Control routes; the agent never
  sees a credential (same shape as `issue_radar_record_investigation`)

### A `kirocrew-core` tool has two halves

Each tool is declared twice in the same per-domain module under
`kiro_crew/mcp_tools/` (`spawn.py`, `artifacts.py`, `workflows.py`, …), and
nothing at runtime notices when only one half lands:

- Its **descriptor** — name, model-facing description, JSON Schema — is returned
  by that module's `schemas()`. `build_tool_list()` concatenates every domain's,
  and `mcp_core._list_tools` answers `tools/list` from it.
- Its **handler** is an entry in that module's `HANDLERS` map, called as
  `handler(name, args)`. `dispatch()` finds it by name and
  `mcp_core._call_tool_inner` delegates to that.

A descriptor with no handler advertises a tool that answers with the
dispatcher's fallthrough; a handler with no descriptor is unreachable, because
the model is never told the name. `test/test_mcp_tool_registry.py` fails when
either half is missing, when the two halves land in different domains, or when a
name is claimed twice.

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

External servers a user may install (a Slack server, anything else) are ordinary
user-added servers: they live in one of the scope files and are merged into the
agent config at render time. They are not managed, so a `mcp_server_alias`
normalization pass rewrites slash-containing keys to kiro-safe aliases: kiro-cli
splits an agent `@server` reference on `/`, so a slash-containing key is
mis-parsed as `@server/tool` and exposes none of the server's tools.

**Browsing is deliberately not an MCP server.** The agent drives a browser by
running `playwright-cli` commands on its ordinary shell path, so no tool schemas
are re-sent per request and the accessibility tree stays on disk instead of
entering the model context. See [browser](../system-specs/modules/browser.md).

## What belongs in `kirocrew-core`, and what does not

`kirocrew-core` is the surface EVERY session carries. kiro-cli reads `tools/list`
once per session, so a tool listed there spends context in every request of every
session for as long as the session lives — whether or not that session will ever
use it. With `agent.tool_search` on (the default) Kiro Crew forces kiro's deferral
always-on, so the per-request cost is a name plus a description rather than a full
JSON schema; it is smaller, not zero, and it scales with the tool count.

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
Folders now answer the same question: a folder created by an app carries it in
`owner_app`, and an absent key reads as the person's — which is why the field
arrived without a migration, since every folder written before it existed is the
person's. An app may create at the top level or inside a folder it owns, and may
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
instead. Nothing shipped loses a capability -- no MCP tool exposes deletion and the
only client of the route is the dashboard UI -- and an app organizes its work by
creating, renaming and reparenting its own folders and filing its own sessions. The
person deletes a full folder exactly as before.

The policy lives in the endpoints, not in the MCP server. Only the endpoint holds
the store lock and sees the authoritative tree, so a second copy of the rule in
the tool layer could only drift or race. What the tool layer still decides is the
one question the endpoint cannot: whether the caller can be placed at all, since
an unverifiable or delegated caller has no scope to bound a write to.

Moving the decision to the endpoint makes the write's IDENTITY load-bearing, so
the gate returns the key it verified and every folder write sends that key
unchanged. The write helpers default to `_resolve_session_key`, whose `/proc`
ancestor walk can resolve to a different slot than `_resolve_session_key_strict`
did; letting them re-resolve would check one identity and write under another,
and for an app-owned session the walk landing on an ancestor makes the write
arrive looking like the unconfined person -- which would let an app reach the
folders the ownership rule exists to protect. `chat_folder_move_session` already
worked this way; create and move now do too, including each intermediate folder a
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

**Assignment is still not authorization.** Being unreferenced by default keeps a
capability cheap and deliberate; it does not prove the user consented to reach the
agent does not otherwise have. For that, `config.json` is the WRONG home —
`security.py` spells out why, and the keystone leaves (`computer_use.json`,
`browser-mode-enabled`, the Ops Mission Control mode) exist because each grants
something outside Kiro Crew (desktop input synthesis, the operator's logged-in
browser, writes against production incident tooling) or is the security floor
itself. One of those moved out of agent-writable config after review found exactly
this mistake.

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
`onboarding_import._MANAGED_MCP_NAMES`, and the hidden `cli.py` subcommand.
`test_computer_use_registration.py` asserts those registries are the same set, so
a half-registered server fails the suite rather than shipping.

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

**A new `kirocrew-core` or `kirocrew-cron` tool MUST NOT keep per-caller or
per-session state in the MCP-server process. Resolve the caller's identity on
every call and keep authoritative state in the gateway.**

### Why: the shared-backend invariant

The managed servers are long-lived stdio subprocesses, and **one server process
serves many sessions.** In the pooled topology a single warm backend is reused
across sessions, and a sub-agent spawned via `spawn_run` runs inside the parent
slot's process tree and talks to the same MCP server. Anything the process
remembers is therefore shared by every session and sub-agent that touches it.
Two failure modes follow.

**1. Identity is not the process, it is the call.** `KIROCREW_SESSION_KEY` and
`os.getppid()` identify the *process*, which is wrong by construction in a shared
backend: the warm pool spawns with an empty key, and a sub-agent inherits its
parent's tree. `mcp_core.py` offers two resolvers:

- `_resolve_session_key_strict()` for **anything that mutates or targets a
  specific session** (post to a slot, change its state, deliver a callback). It
  accepts only the gateway-injected caller context (`mcp_caller.current_caller()`,
  which gatewayd stamps on every forwarded frame after stripping any
  client-forged `kirocrew.caller` block), the injected `KIROCREW_SESSION_KEY`, or
  a `KIROCREW_HOST_PID` lookup whose HMAC sidecar verifies against the
  keystone-protected `sel_hmac.key`. It deliberately **drops** the `/proc`
  ancestor walk and the bare `session_pid_<pid>.txt` fallback: the `.txt` file is
  agent-writable and therefore forgeable, and a sub-agent walking ancestors from
  its own MCP child resolves to its **parent** slot, which would let it mutate
  the wrong conversation.
- `_resolve_session_key()` (lenient, still walks ancestors) is only for read-only
  and telemetry callers where misattribution is harmless.

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
is what the `ask_question` MCP tool itself now does, along with `monitor_start`,
`monitor_watch`, `monitor_update`, `monitor_stop`, `autonudge_stop`, `set_project`
and `suggest_followup`, and `reset_conversation`
(`session_directive.DIRECTIVE_TOOLS`). The tool validates its arguments and
returns a human-readable confirmation plus a marker line carrying the validated
payload and **no session key**. `dashboard/chat_runner`'s tool-result handler
decodes the marker, applies the effect against **its own** `slot.key`, then
strips the marker from the stored transcript. Sub-agent isolation is therefore
structural rather than cryptographic: a sub-agent's tool result flows through the
sub-agent's own runner, so it can only bind to the sub-agent's session. There is
no walk to get wrong.

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

Structured monitoring deliberately splits mutation from inspection.
`monitor_watch`, `monitor_update`, and `monitor_stop` are directives: the
consumer applies them to its authoritative session, so their payloads contain
neither a session key nor a loop id. `monitor_inspect` needs a result in the same
turn and therefore calls the session-bound read route only after
`_resolve_session_key_strict()` succeeds, passing that exact key to `_get`.
It projects that response into bounded agent-oriented state (check counts and
accepted wake count, plus only a small failed/pending/unknown name sample),
omitting wake instructions and browser/persistence internals. Inspection reports
unavailable when strict identity is absent; it never falls back to the
process-ancestor resolver. Every structured-monitor tool refusal caused by a
missing or unsupported session binding returns an `Error:` result and writes an
explicit `denied` SEL event; the shared MCP wrapper therefore records the call as
failed rather than completed.

### The one allowed exception: caller-agnostic process caches

A module-level cache is fine when it is keyed on an **external** signature and is
identical for every caller. `mcp_core._KNOWLEDGE_CACHE` is keyed on the
knowledge-DB and config file signature and is shared safely across calls. Never
key a cache, or any retained object, on caller identity, session, or "the last
request I saw".

### Checklist for a new tool

- No module global holds per-call or per-session data.
- Identity comes from `_resolve_session_key[_strict]()`, never a bare env read.
- Anything mutating or targeting a session uses the **strict** resolver.
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

**A newly added server does not appear in sessions.** The warm pool holds
pre-spawned processes carrying the old config. Use Apply & Restart, or
`kirocrew config set`, which triggers a restart.
