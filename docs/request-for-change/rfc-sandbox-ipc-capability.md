---
title: Sandboxed callers reach the gateway through a connection-bound capability
status: draft
author: bolichen97
created: 2026-11-05
last-audited: 2026-11-05
audited-at: ccf5db72ff
doc-pr:
implementation-prs: []
tracking-issues: [9175, 11988, 13862]
supersedes: []
superseded-by: []
---

# RFC: Sandboxed callers reach the gateway through a connection-bound capability

- Status: draft. Phase 1 is in flight before acceptance, on the repository
  owner's request (section 10); nothing else is built. It proposes how to take the
  gateway's privileged local IPC out of the agent sandbox's reach without
  breaking the Kiro Crew MCP servers that run inside that sandbox and call the
  gateway on every tool call. Claims about main were checked at `ccf5db72ff`.
- Author: bolichen97

## 1. Problem

A Kiro Crew MCP server such as `kirocrew-core` is launched by kiro-cli, so it
runs inside the agent sandbox, under the same uid and the same profile as the
agent's own shell. It calls the gateway's internal API on every tool call. To
do that it needs two things the shell can also reach today.

| What | Where it lives on main | Who in the sandbox can read or reach it |
|---|---|---|
| The gateway credential | One value from `os.urandom(16)` in `dashboard/server.py`, written to `run/gateway-<port>.secret` and `.local_secret` (`_write_instance_credentials` in `dashboard/server_runtime/listener_claims.py`) | Both leaves are in `_CREW_SANDBOX_VISIBLE_LEAVES` (`sandbox_leaves.py`). Every sandboxed process can `open()` them |
| The dashboard unix socket | `config_dir()/dashboard-<port>.sock` (`dashboard_socket_path` in `dashboard/urls.py`), bound by `web.UnixSite` in `dashboard/server_runtime/listener.py` | The data-home root is not masked, and neither renderer has a socket rule, so any sandboxed process can `connect()` |
| Loopback TCP | The same app on `127.0.0.1:<port>` | Neither sandbox enters a network namespace, so every sandboxed process can dial it |

The one credential is two authorities. `X-Internal-Secret` admits the internal
routes (`internal_path_matches` in `dashboard/token_auth.py`), and
`X-Local-Secret` on `/api/token/local` mints an owner dashboard token
(`dashboard/handlers/core_local_token.py`). Both compare against the same
`app["local_secret"]`. Issue #9175 records the consequence: a sandboxed shell
that reads either file can mint an owner token.

Session identity is weaker over TCP than over the socket. On the socket,
`_verify_unix_peer` (`dashboard/token_auth_callers.py`) checks the declared
`X-Session-Key` against the peer's kernel credentials and process ancestry. On
TCP the gateway has no kernel evidence. The declared key is backed at most by
the per-session token (`KIROCREW_STUB_SESSION_TOKEN`), and on a one-session
runtime that token is written into the kiro-cli process environment
(`_apply_session_identity_env` in `acp/client_spawn_env.py`), which the
agent's shell inherits.

`loopback_urlopen` (`loopback_http.py`) prefers the socket and retries over TCP
only on `FileNotFoundError` or `ConnectionRefusedError`. A Seatbelt deny raises
`PermissionError`, which is not in that set, so the call fails. That is why the
obvious fix, denying the dashboard socket in the sandbox profile, would break
every in-sandbox Kiro Crew tool, and why this document does not propose it.

Issue #9175 states the general constraint: nothing inside the sandbox can hold
a secret the agent cannot read. This design accepts that and changes what a
credential is: authority comes from the connection a request arrives on, not
from a value the caller holds.

## 2. Goals and constraints

1. An agent shell in the sandbox cannot mint an owner token, cannot reach a
   socket that grants authority by connection alone, and cannot call the
   internal API as a session other than its own.
2. Every Kiro Crew MCP server inside the sandbox keeps working, on every
   upgrade step, with no user action. The one-line test: a person who never
   reads this document notices nothing.
