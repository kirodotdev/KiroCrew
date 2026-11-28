---
title: Zero-approval MCP sharing -- every local MCP server is shared by default, with no approval step, inside the global sandbox where it fits
status: draft
author: bolichen97, with kirocrew-worker
created: 2026-11-27
last-audited: 2026-11-27
audited-at: dcd82d6bbf
doc-pr: null
implementation-prs: []
tracking-issues: [42105, 40319]
supersedes: []
superseded-by: []
---

# RFC: Zero-approval MCP sharing

**Status:** draft. Nothing here is on main. The code is written on three pushed
branches with no pull request yet (§9). Every reference to main was read at
`origin/main` `dcd82d6bbf`. A citation names the file and the symbol, not a line
number. Paths that start with `src/`, `docs/` or `test/` are from the repository
root; every other path is under `src/kiro_crew/`. Branch heads are named by the
commit that was tested, so a reader can check out exactly what the numbers in §8
describe. Five design questions were decided by the maintainer on 2026-11-27
and are written in as decisions A and B (§5.2), C and D (§5.4) and E (§5.1);
the document as a whole still awaits acceptance.

Part of [#42105](https://github.com/kirodotdev/KiroCrew/issues/42105) (sharing N
servers restarts the broker N times) and
[#40319](https://github.com/kirodotdev/KiroCrew/issues/40319) (sharing without the
folder question). Neither closes on this document.

## 1. Summary

Today a person who wants one copy of an MCP server for all chats, instead of one
copy per chat, has to find a switch, then approve each server, answer a folder
question per server, and approve again whenever the launch changes. Most people
never do it, so each chat starts its own copy of every server, and hosts run out
of memory.

This RFC makes sharing the default and removes the approval concept:

1. Every local (stdio) MCP server is shared across chats by default. That holds
   for a new server, an edited one and one a package manager updated. Nobody is
   asked.
2. In place of the person's look at the launch, a shared server runs inside the
   global OS sandbox at its default (`standard`) tier, where that sandbox
   exists and the server can run in it. That is the profile a chat's agent runs
   in unless the session is confined further (a `strict` tier or a harness's
   own credential mask); such a session keeps its servers per chat.
3. The only controls left are: turn sharing off for one server, turn it off for
   the whole install, and (a follow-up) limit a server to some sessions.
4. Servers that used to be unsafe to share (folder servers, roots servers,
   servers with state, browsers) are made correct on one shared copy, so the
   default does not break them.
5. The gateway daemon is fixed so that 64 or more shared servers all start, a
   slow server does not lock out the others, and a chat reconnects after the
   daemon restarts.

## 2. Motivation

### 2.1 What a person goes through today

An audit of main counted 15 distinct user-visible steps around sharing. Each one
is in the code today:

| # | Step | Where on main |
|---|---|---|
| 1 | "Turn on MCP server sharing" switch, off by default | `mcp_gateway.enabled` in `config/integration_sections.py` |
| 2 | A per-server "Share" switch; the roster is empty by default | `stub_servers` / `stub_overrides`, same file |
| 3 | "Share these N?" bulk list, refused for a batch (`batch_stub_requires_individual`) | `dashboard/handlers/mcp_gateway_stubs.py` |
| 4 | "Review and approve" dialog showing the full command (`expected_launch_required`) | same file; `mcp_gateway/launch_approval.py` |
| 5 | "Share {name} across chats?" folder question (`cwd_unconfirmed`) | same file |
| 6 | "Allow sharing (it doesn't use the folder)" after a roots request | same file; `mcp_gateway/folder_use.py` |
| 7 | Row "Paused - command changed" (`REFUSED_CHANGED`) | `mcp_gateway/launch_approval.py` |
| 8 | Row "Paused - not approved yet" (`REFUSED_UNAPPROVED`) | same file |
| 9 | "Always trust changes to this server" checkbox | `mcp_gateway/launch_approval_heal.py` |
| 10 | Overview banner "N servers not shared" and a doctor issue | `doctor_checks/mcp.py` |
| 11 | "Measure N servers" button | `mcp_gateway/evaluate.py` |
| 12 | "Saved. Restart Crew to use it." | MCP page |
| 13 | "Open chats keep their own copies until you reload them." | MCP page |
| 14 | Dead-end errors: launch too long to show, too many launches, nothing to run | `mcp_gateway_stubs.py` |
| 15 | The in-chat change card when an agent asks to add a server | `change_card_mcp_server.py` |

Step 7 fires on changes a person did not make. A version manager or package
manager that moves the `npx` or `uvx` path changes the hashed launch, so the row
pauses again. On a real Linux host, moving an installed server's path paused it
and the chat then reported the tool missing.

Measured on four real machines on main (§8):

| Machine | Servers | Steps to share them all |
|---|---|---|
| Linux (cloud VM) | 13, then 30, then 64 | 62 share or folder dialogs (13, then 15, then 34) |
| macOS | 13 | 14 clicks, plus a first-run dialog |
| Windows | 14 | 1 switch, 11 row clicks, about 9 dialogs |
| Linux (hosted dev box) | 4 new | 8 decisions |

On every machine the first switch alone shared nothing: the dialog promised 7
servers, then said "Saved, but sharing is not running yet."

### 2.2 What it costs not to share

Each chat starts its own copy of every unshared server, plus a small proxy
process per server.

| Machine | Load | Measured |
|---|---|---|
| Linux | 8 chats, sharing on for some | used memory 3.2 GB to 13.6 GB; 136 proxy processes |
| Linux | 4 chats, 66 servers | 264 proxy processes, 7.5 GB; the kernel OOM killer fired twice |
| Windows | 16 chats, sharing off vs on | peak 29.8 GB vs 22.1 GB of 32 GB; node 269 processes (19.8 GB) vs 28 (6.3 GB) |
| macOS | 8 chats | 264 proxies at 23 to 25 MB footprint each |

### 2.3 What users reported

Users reported, roughly in order of frequency: chats that cannot start because
Kiro Crew's own MCP servers time out; user servers missing in workers, Bots and
sub-agents; servers that stop working after a package update until re-approved;
memory and process counts growing with each chat; a sharing dialog that is
confusing and resets every chat once per server; Windows hosts with hundreds of
Python processes; and servers added, renamed or removed that open chats do not
see.

### 2.4 Why sharing is unsafe to turn on today

The approval exists because a shared server is started by the gateway daemon
outside every session's sandbox (`mcp_gateway/backend.py`, the
`asyncio.create_subprocess_exec` call in `spawn_backend`). And several common
servers give wrong answers when shared, measured with the real servers:

| Server | Shared today |
|---|---|
| server-memory | 40 parallel writes from two chats keep 1 entity |
| playwright | chat B's navigate moves chat A's page; A reads "PAGE-B" |
| puppeteer | B's `location.href` returns A's URL |
| server-filesystem with no folder argument | does not start (no roots offered) |
| mcp-server-git with no `--repository` | runs in the daemon's folder, not the chat's |
| sequential-thinking | history is shared; the fields a chat uses do not change |

## 3. Owner rules

The maintainer set three rules. The design follows them; they are not open for
this RFC to revisit.

1. **Chats on one host may see each other's MCP state.** All agents on a host
   are the same local user. A shared memory graph, a shared folder server that
   can read another chat's project, or a roots list naming every chat folder a
   backend has served is acceptable. The bar is correctness: no lost write, no broken flow,
   no wrong result.
2. **Shared servers run inside the global sandbox where it fits, and not where
   it does not.** On Linux that is Kiro Crew's own user-namespace launcher
   (`sandbox.py`, `sandbox_launcher_program.py`); on macOS it is
   `sandbox-exec`. Windows has no OS sandbox, so nothing changes there. No
   server is exempt for what it launches: a server that starts Chrome or
   Chromium is wrapped like any other (§5.2, decision B).
3. **There is no approval concept.** Every server, including a new one, an
   edited one and one a package manager updated, is shared by default. The
   person can only turn sharing off for one server (or for the install), or
   limit a server to some sessions.

## 4. Goals and non-goals

Goals:

- Zero clicks to share on a fresh install, and zero after any later add, edit or
  update.
- A shared server's reach is no wider than a per-chat copy's on the same host,
  wherever an OS sandbox exists and the server is wrapped. A session confined
  beyond the default `standard` tier (a `strict` tier, a stricter per-harness
  tier, or an enforced harness's credential mask) never attaches to a shared
  backend; its servers stay per chat (§5.2, decision A).
- The common stateful and folder servers stay correct on one shared copy.
- 64 or more shared servers all start, and chats recover after a daemon restart.

Non-goals:

- **Per-tool permission prompts.** A custom agent with no allow list still asks
  for each tool call, and a prompt nobody answers is declined after 600 s
  (`TOOL_APPROVAL_TIMEOUT` in `constants.py`, `tool_approval_timeout_secs` in
  `config/sections_agent.py`). That is the tool gate, not MCP sharing, and this
  work does not loosen it.
- Remote (HTTP) servers, which are not proxied this way.
- Project-folder MCP approval (`/api/mcp/project-servers/approve`), a separate
  flow.
- Cutting the proxy process count to one per chat. That is a rewriter design
  change, measured in §8 and left for later.
- Remote server sign-in prompts (a 401 with `WWW-Authenticate` still shows
  "failed to start" with no sign-in offer).

## 5. Design

Four code changes and one follow-up. Each is a pull request; §9 gives the order.

### 5.1 Auto-share, no approval, and the live watcher

Branch `feat/mcp-auto-share-approve`, tested heads `643f8ecaa11c` and
`e0e784ea821f` (§8 numbers come from these).

- **No approval step.** The gateway start's rewrite pass admits every launch the
  first time it is seen and whenever it changes (arguments, env, a moved
  toolchain path, a package update). It records each one in the existing launch
  store with the audit reason `AUTO_AUDIT_REASON`
  (`mcp_gateway/launch_approval_heal.py`), so the store stays the record of what
  ran. The "Always trust changes" checkbox and the review dialog go away.
  Servers that are never pooled, and the per-server launch cap, still refuse.
- **Shared by default.** `mcp_gateway/auto_share.py` gives every stdio server
  nobody decided about `stub_overrides[name] = true`. Candidates come from the
  rewriter's own declarations (`~/.kiro/agents`, crewmate files under
  `~/.kiro/mates/agents` and settings; decision E below), matched by exact
  name and canonical alias, and the pass decides again inside the config lock.
  `mcp_gateway.enabled` defaults to true. A stored `false` written by an older
  build is adopted once, so existing installs get sharing too. A person's off
  (one server or global) wins and survives restarts.
- **Stale entries.** `auto_share.stale_on` drops a `true` override for a server no
  declaration carries any more, so the switch never counts deleted servers. A
  person's off is kept.
- **Live pickup.** `_watch_mcp_declarations` in
  the gateway runtime's MCP broker stats the declaration files every 15 s
  and reads them only when one moved. A changed launch set (added, removed, or a
  command, argument or env edit) re-runs auto-share and restarts the broker. A
  failed pass retries on the next poll. A poll while sharing is off does nothing.
- **Folder answer only when needed.** A launch whose arguments and env name no
  folder needs no folder answer. A launch that names the chat's folder stays per
  chat, and the row says so.
- **Measure on first sight.** One budgeted pre-flight pass runs after each
  background probe round (`evaluate.first_sight`): only servers that answered,
  never a disabled one, one that needs sign-in, or one with a `secret://` value.
- **Quiet refusals.** The refused-share banner becomes an info line; doctor
  reports a refusal as info.
- **Decision E (maintainer, 2026-11-27): crewmate servers are shared too, with
  no new seal.** Launches read from files under `~/.kiro/mates/agents` (each
  crewmate's own file and the crewmate conductor and worker specs the rewriter
  reads from that folder through `with_conductor_source` in
  `mcp_gateway/mate_overlays.py`) are auto-shared like any other declaration.
  That folder has no OS seal in sessions, and the maintainer accepts the risk
  this leaves (§6 item 5) rather than add a sandbox seal in this design.
- **Agent add card.** An add card that keeps a new local server per chat records
  `stub_overrides[name] = false`, so auto-share does not share what the person
  just chose to keep per chat.

### 5.2 Shared backends inside the global sandbox

Same branch. `_sandboxed_backend_argv` in `mcp_gateway/backend.py` wraps every
shared backend on Linux and macOS with the existing `wrap_argv` in
`sandbox_wrap.py`, in `standard` mode, plus its cgroup scope and the session
host's resource limits. This is the same wrap the MCP pre-flight probe and App
Kit app backends already use. It is also the profile a per-chat server runs in
as a child of the sandboxed agent when the session has no confinement beyond
the default: `agent.sandbox` is `auto` and the chat's harness adds no mask of
its own. The wrap applies only `standard` plus the governance floor
(`sandbox.min_level`, which `wrap_argv` re-reads). It does not follow a
session's own stricter confinement: `agent.sandbox: strict`, a per-harness tier
in `agent.harness_sandbox`, or the credential mask an enforced harness puts on
its process tree (`agent_sdk/tool_gate.py`, `adapter_hidden_credential_dirs`).

**Decision A (maintainer, 2026-11-27): a session confined beyond `standard`
keeps its servers per chat.** Such a session is never attached to a
`standard`-tier shared backend. Its per-chat copies run as children of its own
sandbox, so they keep that session's tier and masks. Reason: a shared backend
at `standard` would let those servers read credential files the operator chose
to hide, and the owner's rule is to sandbox where it fits and otherwise not
share it into a weaker fence. Wrapping each pool at the strictest attached
session's confinement was not chosen, because it adds a pool dimension and
changes a running backend's fence as sessions come and go. Keeping `standard`
and accepting the widening was not chosen either. `e0e784ea821f` does not
implement this yet; PR 1 adds it.

- The backend's private temp folder is the one writable carve-out. A forwarded
  SSH agent socket is passed only when the operator named it.
- Kiro Crew's own managed servers stay unwrapped: their launch is derived by
  code, and gatewayd attests them.
- **Decision B (maintainer, 2026-11-27): no browser exemption.** Every shared
  backend runs inside the sandbox on Linux and macOS, including a server that
  starts Chrome or Chromium. Chrome cannot start its own sandbox inside ours:
  on Linux the launcher sets no-new-privileges, which disables Chrome's setuid
  helper, and its seccomp filter denies `unshare`, `setns` and `mount`, which
  Chrome's namespace sandbox needs (both Linux machines showed "No usable
  sandbox!"); on macOS Chrome's helpers hit `forbidden-sandbox-reinit` inside
  `sandbox-exec`. So the gateway adds `--no-sandbox` to a shared playwright-mcp
  launch, but only when its declaration names neither `--sandbox` nor
  `--no-sandbox`. That turns off Chrome's own sandbox only; ours stays on. A
  run inside the Linux sandbox showed Chrome failing with its own sandbox on
  and starting with `--no-sandbox`. Reason: an earlier draft let a launch that
  named a browser run unwrapped, and because that was a match on launch text,
  any declaration could name a browser to leave the sandbox. Widening the
  seccomp filter was not chosen either, because it weakens the fence for every
  wrapped process. `e0e784ea821f` still exempts browsers on macOS, and
  `fb0e016bdec1` drops the exemption but does not add `--no-sandbox` yet, so
  shared playwright fails there; auto-share `467e7f360c9c` carries this
  decision. A shared puppeteer server gets the same through its env
  (decision C, as reversed, §5.4).
- The wrap is refused, and the server falls back to one copy per chat inside the
  chat's sandbox, when no sandbox can be built, when a declared loader variable
  (`LD_*`, `DYLD_*`) differs from the daemon's own, or when the launcher would
  scrub a declared env key. A refused wrap never falls back to an unwrapped run.
  A wrap that confined nothing (`wrap_argv` returned the argv as it was: the
  operator's no-isolation opt-in, or an in-sandbox marker the daemon
  inherited) is refused the same way, so a shared backend is never wider than
  a per-chat copy; the daemon drops an inherited marker at start.
- Windows is unchanged: there is no OS sandbox to wrap with, and shared
  backends start as they do today.

What the sandbox gives a shared backend, checked inside the profile: the
credential homes the profile masks stay masked, the Kiro Crew `.env` is an empty
mask, and the MCP declaration files, `config.json` and the launch store are
read-only. Project folders, `/tmp`, `.aws`, `.ssh` and the network stay open, as
they are for a chat at the `standard` tier. The `strict` tier also hides `.aws`,
`.ssh` (except `known_hosts`), `.kube`, `.config/gh` and the package-registry and
git credential files (`agent.sandbox` help text in `config/sections_agent.py`),
and an enforced harness hides the whole sensitive-home floor from its tree; a
shared backend gets neither, which is why decision A keeps those sessions'
servers per chat.

### 5.3 Folder and roots scoping

Branch `feat/mcp-folder-scope`, tested heads `c05422cb6358` and `7624c9298a58`
(§8 numbers come from these). All per-caller work runs at one hook on the gateway's forward path,
`mcp_gateway/backend_caller_scope.py`.

- **Where the chat's folder comes from.** The Crew-authored `claim` frame carries
  the chat's `work_dir` (`mcp_gateway/claim.py`). The daemon keeps it per
  kernel-attested session key (`folder_scope.record_session_folder`). It never
  comes from the proxy's register frame, which the peer controls.
- **Arguments.** In a `tools/call`, a relative folder argument (`repo_path`,
  `cwd`, `working_dir`, `workdir`, `directory`, `dir`, `project_path`) becomes
  absolute against the caller's folder, and a missing required one is filled in.
  Other path arguments are rewritten only for a server whose roots the gateway
  serves. `repo` and `repository` are left alone, because hosting servers use
  them as names.
- **roots/list.** The gateway answers a server's `roots/list` itself and does
  not recycle the backend. The answer lists the folders named in the server's
  own launch arguments first (a roots answer replaces them in server-filesystem),
  then every chat folder the backend has served. A folder stays in the answer
  for the backend's life once a chat used it, because a chat's proxy connection
  comes and goes inside one open session; the list is capped at 64. The gateway
  sends `notifications/roots/list_changed` when a chat with a new folder
  attaches. It answers this way only when every tool can be scoped (readable
  schema, no command or URL argument) and at least one folder is known.