3. Callers outside the sandbox (the CLI, the desktop app, the person's own
   terminal) keep working unchanged.
4. Nothing new is asked of Windows. It has no OS sandbox
   (`rfc-sandbox-gateway-checkout-git-seal.md` section 5), so the change is
   POSIX-only and Windows keeps today's flow.

Not a goal: confining the MCP tool surface itself. A sandboxed agent may still
call its own Kiro Crew tools; whether a direct call must also prove a
PreToolUse approval is issue #10429.

## 3. Decision

### 3.1 Split the credential (closes the owner-token path)

Mint two values, as #9175 proposes:

- the **owner secret**, for `X-Local-Secret` (`/api/token/local`,
  `/api/logout`, `/api/shutdown`), written to `.local_secret` (as today, only
  when no other gateway shares the data home) and to a per-port copy
  `ipc/owner-<port>.secret` in the masked leaf of section 3.2;
- the **internal secret**, for `X-Internal-Secret`, written where the
  per-listener files go today (`run/gateway-<port>.secret` and the
  address-keyed `run/gateway-<port>-<address>.secret`).

`.local_secret` then moves from `_CREW_SANDBOX_VISIBLE_LEAVES` to
`_CREW_HIDDEN_LEAVES`. Its file name and mode do not change, so the desktop
app needs no edit. The CLI's owner-secret senders (`cli_server_requests.py`,
`cli_server_stop.py`, `cli_server_restart.py`, `cli_server_update.py`,
`app_lifecycle_client.py`, `memory_forget_client.py`) read the owner secret
through a new `read_owner_secret(port)`, per port first and then
`.local_secret`, instead of `read_local_secret`, whose per-port file will hold
only the internal secret.

The in-sandbox MCP readers #11988 names (cron triggering, screencast, the Sage
review driver) send `X-Internal-Secret` only, so they keep working. What the
sandbox loses is owner authority, by design: section 3.5.

### 3.2 A masked directory for privileged listeners

Add one data-home leaf, `ipc/`, created `0700` by the gateway and masked in
every sandbox mode:

- Linux: an entry in `_CREW_HIDDEN_LEAVES`, so the namespace launcher
  bind-masks it and `connect()` inside it fails with `ENOENT`.
- macOS: the same hidden entry, plus a
  `(deny network-outbound (remote unix-socket (path-regex ...)))` rule for the
  subpath, because Seatbelt checks `connect()` to a unix socket as a
  `network-outbound` operation, not as a file read. The rule shape is the one the socket-deny change for `/tmp/mcs-*`
  sockets adds to `render_seatbelt_profile`.

Into `ipc/` go:

- the dashboard socket, as `ipc/dashboard-<port>.sock`;
- any socket that grants authority by connection alone. An edition's
  remote-access sidecar is the case that motivates this rule: such a socket
  must bind under `ipc/` and never under `run/` or the data-home root;
- the internal secret's file, once phase 4 lands (section 3.6).

`run/` itself cannot be masked: it holds the namespace launcher
(`namespace_launcher_script_dir` in `sandbox_argv.py`), which the sandboxed
child must exec. That is why this is a new leaf rather than a mask on `run/`.

### 3.3 The agent socket and its capability

The in-sandbox MCP servers get their own listener, outside `ipc/`:
`agent-ipc/agent-<port>.sock`. It serves only the internal routes, and each
request on it must carry a **capability** that the gateway checks against the
connection it arrived on.

**Issue.** For each ACP session and each Kiro Crew server entry the gateway
injects into that session's `mcpServers`, the gateway mints a one-time
**pickup** (32 random bytes). The pickup rides the server entry's own `env`
element, the per-element channel `AcpRuntime._own_stub_session` already uses.
It never goes into the kiro-cli process environment and is never written to
disk, so `printenv` in the agent's shell does not show it.

**Redeem.** At startup, before it reads any request, the server:

1. on Linux, calls `prctl(PR_SET_DUMPABLE, 0)`, which makes its
   `/proc/<pid>/environ` and `/proc/<pid>/mem` unreadable to other same-uid
   processes and refuses `ptrace` attach;
2. reads the pickup and deletes it from `os.environ`, so its own children do
   not inherit it;
3. sends `POST /api/ipc/redeem` over the agent socket.

The gateway accepts the redemption when the pickup is unspent, the peer's uid
matches (`check_peer_is_self` in `mcp_gateway/socketsec.py`), and the
ancestry of the peer's kernel pid (`get_peer_pid`) reaches the session's
runtime pid, the walk `_verify_unix_peer` already does. The agent's shell also
descends from that runtime, so the ancestry check only narrows the caller to
this session. What separates the server from the shell is that only the
server was handed the pickup, and the server spends it at startup, normally
before the session's first turn can run a shell command (open question 5).
It answers with a **capability**: 32 random bytes bound to
`(session key, server name, peer pid, peer process start token)`, held only in
the server's memory. The pickup is then spent.

**Use.** Every later request on the agent socket carries the capability. The
gateway reads the peer pid of that connection and admits the request only when
pid and start token match the binding. The session the request acts for is
the one in the binding: a declared `X-Session-Key` that names another session
is refused `403`. A capability copied to another process is useless, because
that process is a different peer.

**Gateway-spawned children.** A child the gateway starts itself, such as a
script cron (`cron_script.run_script_sandboxed`), does not need a pickup. The
gateway hands it a connected `socketpair` end through `pass_fds`, bound to the
cron job's session on the gateway side. It replaces the 0600 temp file that
`cron_script_child_env._child_internal_secret` writes today.

**Restarts.** kiro-cli can restart a crashed MCP server with the same entry,
so the same pickup. A re-redemption is accepted only when the pid the previous
capability was bound to is gone (checked by start token), and each one is
recorded to the SEL. A second redemption while the first holder is alive is
refused and recorded, and the legitimate server then reports the failure as a
tool error.

### 3.4 No header-on-faith fallback for a capability

A capability-bearing request goes out through `unix_socket_urlopen`, which has
no TCP handler at all. That makes the "no fallback" rule structural, as
`loopback_http.py` already argues for credential-bearing sends: no flag added
to `loopback_urlopen` can switch it back on. A dead gateway fails the call, as
a dead gateway fails a TCP call today.

Once the last phase removes the internal secret from the sandbox (section 4,
phase 4), a TCP request carrying `X-Internal-Secret` comes from a caller
outside the sandbox, which is the case TCP is kept for: the CLI, Windows, and
an older gateway. The socket paths a sandboxed MCP server dials are never
denied, so the `PermissionError` case from section 1 does not arise. A test
pins that the agent socket matches no deny rule in either renderer.

### 3.5 What agents lose

The person at the machine loses nothing: the desktop app, the CLI in their own
terminal and the dashboard keep every path they have. An agent loses the
commands that work today only because its shell can read the owner secret.
Run from the agent's shell, these stop working after phase 2:

- `kirocrew app` lifecycle commands (`app_lifecycle_client.py`) and
  `kirocrew memory forget` (`memory_forget_client.py`), which mint an owner
  token through `/api/token/local`;
- the gateway control commands that send `X-Local-Secret`
  (`cli_server_requests.py`, `cli_server_stop.py`, `cli_server_restart.py`,
  `cli_server_update.py`). The agent safety policy already refuses most of
  these from an agent shell, so this mainly closes the path around that
  policy.

Each one fails the way it fails today when no secret can be read. Whether any of them deserves an agent route of its own (an internal
route under the capability, with an approval card) is open question 6.

### 3.6 What happens to `.local_secret`

`.local_secret` stops being sandbox-readable in phase 2 (section 3.1). The
per-port internal secret stays sandbox-readable until phase 4, so today's
in-sandbox readers keep a working fallback while the capability path is
proven. In phase 4 the gateway stops writing `run/gateway-<port>.secret` and
writes the internal secret to `ipc/internal-<port>.secret` instead.
`read_local_secret` reads the new path first, so the CLI is unaffected.

## 4. Migration

Each phase is its own pull request, and each one leaves every normal user's
gateway IPC working.

| Phase | Change | Why no user loses IPC |
|---|---|---|
| 0 | Record the transport (`unix`, `agent`, `tcp`), the caller (`X-Internal-Caller`) and whether a capability was shown on every internal-auth SEL grant | Logging only |
| 1 | Add the agent socket, `/api/ipc/redeem`, pickup issue and capability check. Kiro Crew's in-sandbox servers redeem and prefer the agent socket, and fall back to today's path when there is no pickup or the route answers `404` | Additive. An older gateway or a missing pickup takes today's path |
| 2 | Create the masked `ipc/` leaf (3.2), split the credential (3.1) and hide `.local_secret` | The desktop app reads the same `.local_secret`, and the CLI's owner-secret senders move to `read_owner_secret` in the same pull request. Every in-sandbox MCP reader sends the internal secret already. Agent shells lose owner authority (3.5) |
| 3 | Move the dashboard socket and every authority-by-connection socket into `ipc/` (3.2). For one release the gateway also binds the old dashboard socket path, which still needs the internal secret | An in-sandbox server dials the agent socket since phase 1. The CLI resolves the new path through `dashboard_socket_path` |
| 4 | Stop publishing the internal secret where the sandbox can read it (3.6). Enter this phase only when phase 0 shows no sandboxed secret-only grant from a Kiro Crew component on nightly for 14 days | By then every Kiro Crew in-sandbox caller authenticates by capability, and the telemetry gate shows it |

Rollback of any phase is a revert. No phase stores data that a revert would
leave behind: pickups and capabilities live in memory, and the files the later
phases move are rewritten on every gateway start.

## 5. Per-OS behaviour

| OS | Owner secret | Privileged sockets | In-sandbox MCP auth |
|---|---|---|---|
| Linux (namespace sandbox) | Hidden by bind mask | `ipc/` bind-masked, `connect()` gets `ENOENT` | Capability on the agent socket, `SO_PEERCRED` pid, `PR_SET_DUMPABLE` |
| macOS (Seatbelt) | Hidden by file deny | `ipc/` file deny plus socket deny | Capability on the agent socket, `LOCAL_PEERPID` |
| Windows | Unchanged | Unchanged (no `AF_UNIX` use) | Unchanged (TCP plus internal secret) |

## 6. Known limits

1. **macOS has no `PR_SET_DUMPABLE`.** A same-uid process can read another's
   startup environment (`KERN_PROCARGS2`, which is what `ps eww` reads), so a
   sandboxed shell that reads a server's environment before the server
   redeems gets the pickup. It still cannot use the server's capability. What
   it can do is redeem first, and then the legitimate server's redemption
   fails loudly. Phase 1 measures whether Seatbelt's `process-info*`
   operations can deny this read inside the profile (open question 1).
2. **A stolen redemption equals the agent's own tool surface.** The agent
   already holds the internal routes through its MCP tools, so a capability
   it gets by winning the race grants nothing new. Calling those routes
   without kiro-cli's approval prompt is issue #10429.
3. **The SEL trust root stays visible.** `sel_hmac.key` and `trust/` remain in
   `_CREW_SANDBOX_VISIBLE_LEAVES`, so a sandboxed shell can still forge a
   session-token mapping. That is the residual `AGENTS.md` and #11988 item 1
   name, and it is out of scope here. The capability does not depend on that
   key, so the residual no longer reaches the routes the agent socket guards.

## 7. Alternatives considered

1. **Deny the dashboard socket in the sandbox profile.** Rejected: the deny
   raises `PermissionError`, `loopback_urlopen` does not fall back on it, and
   every in-sandbox Kiro Crew tool would fail (section 1).