- **roots/list covers servers before MCP 2026-07-28 only.** In the 2026-07-28
  revision a server no longer sends requests, `notifications/roots/list_changed`
  is removed, and Roots is deprecated
  ([SEP-2577](https://modelcontextprotocol.io/specification/2026-07-28/changelog)).
  A 2026-07-28 server asks for roots by returning an `input_required` result
  that the client answers on a retry, and every request carries the client's
  capabilities in `_meta` (and should carry `clientInfo`). How the gateway answers that per
  caller is open item 10.
- **Known limit, upstream: macOS `/tmp` paths in server-filesystem 2.4.1.**
  Once a client offers any roots, server-filesystem keeps only the resolved
  roots (`/private/tmp/...` on macOS) and does not resolve a requested
  `/tmp/...` path before its check, so it denies `/tmp/...` and `/var/...`
  paths. A run against the bare server, with no Kiro Crew in between, showed
  it: with only a `/tmp/mcpmac` argument and no roots the listing passed, and
  with roots of `file:///tmp/mcpmac` or `file:///private/tmp/mcpmac` it was
  denied ("/tmp/mcpmac not in /private/tmp/mcpmac"). This is the server's own
  behaviour, not a sharing bug; the gateway's roots answer is only what turns
  it on for a shared filesystem server. On Linux it applies only where a root
  path is itself a symlink.
- **Nothing is refused.** Under owner rule 1, a shared folder server can read any
  attached chat's folder.

### 5.4 Stateful servers on one copy

Same branch.

- **Serialized calls, only for servers with shared mutable state**
  (`mcp_gateway/call_gate.py`, `CallGate`). **Decision D (maintainer,
  2026-11-27).** A backend keeps one `tools/call` in flight only when the
  server holds shared mutable state: a memory or knowledge-graph store that
  reads, changes and saves one file, or a browser with a current page.
  Stateless request/response servers (filesystem, fetch, time, and the many
  company-internal servers that wrap a web API) run calls concurrently, as
  their per-chat copies do today. PR 3 names how a server is recognized as
  stateful without a server name list (open item 11).
  - Reason: the branch heads up to `9222a94f20a7` serialized every server with
    a tool lacking `readOnlyHint: true`. On the hosted dev box that rule
    serialized 37 of the 41 servers that answered (each server's `tools/list`
    judged by the branch's rule), because most servers declare no hints; the
    four left concurrent were fetch, time, a test fixture and a server that
    lists no tools. Measured with
    the real `Backend` class, it added real waits: a fast call behind a slow one waited +16.7 s on filesystem, +9.2 s
    on playwright and +1.7 s on a mail server, and five chats on one search
    server lined up to 2.1 s for a 0.5 s search. Through the full gateway, a
    chat behind a 20 s `browser_wait_for` waited about 20 s. The servers that
    actually lose state when shared are the small set above (memory kept 1 of
    50 parallel writes unserialized). Browsers stay serialized under this
    decision, so their waits remain, as the costs below accept.
  - The protocol calls tool annotations untrusted
    ([tools, 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)),
    so `readOnlyHint` is not used as the signal. Had it been, a false
    `readOnlyHint: true` would only have turned serialization off for that
    server's own users; it raises no privilege.
  - Every cancel path goes through `CallGate.cancel`: a call not yet written is
    never sent, and a sent call frees the slot on its reply or 5 s
    (`CANCEL_GRACE_SECS`) after its cancel. Measured: a chat queued behind a
    cancelled playwright call finished 5.1 s after the cancel.
- **One browser tab per chat** (`tab_tenancy.py`). It engages by tool shape: a
  tabs tool with list, new, close and select and an `isolatedContext` field.
  Inside the serialized slot the gateway opens the caller's own tab in its own
  context and selects it before the call. A chat sees only its own tabs, and
  `browser_close` closes only its own. On the retested heads chats could still
  see each other's pages; the maintainer accepts that (open item 12).
- **Single-page browsers** (a `*navigate` tool with a `url`, no tabs tool) are
  recorded as the new hazard `single_page_browser`. A backend already shared by
  two or more chats is recycled, and each chat gets a private copy through the
  existing hazard retreat.
- **Decision C (maintainer, 2026-11-27; reversed by the owner on 2026-11-28):
  a shared puppeteer server gets Chrome's sandbox turned off the way shared
  playwright does.** server-puppeteer reads `PUPPETEER_LAUNCH_OPTIONS` (JSON
  launch options) and refuses a dangerous argument such as `--no-sandbox`
  unless `ALLOW_DANGEROUS` is `true`. For a shared backend that runs inside our
  OS sandbox (Linux, macOS; not Windows), the gateway sets
  `PUPPETEER_LAUNCH_OPTIONS={"args": ["--no-sandbox"]}` and
  `ALLOW_DANGEROUS=true`, and nothing else. It sets neither when the server's
  own declaration already sets either key. Our sandbox stays on and still holds
  the whole process tree. The first decision left puppeteer failing inside the
  sandbox, as it does per chat on main (§8.1, §8.2); the owner reversed it so a
  shared puppeteer server works. The cost: with `ALLOW_DANGEROUS` on, a tool
  call may also pass other dangerous launch options to that browser.
- sequential-thinking is not serialized: its shared history has no read-modify-write
  step, so concurrent calls do not change the fields a chat uses (§2.4).

Costs, accepted: chats wait for each other on a serialized server, about 0.8 ms
per queued memory call and about 19 ms per playwright call. Two chats driving
playwright at once run at about half speed each. A slow call delays the others,
bounded only by the existing wedge recycle.

### 5.5 Daemon capacity and reconnect

Branch `fix/mcp-gatewayd-capacity-reconnect`, tested heads `f5cf0eead96a` and
`885935618f7f` (§8 numbers come from these).

One cause behind most failures: the agent engine opens every server with an
era probe (`server/discover`), and the daemon forwarded it to the backend. A
server on the newer MCP library then locks its pipe to that era, so the
fallback `initialize` is refused with -32022, the spawn permit is held until the
30 s initialize deadline, and the proxy has no captured `initialize` to replay
after a daemon restart.

- The daemon answers `server/discover` itself with "method not found"
  (`daemon/connection.py`, `_is_era_probe`), so every session handshakes with
  `initialize`.
- The resident ceiling grows to the number of shared servers
  (`admission.derive_resident_ceiling`), so more than 64 shared servers keep
  their tools.
- On macOS and Windows, backend memory is read from the platform's own process
  walk instead of `/proc`, so `rss_kb` is no longer -1.
- `kirocrew stop` passes a 120 s bound to the Windows `taskkill` walk instead of
  5 s, so a large tree no longer prints "No permission".

### 5.6 Limit a server to some sessions (follow-up)

Not built. A follow-up pull request, by the same owner, on its own branch, adds
the one remaining control from owner rule 3: a server can be shared only with
named sessions (or session kinds), and is absent from the others. It needs its
own short design round for where the control lives on the MCP page.

## 6. Security considerations

This is a security-relevant change. Each code pull request touching the launch
path or the sandbox asks for a Sensitive Change review by a maintainer other than
its author.

**What the approval protected.** A shared backend's command comes from files
(`mcp.json` in the data home, `~/.kiro/agents`, the Kiro CLI settings file, the
resolved overlays, and crewmate files under `~/.kiro/mates/agents`). The
approval made a person look at that command before the daemon ran it outside
every sandbox.

**What replaces it.** Where an OS sandbox runs, every session Kiro Crew wraps
seals four of those sources read-only: `mcp.json` and `mcp/resolved` in the
data home (`sandbox_leaves.py`), `~/.kiro/agents` (`_resolved_kiro_agents_targets`
in `sandbox_tiers.py`) and the Kiro CLI settings file
(`_resolved_kiro_mcp_registry_targets` in `sandbox_seals.py`). So an agent
cannot write a launch there. **The crewmate folder `~/.kiro/mates/agents` is
not sealed**: `mate_agents_dir` in `config/paths.py` says only unsandboxed
gateway handlers write it, and the agent's file-edit tool is refused there
(`security/paths_lists.py`), but a spawned shell in a session can still write
it. Launches from that folder are shared anyway (decision E, §5.1); item 5
below states the accepted risk. The shared backend
then runs in the `standard` global profile. Only a session with no
confinement beyond the default attaches to it, and that is the profile its
per-chat copy already runs in, so sharing does not widen what the server can
reach. A session confined further keeps its servers per chat (item 4 below).

**The risk that remains, stated plainly.** An agent that can edit an MCP
declaration file can make the daemon run a program of its choice outside the
sandbox in cases 1 and 3; case 2 weakens a fence inside the sandbox, and case 5
lets any session get a program started by the daemon (inside the sandbox on
Linux and macOS, outside it on Windows):

1. **Windows.** There is no OS sandbox. Declaration files are writable by the
   agent, and a shared backend runs as the user with the daemon's environment.
   A per-chat server on Windows already runs unsandboxed as the agent's child,
   so the new part is that no person looks at the launch first.
2. **Shared playwright runs Chrome without Chrome's own sandbox** (decision
   B). The gateway adds `--no-sandbox` unless the declaration chose. A page
   that breaks out of a Chrome renderer then lands inside our sandbox, with
   the reach of any wrapped backend, instead of inside Chrome's tighter one.
   This needs no declaration edit. Declaration files are sealed read-only for
   sessions Kiro Crew wraps; a Kiro CLI session that delegates to Kiro CLI's
   own sandbox (`sandbox_delegation.py`) gets none of Kiro Crew's seals and is
   one of the paths in item 3.
3. **Anywhere the session sandbox is not in force:** `agent.sandbox: off`, a
   harness that runs outside the Kiro Crew sandbox, or a tool on an already
   unwrapped backend (for example a filesystem or shell server) that can write
   the declaration files on the agent's behalf.

A fourth case would widen reach without any edit, and decision A closes it; a
fifth is accepted by the maintainer:

4. **A session confined beyond `standard`.** A per-chat server is a child of
   its session's sandbox and inherits all of it. Three settings confine a
   session further, and a shared backend gets none of them:
   - `agent.sandbox: strict` hides `.aws`, `.ssh` (except `known_hosts`),
     `.kube`, `.config/gh` and the package-registry and git credential files;
   - a stricter per-harness tier in `agent.harness_sandbox` does the same for
     that harness's chats;
   - an enforced harness (the routings in `ENFORCED_ROUTINGS`,
     `agent_sdk/tool_gate.py`) hides the whole sensitive-home floor, including
     `.ssh` and `.aws`, from its process tree even at the default `auto` tier
     (`adapter_hidden_credential_dirs`). A harness that takes its MCP servers
     as a session array (`ACP_BACKENDS_SESSION_MCP_ARRAY`) receives the pooled
     proxy entries (`_pooled_broker_stubs` in `acp/client_mcp_array.py`).

   If such a chat attached to a shared backend, the backend would run at
   `standard` (§5.2) and could read those files. Under decision A it never
   attaches: its servers stay per chat, inside its own sandbox, so its reach
   is what it is today. The cost is that those chats keep one copy per chat.
5. **Crewmate files are writable from a session** (decision E, accepted by the
   maintainer). `~/.kiro/mates/agents` has no OS seal, so an agent that runs a
   shell in any session can edit a crewmate file or the crewmate conductor or
   worker spec and add a stdio server. Auto-share picks the edit up within
   15 s, and the shared daemon starts that server with no person looking at
   it: inside the `standard` sandbox on Linux and macOS, and unsandboxed on
   Windows (item 1). This holds for a session at `agent.sandbox: strict` too.
   Decision A keeps a strict session's own chats off shared backends, but it
   does not stop that session from writing the crewmate file, so a strict
   session's agent can get a program started at `standard`, a wider fence than
   its own. A reused server name gives the same route. The maintainer accepts
   this rather than add a seal on the folder.

Kiro Crew's own managed servers stay unwrapped, but their launch is derived by
code, not read from an editable file.

**Other accepted effects.**

- Under owner rule 1, a shared folder server can read any attached chat's
  folder, and a roots-aware server sees the folder names of every chat it has
  served, including chats that have since left.
- A chat's sandbox seals its own folder's project MCP files read-only
  (`project_mcp_files.py`). The shared wrap names no project folder, so a shared
  backend with a write tool can edit a project's `.mcp.json` or
  `.kiro/settings/mcp.json`. A changed project server still needs the owner's
  approval card (`dashboard/handlers/project_mcp_approve.py`) before it starts.
- A server whose env puts a `secret://` value under a name the sandbox launcher
  scrubs (keys starting `AWS_SECRET`, `AWS_SESSION`, `GNUPGHOME`,
  `GIT_ASKPASS`, and `SSH_AUTH_SOCK` unless the operator named it) does not
  start, shared or per chat. This fails closed; the daemon log names the keys and
  says to declare the secret under another env name.
- The launch store still records every launch, so what ran stays auditable even
  though nobody approved it.

**Superseded decisions.** This document's pull request adds the decision entry
`docs/decisions/2026-11-27-mcp-server-sharing-has-no-launch-approval.md`, which
cites the maintainer's restatement on the pull request. It supersedes
`docs/decisions/2026-11-23-the-owner-can-trust-a-shared-mcp-servers-changes.md`
and, in part, `2026-11-20-the-gateways-own-path-is-not-a-launch-change.md`,
`2026-11-26-an-edition-folded-version-folder-keeps-a-launch-approval.md` and,
for the crewmate folder only (decision E),
`2026-11-20-agents-ask-before-changing-gateway-trusted-files.md`.

## 7. Alternatives considered

| Alternative | Why not |
|---|---|
| Approve by authorship: a person's own add from the MCP page records the approval | Removes some clicks but not the pause after a package update or an edit outside the page. Overtaken by owner rule 3 |
| Trust any launch from a sealed file | Same uid means the file cannot say who wrote it; an unwrapped shared backend could still write it |
| Auto re-approve a moved toolchain path | An agent that can write a folder earlier on `PATH` plants its own `npx`; moot once backends are wrapped |
| "Trust changes" on by default | Every later edit runs unsandboxed with no look; worse than wrapping |
| Keep stateful servers per chat | Keeps the memory cost that motivates this work; serialization and tabs make them correct on one copy |
| Playwright's HTTP mode, one context per HTTP client | An unauthenticated loopback listener open to every local user, with a code-execution tool; and two parallel snapshots got the same file name |
| Leave browser servers unwrapped, chosen by launch text | Not chosen (decision B): any declaration could name a browser to leave the sandbox |
| Widen the seccomp filter so Chrome's own sandbox starts | Not chosen (decision B): it weakens the fence for every wrapped process |
| Wrap each shared backend at the strictest attached session's confinement | Not chosen (decision A): a new pool dimension, and a running backend's fence would change as sessions come and go |

## 8. Test evidence

Four real machines, each with an isolated data home, a real gateway and real
servers, on the KAS backend (the default) and on the Kiro CLI backend. Numbers
are copied from each machine's run report. "Not shown" means the raw output does
not show it. The tables below come from the earlier heads named in their
columns. All four machines then retested auto-share `467e7f360c9c`, folder-scope
`6891bfcc8ff7` and daemon `05369284137f`. Every failure left on those heads is
accepted by the maintainer or parked, and is listed in §10 (items 4, 12, 13
and 14; item 14, shared puppeteer, is decision C in §5.4, which the owner reversed on
2026-11-28 so the code now turns Chrome's own sandbox off for it). Each code pull request still carries its own results
for its final head.

### 8.1 Linux cloud VM (external servers)

| Check | main `dd886bb5bc9e` | folder-scope `7624c9298a58` | auto-share `643f8ecaa11c` |
|---|---|---|---|
| Fresh switch shares what it promises | FAIL: promised 7, shared 0 | FAIL (no auto-share on this branch) | PASS: 13 shared, no click |
| Share or folder dialogs | 62 | 15 | 1 ("Share time across chats?") |
| More than 64 shared keep tools | PASS this run (an earlier run failed at 64) | not run | PASS: 72 proxies, 5 of 5 copies OK |
| -32022 lines in the daemon log | 3 | not run | 1 |
| Shared backends inside the sandbox | 0 of 4 | 0 of 5 | 6 inside, 4 outside |
| playwright and puppeteer, per chat (KAS) | FAIL: Chrome does not start in the chat sandbox | not run | not run |
| playwright and puppeteer, shared | pass, but leak between chats | pass, no leak | FAIL: Chrome aborts inside the sandbox |
| memory, 25 + 25 parallel writes | not run | PASS: 50 | not run |
| Folder T1 to T3 (relative path, cross-folder, symlink) | not run | PASS | not run |
| New server, edited args, one server off | paused rows | paused rows | PASS: shared, no refusal, off works |
| Server removed from the config | not run | not run | FAIL: still callable after restart |
| 8 chats | 3.2 to 13.6 GB, 136 proxies | not run | 3.9 to 12.9 GB, 135 proxies |

Folder-scope head `7624c9298a58` regressed one case: a filesystem server started
with its own folder arguments loses them once shared, because server-filesystem
replaces its command-line folders with the roots the gateway now answers (open
item 2).

### 8.2 Linux hosted dev box (servers that sign in with a browser cookie the OS user holds)

| Check | main | daemon `f5cf0eead96a` | auto-share `643f8ecaa11c` |
|---|---|---|---|
| Chat reconnects after a daemon kill | FAIL / FAIL | PASS on KAS; FAIL on Kiro CLI (memory re-init -32022) | not run |
| 30 servers: all tools on the first prompt | FAIL 5 of 8 | PASS 8 of 8 on both | not run |
| 64 servers: every backend starts | FAIL 31 of 64 | 73 of 74 | not run |
| Shared backends inside the sandbox | 0 of 16 | not run | PASS 42 of 42 (KAS), 46 of 46 (Kiro CLI) |
| Sign-in servers still work inside the sandbox | PASS | not run | PASS 3 of 3 |
| Edited launch, package-update path change | refused | not run | no refusal, still shared |
| New servers shared with no click | 8 decisions for 4 | not run | 3 of 4 (the fourth: open item 5) |
| playwright inside the sandbox | n/a | n/a | FAIL: "No usable sandbox!" |

On this box the two-chat puppeteer case did not run on main or on any branch
head: server-puppeteer refused its launch options ("Set allowDangerous: true")
each time.

On folder-scope `c05422cb6358`, playwright tab isolation passed (chat 1 closing
its browser left chat 2's page; a popup stayed in its own chat), and git with no
`repo_path` resolved to each chat's folder. Turns with two or more chats were
slower than on main (for example 104 s against 43 s); the cause is not proven.

### 8.3 macOS (Apple silicon, macOS 26)

| Check | main `4ad65fea3147` | folder-scope `7624c9298a58` | daemon `f5cf0eead96a` | auto-share `643f8ecaa11c` |
|---|---|---|---|---|
| First-use clicks | 29 dialogs for 30 servers | 15 dialogs, 16 clicks | 15 dialogs, 16 clicks | 2 clicks, 1 dialog |
| Approval cards in chats | 0 | 0 | 0 | 0 |
| Shared backends in `sandbox-exec` | 0 of 33 | 0 of 18 | 0 of 18 | 14 of 18 (every user server) |
| -32022 | not shown | 0 | 0 | 0 |
| Reopen, restart, daemon kill | PASS | PASS | PASS | PASS |
| `rss_kb` in metrics | -1 | -1 | real values | -1 (no daemon fix on this head) |
| 70 user servers | not run | not run | 78 proxied, 0 refused, 6 of 6 probes | not run |
| playwright and puppeteer | PASS | PASS | PASS | FAIL: `forbidden-sandbox-reinit` |

This macOS browser failure, and the Linux one in §8.1 and §8.2, are what
decision B's added `--no-sandbox` for shared playwright answers; it is in
auto-share `467e7f360c9c`. On folder-scope `7624c9298a58`, T2 (a chat reading
another chat's folder) and T6 (roots after a chat closes) failed: roots dropped
a folder a still-open chat used. Folder-scope `b78afd0550dd`, with the
served-folder rule, passed T2 and T6 on macOS on both backends (open item 2).
Per proxy process: 33.4 to 33.7 MB resident, 23 to 25 MB footprint, on every head.

### 8.4 Windows Server 2022

| Check | main | folder-scope `7624c9298a58` | daemon `f5cf0eead96a` | auto-share `643f8ecaa11c` |
|---|---|---|---|---|
| Approval cards | 0 | 0 | 0 | 0 |
| Roots-only filesystem server starts when shared | FAIL | PASS | n/a | n/a |
| T1 to T3 (relative, case-folded path, junction) | not run | PASS on KAS and Kiro CLI | n/a | n/a |
| 23 unshared servers x 4 chats ready in 90 s | FAIL | n/a | FAIL (same as main) | FAIL (same as main) |
| `kirocrew stop` on a large tree | "No permission" | n/a | fixed in `885935618f7f` | n/a |
| Switch count vs real shared count | n/a | n/a | n/a | FAIL: counted 49 deleted servers |
| New server auto-shared, hand edit picked up | n/a | n/a | n/a | FAIL both |
| Turn one server off | n/a | n/a | n/a | PASS |

The three auto-share failures on Windows are what the stale-entry pass and the
live watcher in `e0e784ea821f` fix (§5.1); auto-share `467e7f360c9c`, which
carries them, was retested with the other heads (§8 intro).
puppeteer fails on this Windows Server box with sharing on, off and per chat, so
it is not a sharing result.

### 8.5 Local checks on the branch heads

Each branch head passed the repository gates (formatting, isort, flake8,
`mypy --platform linux`, comment history, brand name, file line cap, docs-lint,
flake ratchet) with exit code 0, its related tests, and the repository's two
local AI review lanes with PASS on that exact head (`e0e784ea821f`,
`885935618f7f`, `7624c9298a58`). Related test counts: 8,829 passed on the
auto-share round before `e0e784ea821f`, whose last change only deletes code;
7,179 passed on `885935618f7f`; 1,034 passed on folder-scope head
`c05422cb6358`.

## 9. Planned pull requests and merge order

| Order | Pull request | Branch | Depends on |
|---|---|---|---|
| 0 | This RFC | `docs/rfc-mcp-zero-approval-sharing` | - |
| 1 | Auto-share, no approval, global sandbox, live watcher (§5.1, §5.2) | `feat/mcp-auto-share-approve` | the RFC |
| 2 | Daemon capacity and reconnect (§5.5) | `fix/mcp-gatewayd-capacity-reconnect` | 1; it rebases onto 1's final head (both edit `gatewayd.py`) |
| 3 | Folder and roots scoping, stateful servers (§5.3, §5.4) | `feat/mcp-folder-scope` | 1; it rebases onto main after 1 and 2 |
| 4 | Limit a server to some sessions (§5.6) | not started | 1 |

The branches keep moving, so this table names no head; §8 names the heads each
number was measured on. Each code pull request opens only after its final head
passes the four-machine smoke, and each runs the repository's full pull request loop.

Exit criteria, testable on each machine:

- PR 1: a fresh install shares every stdio server with 0 clicks; an added server,
  an edited one and a moved install path stay shared with 0 dialogs; turning one
  server off survives a restart; on Linux and macOS every shared user backend has
  a sandbox launcher ancestor, browser servers included (decision B); shared
  playwright passes on both Linux machines and on macOS; a removed
  server's override is dropped; a server added to a crewmate file is shared
  like any other (decision E); a chat at `agent.sandbox: strict`, at a
  stricter `agent.harness_sandbox` tier, or on an enforced harness gets
  per-chat copies only and never a shared backend (decision A).
- PR 2: 64 shared servers all start; 30 servers keep every tool on the first
  prompt; 0 `-32022` lines; a chat's next call works after a daemon kill.
- PR 3: memory 25 + 25 parallel writes keep 50; one chat's playwright flow is
  not broken by another's (seeing another chat's pages is accepted, open item
  12); each chat reads its own relative `note.txt`; a filesystem server
  with its own folder arguments keeps them (on macOS judged on a folder outside
  `/tmp`, because of the upstream limit in §5.3); a fast call to a stateless server (filesystem,
  fetch) does not wait behind another chat's slow call.

## 10. Open items

1. **Chrome inside the sandbox.** Decided 2026-11-27 (§5.2, decision B): no
   browser exemption; shared playwright-mcp gets `--no-sandbox` unless its
   declaration chose; no seccomp change. It is in auto-share `467e7f360c9c`,
   which all four machines retested. Shared puppeteer gets Chrome's sandbox
   turned off too (decision C, reversed by the owner on 2026-11-28); not yet
   retested on the machines.
2. **Roots on a server with its own folders.** On `7624c9298a58` a
   server-filesystem launched with folder arguments loses them once the gateway
   answers roots; and on macOS roots dropped a folder an open chat still used.
   Fix before PR 3 opens: answer roots only to a server started with no folder
   argument, or include its own folders in the answer. The folder-scope branch
   now lists the launch folders first and keeps every served chat folder
   (§5.3). On the hosted dev box, head `9222a94f20a7` passed a shared
   server-filesystem with its own folder argument on both backends, and on
   macOS head `b78afd0550dd` passed T2 and T6 on both backends. A `/tmp`
   folder on macOS still fails, from the upstream limit in §5.3.
3. **`secret://` under a scrubbed env name** fails closed (§6). The ruling is to
   keep it closed and say so in the daemon log; a direct-exec exemption was tried
   and removed because it ran an unreviewed launch with no sandbox.
4. **A moved install path** passed the refusal check but the chat reported the
   tool missing on Linux and macOS on `643f8ecaa11c`, and the time server still
   fails after an install-path move on the retested heads. Known; parked for a
   daemon follow-up pull request.
5. **An opt-out may follow the launch, not the name.** On the dev box a new
   server whose launch was byte-identical to one switched off under another name
   stayed off. Cause not confirmed.
6. **Proxy process count.** One proxy process per chat per server stays (about
   21 MB private each on Linux, three processes per proxy on Windows). Fewer
   proxies is a rewriter change, out of scope here.
7. **Sharing switched off live** reports no restart needed, but new chats were
   still routed through the daemon until a restart (seen on Windows).
8. **Per-tool approval prompts** for custom agents stay as they are (§4).
   Decided 2026-11-27 by the maintainer: any change to them is separate,
   later work, outside this design.
9. **Sessions confined beyond `standard`** (§6 item 4: `strict`, a stricter
   `agent.harness_sandbox` tier, an enforced harness's credential mask).
   Decided 2026-11-27 (§5.2, decision A): those sessions keep their servers
   per chat and never attach to a `standard`-tier shared backend. PR 1 adds the
   check and a test for each of the three confinements.
10. **Roots for MCP 2026-07-28 servers.** §5.3's `roots/list` answering only
    reaches servers before 2026-07-28. For a 2026-07-28 server the gateway would
    answer the `input_required` roots request on the caller's own retry, which
    already belongs to one chat, and could pass a per-chat identity in each
    request's `_meta`. Which of these to build, and when, is open; follow-up 1
    (§13) is the detection half.
11. **How PR 3 recognizes a server with shared mutable state** (decision D).
    The signal must come from evidence, never a server name list: candidates
    are the tool shapes the gateway already reads (the browser tabs shape, a
    navigate tool with no tabs tool) and the hazard ledger. A memory store has
    no such shape today. The branch still carries the missing-`readOnlyHint`
    rule, which over-serializes (§5.4); PR 3 replaces it.
12. **Chats see each other's playwright pages** on the retested heads. The
    maintainer accepts it under owner rule 1: all chats on a host belong to one
    local user, and the bar is that no flow breaks.
13. **A server deleted while the gateway is down lingers** in the agent specs
    after the next start. The maintainer reports main behaves the same; no run
    in §8 checked main for it. Deferred to a follow-up pull request.
14. **Shared puppeteer in the sandbox.** Decision C reversed by the owner on
    2026-11-28: a wrapped puppeteer server gets `--no-sandbox` through
    `PUPPETEER_LAUNCH_OPTIONS` and `ALLOW_DANGEROUS=true` unless its declaration
    sets either. Per chat on main it still fails, as before.

## 11. Backward compatibility

- Existing installs: a stored `mcp_gateway.enabled: false` from an older build is
  adopted once as "on". A person who turns sharing off after the upgrade keeps it
  off.
- A per-server off stays off.
- The launch store keeps its format; old approvals stay as records.
- No setting is removed from `config.json`; the trust-changes option has no
  effect once every change is admitted.
- Undo: turn sharing off on the MCP page (global or per server). Each code pull
  request reverts on its own.

## 12. Prior art

A survey on 2026-11-27 read other MCP hosts and gateways from their source or
docs at the linked commit; none of them was run. Hosts with no primary statement
found are left out.

| Product | One process shared across sessions? | How it decides |
|---|---|---|
| [OpenAI Codex](https://github.com/openai/codex/blob/372947d9369a6c608dde98742a9130a3198a66fe/codex-rs/codex-mcp/src/connection_pool.rs) | Yes, on by default, for a server that negotiates MCP 2026-07-28 and asks no user input at startup; any other server stays per chat | Once, at startup, from the protocol version; an optional per-chat lease id in `_meta` ([#57978](https://github.com/openai/codex/pull/57978)) |
| [ToolHive](https://github.com/stacklok/toolhive/blob/a7c83e286d9293e965744632062aaa5d1f5e9992/docs/arch/03-transport-architecture.md) | Yes, always | Rewrites ids and progress tokens per session; drops notifications it cannot attribute; answers server-sent requests with `-32601` |
| [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy/blob/b3dc6de1d461cd034e0af9fd9edcb5c21c3dfb3b/src/mcp_proxy/mcp_server.py) | Yes, always | No checks; exits if the server dies |
| [IBM ContextForge](https://github.com/IBM/mcp-context-forge/blob/612778b87cefc96583c4af9375bca0ce436a1877/docs/docs/manage/tuning.md) | Pool off by default; within one identity when on | The operator decides |
| [MetaMCP](https://github.com/metatool-ai/metamcp/blob/05818ec2767b946f98d280ae9cefa401f0362d62/apps/backend/src/lib/metamcp/mcp-server-pool.ts) | One per session, but reuses a live one once a cap is hit | A connection cap, with no warning |
| [mcp-local-hub](https://github.com/applicate2628/mcp-local-hub/blob/b74b15b7d85516cc75779b09484c0f7f24908f15/docs/architecture-highlights.md) | Yes, for servers its manifest marks global | A curated per-server list |
| [Docker MCP Gateway](https://github.com/docker/mcp-gateway/blob/4b5180c5ba8e1b456c53b36b7f90f17964131a16/pkg/gateway/clientpool.go) | No: a container per call, or per session with `--long-lived` | n/a |
| [supergateway](https://github.com/supercorp-ai/supergateway/blob/ffdf9b1d63a8e6086ed919da7039207b3f0ed3f5/README.md), [agentgateway](https://github.com/agentgateway/agentgateway/blob/134cbf02e02742cce08ad52cdd221fef579186b8/crates/agentgateway/src/mcp/upstream/mod.rs), [mcp-remote](https://github.com/geelen/mcp-remote/blob/c1d76e4293b538697a15ce79497038ec91099847/README.md) | No, one per connection or client | n/a |
| [Claude Code](https://github.com/anthropics/claude-code/issues/99831) | No, each session starts its own copies | n/a |

The protocol gives no "safe to share" field in either revision. MCP 2026-07-28
removes protocol sessions and asks servers to keep cross-call state in explicit
handles ([SEP-2567](https://modelcontextprotocol.io/seps/2567-sessionless-mcp)),
which is why Codex uses that revision as its sharing gate.

What this design does that none of the surveyed products does:

1. It decides sharing from observed traffic, keeps a hazard ledger keyed by
   launch identity, and moves a running shared backend back to per-chat copies
   when a hazard appears. The others decide once, always share, or leave it to
   a cap or an operator.
2. It serializes calls only for servers that hold shared mutable state, and
   lets stateless servers run concurrently (§5.4, decision D).
3. It gives each chat its own browser tab on one shared browser server, chosen
   by tool shape. The only per-client browser isolation found is inside
   Playwright MCP's own HTTP mode.
4. It maps one shared process onto many project folders: relative arguments per
   caller and a `roots/list` answer naming every chat folder the backend has
   served. Others
   use one fixed folder per process or per hub.

Nobody, this design included, detects hidden process-global state that never
shows on the wire.

## 13. Follow-ups

Low-cost changes taken from the survey, each its own later pull request.

1. **Watch `input_required` results, not only `roots/list` requests.** Closes
   the gap that an MCP 2026-07-28 server asking for roots, sampling or
   elicitation is never detected, so it is never kept per chat or scoped
   (open item 10). Small: the retry already belongs to one caller.
2. **Treat `cacheScope: "private"` as a reason not to share across
   credentials.** Closes the gap that a server whose list results vary per
   authorization context could serve one chat's data under another chat's
   credentials. Low: it reads one field
   ([schema](https://modelcontextprotocol.io/specification/2026-07-28/schema)).
3. **Keep a server per chat when it asks the user anything at startup.** Closes
   the gap that input given at startup, such as a typed token, binds the process
   to one chat's user and would then serve every chat. Low: one more hazard code,
   recorded like `roots_requested`.
4. **Answer an unroutable server request with `-32601` while the per-chat copy
   starts.** Closes the gap that the old shared process waits on a request no
   chat will answer during a hazard retreat. Low: one error frame back to the
   backend; the retreat itself is unchanged.