2. **Widen the fallback to `PermissionError`.** Rejected: the fallback goes to
   TCP with the same readable secret, so it closes nothing and hides the
   denial.
3. **Hand the credential through the environment.** Rejected: the kiro-cli
   environment is the agent shell's environment (`client_spawn_env.py`), and
   a per-element value read at any time is still a bearer value. The pickup
   uses the per-element channel only to start a redemption that binds
   authority to a pid.
4. **Route every Kiro Crew server through gatewayd stubs**, so the privileged
   half runs as an unsandboxed pooled backend (`mcp_gateway/pool.py` notes
   that pooled backends are not sandboxed). Kept as a compatible later step,
   not the decision: `mcp_gateway.stub_servers` is empty by default, the stub
   topology is a per-server operator choice, and the stub still has to prove
   its session to gatewayd, which is the same problem moved one hop.
5. **Split the credential only (#9175 alone).** Taken as phase 2, but not
   enough alone. The internal secret stays a bearer value any sandboxed
   process can present for any session over TCP.

## 8. Implementation sketch

| Phase | Files | Tests |
|---|---|---|
| 0 | `dashboard/token_auth.py` (the transport and capability fields on `log_api_access`) | A grant over each transport records its transport |
| 1 | New `dashboard/agent_ipc.py` (pickup store, `/api/ipc/redeem`, capability middleware on a second `web.UnixSite` in `server_runtime/listener.py`), `acp/client_spawn_env.py` and the mirror projections (pickup into the per-element `env`), `mcp_core_parts/gateway.py` (redeem at startup, prefer the agent socket, send through `unix_socket_urlopen`), `cron_script.py` (`pass_fds` socketpair) | Redeem accepts the runtime's child and refuses a sibling pid, a spent pickup, and a live previous holder. A capability sent from another pid is refused. A declared `X-Session-Key` that disagrees with the binding is refused. The pickup is absent from the kiro-cli process environment. The agent socket matches no deny rule in either renderer |
| 2 | `dashboard/server.py` and `server_runtime/api_entrypoint.py` (two values), `server_runtime/listener_claims.py` (owner copy under `ipc/`), `config/loader_locations.py` (`read_owner_secret`), the six owner-secret senders named in 3.1, `sandbox_leaves.py` (`ipc` and `.local_secret` to hidden) | `/api/token/local`, `/api/logout` and `/api/shutdown` refuse the internal secret, and internal routes refuse the owner secret. `test_sandbox_governance_mask` gains the leaves on both sides |
| 3 | `dashboard/urls.py` (`dashboard_socket_path` under `ipc/`), `sandbox_leaves.py`, `sandbox_plan.py` and `sandbox_seatbelt.py` (the `ipc/` socket deny) | The rendered profile denies `ipc/` and not `agent-ipc/`. The Linux plan masks `ipc/` |
| 4 | `server_runtime/listener_claims.py` (internal secret to `ipc/`), `config/loader_locations.py` | No sandbox-visible leaf holds either secret |

## 9. Open questions

1. Can the macOS profile deny reading another process's arguments and
   environment (`process-info*` against non-self targets) without breaking
   `ps`, `pgrep` or a build tool the agent runs? Phase 1 measures it. If it
   can, known limit 1 closes on macOS too.
2. Phase 4's gate uses telemetry. Is "14 days of nightly with no sandboxed
   secret-only grant from a Kiro Crew component" the right bar, and does a
   user-written script cron that calls the API with the internal secret need
   a migration notice before phase 4?
3. Should phase 3 keep the old dashboard socket path for one release, as
   proposed, or move it at once? The only callers of the old path are
   out-of-sandbox processes started before the upgrade.
4. Issue #13862 moves the desktop app's owner-token mint onto the unix socket.
   After phase 3 that socket is `ipc/dashboard-<port>.sock`. Should that issue
   wait for phase 3 so the app learns one path, not two?
5. Does kiro-cli always finish starting a session's MCP servers before the
   session's first turn, or can a server start lazily, or restart, while a
   turn is running? If it can, the pickup race in known limit 1 also applies
   on Linux for the window between `exec` and `PR_SET_DUMPABLE`. Phase 1
   measures it, and the SEL record of every redemption makes a lost race
   visible either way.
6. Should any owner-gated command an agent can run today (section 3.5) get
   an agent route of its own, an internal route under the capability that
   raises an approval card, or is losing them the intended outcome?

## 10. Amendment 2026-11-05: phase 1 as built

Phase 1 started before this document was accepted: the repository owner
(buluoray) asked for it on 2026-11-05 to fix #11988, where an enforced
harness's `kirocrew-core` holds no internal secret at all, so every
gateway-backed tool refuses `internal_secret_missing`. The code forced three
changes from sections 3.3 and 4, recorded here rather than by editing them.

1. **Phase 1 lands in two pull requests.** 1a is the gateway half:
   `dashboard/agent_ipc.py` (the store, the gate and the listener),
   `token_auth` routing a capability onto its ordinary internal-grant arm, and
   the `agent-ipc` leaf. It issues no pickup, so nothing changes for any
   session. 1b is the client half: the pickup on the element, redemption at
   server startup, and sending through `unix_socket_urlopen`.
2. **1b issues pickups to enforced harnesses first**, the four whose
   `kirocrew-core` has no working path today (`tool_gate.is_enforced`). A
   kiro-cli, KAS or Claude session keeps today's path byte for byte until a
   later step extends issuance, which is where the RFC's original phase 1
   scope resumes. The cron `socketpair` of section 3.3 is not in phase 1.
3. **A capability is bound to the session's stub token, not to its key.** A
   warm-pool runtime starts its servers before a session claims it, and
   `rekey()` re-binds the same stub token to the claiming session
   (`acp/client_class.py`). A key captured at redemption would name nobody,
   or a previous owner. So the store binds `(stub token, server name, peer pid,
   peer start token)`, and every request is admitted only for the session
   `verify_session_token` says that token names at that moment. Under the
   mask the server cannot read the signed token mapping, so it declares the
   `KIROCREW_SESSION_KEY` on its own element; a declaration that disagrees
   with the token is refused `ipc_session_mismatch` rather than admitted for
   either session. The redemption ancestry check reads the runtime's root pid
   live from the client for the same reason.
4. **Where 1b lives.** The pickup is put on the array in
   `acp/ipc_pickup.py`, called from both array builders: the runtime's
   mirrored projection (`_mirrored_session_mcp`) for the shared-runtime
   backends, and `AcpClient._resolve_session_mcp_servers` for the
   client-driven ones (opencode, goose), each with its session's stub token
   and its own root pid. The managed-tool-policy read goes over the
   capability too, so an operator exclusion still applies to a call the
   capability admits. The
   element also carries the gateway's pid, and the server refuses to write to
   an agent socket whose kernel peer pid is not it: the socket file sits in a
   directory a sandboxed process can write, so without that check a process
   that rebinds the path would receive pickups and requests.
   The server side is `agent_ipc_client.py`, a top-level module rather than a
   `mcp_core_parts` owner, so the `mcp_core` facade serves none of its names;
   `mcp_core_parts.gateway._api_urlopen` routes through it, which covers
   `_send` and the browser tool's direct call alike.

The pickup's lifetime is the issuing client's, through a weak reference: when
the client is collected, its pickups and every capability redeemed from them
are dropped.

The agent socket also answers `GET /api/ipc/session` to a capability holder:
the session its bound stub token names at that moment. A server under the mask
cannot verify the signed token mapping (the SEL trust root is masked), and a
warm-pool runtime is re-keyed after its servers start, so without this answer
it could not name the session its requests must declare.
