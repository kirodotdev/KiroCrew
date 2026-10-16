# LLM Provider Abstraction

Kiro Crew drives every LLM through one seam: the `LLMProvider` ABC in
`providers/base.py`. `AcpProvider` (`providers/acp.py`) is the only implementation
the factory selects; `AcpSessionProvider` (`acp/session_provider.py`) is a second
concrete subclass, the adapter a shared-runtime session is swapped onto once
`AcpRuntime` is up.
`agent.provider` is fixed to `"acp"` (enum `["acp"]`) — the provider is not the
harness selector. **Which harness that one provider drives is a separate
decision, taken from `agent_sdk/backends.py`**: `agent.acp_backend` names a backend id
and `BASELINE_SELECTABLE_BACKENDS` decides which ids an operator may choose.
Several are selectable on a plain public build, so "one provider" never meant
"one backend".

## Architecture

Member execution carries one immutable member/store selection into provider
allocation. `member_context` controls native instruction deduplication; it is not
a security capability and does not choose a database. Memory access uses the
Gateway's captured execution record and ordinary authenticated transport.

Retention mode is established before provider startup. A restricted session is
one whose `memory_mode` is `incognito` or `temporary` (any non-persistent mode;
[session.md](session.md) owns the term). Restricted sessions bypass
resumable warm providers, suppress Crew raw frame recording and discard known
native transcript files on teardown, including a declined late startup. A shared
runtime suppresses recording once it receives a restricted session. Provider
stderr and reader-exception text are also excluded from restricted diagnostics;
exit codes and the authentication-failure signal remain available. Provider
engines can still write their own files during a live process; teardown cleanup
cannot guarantee removal after a crash or for undocumented engine storage.

The ordinary host sandbox and backend capability checks apply independently.
Member memory requires no extra OS filesystem view, HMAC capability or platform
isolation admission. See [the memory contract](memory-skills-hooks.md).

```
┌─────────────────────────────────────────────┐
│  Consumers (handler, gateway, cli, session) │
│  Use LLMProvider interface only             │
└──────────────────┬──────────────────────────┘
                   │
         ┌─────────┴─────────┐
         │   LLMProvider ABC  │
         │   providers/base   │
         └─────────┬─────────┘
                   │
            ┌──────┴──────┐
            │ AcpProvider │
            │ acp.py      │
            └──────┬──────┘
                   │  backend id from agent_sdk/backends.py
                   └─ harness selected from the live backend registry
```

`agent_sdk/backends.py` is the selection authority: it defines the ids, the membership
floor (`ACP_BACKENDS_KNOWN`), the selectable baseline, and every capability set a
backend opts into. Do not re-describe that seam here —
[harness-parity.md](harness-parity.md) holds the invariants that keep the Kiro
path from being widened for an adapted harness,
[harness-onboarding.md](harness-onboarding.md) the sequence a new harness walks,
and [agent-host-contract.md](agent-host-contract.md) the host obligations every
adapted backend meets, KAS included, as a worked example. The transport itself (framing, timeouts, binary resolution,
config isolation) is [acp-client.md](acp-client.md); this file owns the
*interface*.

**One provider, by invariant:** `AcpProvider` is the only provider the factory
builds, and `agent.provider` stays `enum=["acp"]`. There is no Bedrock or
standalone provider and no multi-provider dispatch factory, because a second
`agent.provider` value would route around every harness-parity invariant.

The Claude Code harness has its own page: [claude-code-provider.md](claude-code-provider.md).


## Session Environments contract

Session Environments is a developer feature for choosing where a session's
runtime and development tools execute. Its shared helpers live in
`session_environment.py` (the start-stage record, the remote-workspace record,
the local-only tool fence and `SessionEnvironmentUnavailable`), separately from
the LLM provider and harness selector, so execution placement does not add an
`agent.provider` value or select a model. Gateway hosting, whole-Crew
deployment, and application deployment are outside this contract. Coder is the
workspace provider; Kiro Cloud Sessions (below) is the one provider that moves
the agent itself.

Coder configuration lives in Settings > Session Environments, revealed by the
per-device toggle under Settings > Developer > Feature Previews. The Settings
navigation updates immediately when the flag changes, and the configuration deep
link and search results are unavailable while it is off. The panel contains a
Coder URL and named profiles persisted through the existing config API. Setup
instructions explain deployment prerequisites, CLI sign-in on the gateway
machine, and provider-owned storage behavior. Credentials are never collected in
this form. Save
writes the complete section atomically, while Discard restores the latest loaded
settings. Edits stay local until Save, survive background refetches, and remain
editable after a rejected write. Leaving the Settings
panel discards unsaved edits. Adding, editing or removing a saved profile is
configuration management only. Removal never stops or deletes an external resource.
An older gateway without this config section
shows an unsupported notice rather than a form that cannot save. Saving starts
no compute. Sessions pick a
profile in the composer, and the card's readiness check and workspace list are
read-only. Turning the preview off hides configuration without deleting
profiles or changing existing sessions.

A session environment is a disposable worker, never a second crew. The gateway
keeps every stateful surface: the conversation and transcript, memory, lessons
and skills, approvals, the tool gate and the audit log. The agent's engine also stays
on the gateway; the remote side holds only the workspace (files, build tools,
processes) and a tool server that operates on it, so every Crew tool, memory
write, hook and approval runs exactly as for a local session and what a session
learns lands in the gateway's one memory store. Losing a worker loses at most uncommitted workspace changes. This is
why the whole-Crew Fargate lane (`cloud/fargate_engine.py`, Deploy my crew) is
not a session host even though it already runs ECS tasks: that task runs a full
headless backend with its own data home, model credential and approvals, and
exposes only a turn API, so memory written there never reaches the gateway.

No model credential ever reaches a session environment. The engine runs on
the gateway under its normal sign-in, and the guest runs
`remote_session/workspace_tools.py`, a stdlib-only MCP server whose tools
(`read_file`, `write_file`, `edit_file`, `list_directory`, `glob`, `grep`,
`run_command`) act on the profile working directory. The worker starts it as its
child, so the guest needs Python 3 and nothing else, and the gateway reaches it
through the `wire` stream. Every path is resolved with symlinks and refused when
it leaves the root, results are capped (256 KB per read, 64 KB per command
output, keeping the tail), and `run_command` is an unrestricted `/bin/sh` in the
root with a timeout of at most 30 minutes and credential variables withheld. That
is deliberate: the workspace is disposable, and every call is an MCP tool call
that goes through Crew's normal permission flow. The fence that matters is on
the gateway: the engine's built-in file and shell tools would act on the gateway
machine, so a remote session's agent definitions have every built-in UNMOUNTED
(`AcpRuntime._remote_workspace_agents` keeps only `@server` grants, expands `*` to
the servers actually sent, and adds `@workspace`), which requires a host whose
agent tool allowlist travels over the wire. KAS is that host, so a remote
session on any other backend is refused before any workspace is created or
started (`check_remote_workspace_supported`, a positive identity check). The
agent prompt also states the remote working directory. Some Crew tools act on
the gateway machine too, so they are held to the same rule. The
`kirocrew-core` tools in `session_environment.LOCAL_ONLY_CORE_TOOLS`
(`set_project`, `file_send`, `browser`, `pod_up`, `pod_down`) are left out of a
remote session's grants, with a whole-server `@kirocrew-core` grant narrowed to
one grant per remaining tool (`_without_local_only_grants`, which reads the
declared titles table so a new core tool is granted without an edit), and the
servers in `LOCAL_ONLY_SERVERS` (`kirocrew-computer`) are not sent at all. Both
servers also refuse those tools in band when the session's choice record says it
is remote (`refuse_local_only_tool`, from `mcp_core._call_tool_inner` and
`computer_use.tools._dispatch`), and an unreadable record refuses, because
refusing a local tool by mistake is recoverable and acting on the wrong machine
is not. The computer-use refusal is a statement about which machine the session
lives on, not a governance scope, so computer use stays ungoverned. Engine-in-guest was
rejected because it needs a credential on remote compute and a compatible
kiro-cli in every image, and it moves Crew tools, hooks and memory out of reach
of the session.

Git from a remote workspace to GitHub, GitLab, Bitbucket and Azure DevOps is
proxied through the gateway, so the credential never goes to the guest. Session
start runs `begin_git_proxy` in the background (it never delays the start, and
a failure only costs that git access): a guest worker runs `git_proxy.py`, a
stdlib HTTP server on guest loopback that writes
`~/.kirocrew-worker/git-proxy.port`, and the gateway's
`git_gateway.GitProxyChannel` answers it over that worker's own authenticated
link. `workspace_tools.run_command` gives each command git's environment config
(`GIT_CONFIG_COUNT`, git 2.31+) rewriting each host's spellings
(`workspace_tools.GIT_PROXY_REWRITES`, pinned equal to `git_gateway.HOSTS`:
`https://`, `git@host:` and `ssh://git@host/` for the first three, HTTPS only
for `dev.azure.com`, whose SSH path shape differs) to `/<host>/` on the proxy,
with `GIT_TERMINAL_PROMPT=0` and an empty `GIT_ASKPASS`, and only while the
port answers (a stale file from a dead proxy must not break public clones). No
git config file is written, so a terminal opened outside the tool is
unaffected. The channel accepts only git's smart-HTTP requests
(`info/refs?service=git-upload-pack|git-receive-pack` by GET, the two pack
endpoints by POST) whose repository path matches that host's shape in
`HOSTS`: `owner/repo` on GitHub and Bitbucket, nested `group/.../project` on
GitLab, `org/[project/]_git/repo` (percent-encoded names) on Azure. Every
segment is decoded and refused if it is empty, `.`/`..`/`-`, starts with `-`,
or holds a separator, `@`, `:` or a control character. Leading-dot repository
names such as `.github` remain valid. The upstream URL
is rebuilt from those parts (GitLab gets `.git` so it does not redirect), so the
guest cannot aim it elsewhere. It then asks
`vet_and_audit("network.egress", <host>, agent=<calling agent>, fail_closed=True)`.
The calling agent is captured by `AcpProvider` and carried through proxy startup
into the channel, never taken from a guest frame. An absent identity refuses
proxy startup and forwarding; a task-bound agent profile still intersects the
policy ceiling even on an unrestricted dashboard surface. The channel asks the person
before the first push to each repository per session (a dashboard approval
card naming `host/repo`, `dashboard_push_approval`, which refuses when there is
no dashboard to ask), and takes the credential from that host's sources on this
machine, in order (`host_credential`): `gh auth token` for GitHub (Basic
`x-access-token`), `glab config get token` then git's credential helper for
GitLab (Basic `oauth2`), git's credential helper for Bitbucket (Basic
user:password, an app password or API token), and `az account get-access-token`
for the Azure DevOps resource then the helper for Azure (Bearer, or a PAT as
Basic). The helper is asked with `GIT_TERMINAL_PROMPT=0` and
`GCM_INTERACTIVE=never`, so it answers from what is stored and never opens a
prompt or a browser on the gateway. Each host's value is cached in memory for
five minutes, never logged or framed, sent upstream only, with no redirect
followed, and the response is streamed back. An upstream 401 reaches git as a
403 naming that host's sign-in step and drops the cached value, so git never
prompts inside the guest. Azure's legacy `ORG.visualstudio.com` URLs and clone
URLs carrying a user (`https://ORG@dev.azure.com/...`) are not rewritten. The proxy lives as long as the session's CURRENT
workspace relay: a start can make more than one (an early tools probe, a
respawn), so `relay.on_close` stops the proxy only when the closing relay is
still `launch.relay`, ending its input first so it removes its own port file,
so an older relay idling out cannot leave a live session without git. The agent's
remote-workspace notice says git to the proxied hosts works as usual and that
the workspace deliberately holds no credentials, so the agent does not hunt
for keys or unset the rewrites, and `git_proxy.py` ships in the installed worker
files. Pinned end to end by `test_remote_git_proxy.py`,
which runs a real `git` clone and push through the real proxy and channel
against `git http-backend`, and asserts the token never crosses to the guest.
The git-only `GitProxyLink` binds its output reader to a byte watermark: when
unread bytes fill the reader, it stops feeding the wire endpoint before more
output can be acknowledged. The worker's unacknowledged-byte limit then stops
the guest producer, including when an upstream push stops draining its body.
If the guest cannot finish delivering the response (or uploading its request),
it sends a `cancel` frame for that request id. The channel cancels and awaits
only that request's task, closing its upstream response and releasing its
live-request slot without waiting for the rest of a repository download.

Git LFS inherits the same URL aliases. Its batch and lock-verification POSTs
receive a 307 redirect to their original HTTPS endpoint after the egress check,
without reading or attaching a gateway credential. Object transfers run directly
from the workspace: public LFS remains anonymous, while private LFS requires
workspace-side authentication. `test_remote_git_channel_bounds.py` verifies real LFS
clone/smudge and pre-push upload through this exclusion, as well as bounded wire
receive flow and git status/log/diff failure reporting through `ErrorNotice`.

A remote transport must deliver ACP byte for byte, and a vendor tunnel is not
assumed to: an SSM port forward was measured rewriting the final LF of some
writes into CR, which joins two ACP messages and desynchronizes the stream for
the life of the bridged process, and ECS Exec does not forward piped stdin. A
tunnel-based adapter therefore carries ACP in an integrity-checked framing
(sequence number, CRC and base64 payload per line in pieces of at most 32 KB,
CR accepted as a terminator, cumulative acknowledgement, resend on a bad
checksum or gap, backoff on retransmission) with a keepalive at least every five
minutes. Throughput over such a tunnel is tens of KB/s, so an adapter must not
apply fixed per-message timeouts.

`kiro_crew.remote_session` implements this transport. `wire` is a sans-IO,
stdlib-only protocol: a per-connection handshake in which both sides prove the
per-session secret with HMAC-SHA256 over fresh nonces and the resume position,
then data in base64 pieces of at most 32 KB and control lines (ack, resend,
ping, retire, close), each carrying a truncated HMAC under a key derived for
that connection and labelled by direction, so a frame cannot be forged,
reflected or replayed onto another connection. Sequence numbers and
unacknowledged data persist across connections, so a reconnect resumes with
every byte delivered exactly once, and unacknowledged data is bounded so a
stalled peer applies backpressure instead of growing memory. Retire and close
are remembered and repeated until the connection ends, so neither is lost with
a line. `worker` runs in the guest beside the agent process: it reads its
secret from a private file and unlinks it, listens on loopback only, attaches
only an authenticated gateway (a newly authenticated connection replaces the
old one), keeps the child across connections, withholds credential variables
from the child, stops the child after an orphan timeout with no gateway, and
retires on SIGTERM. `link` is the gateway side: it presents the remote process
as a stdout reader and a stdin writer, sends keepalives, drops a connection
with no inbound traffic for the stall interval, redials with jittered backoff,
renews the lease only on authenticated inbound traffic, and reports LOST when
the lease expires, REFUSED when the worker fails authentication, RETIRING when
the worker announces retirement, and FINISHED when the worker's output has
ended. The worker should run as a different OS user from agent tools, because
the secret is in its memory for the life of the session.

`AcpRuntime.set_remote_workspace` attaches a prepared workspace to a runtime
before it spawns: the workspace server joins every session's `mcpServers`
array (new and resumed) and the agent definitions are unmounted as above. The
server element's command is the local bridge
`python -P -m kiro_crew.remote_session.client`, through the shared Python
launcher (preserving its user-site policy), launched by the engine like any
stdio MCP server; it pipes MCP through a `RemoteLink` and exits 0 finished, 75
lost, 76 retiring or 77 refused. `coder.remote_worker` prepares the guest: the
three guest files and the per-session secret travel on the remote command's
stdin under `umask 077`, the worker starts (a transient systemd user service
when one exists, otherwise `setsid`/`nohup`) on an ephemeral loopback port it
reports through a port file, the dial command is
`coder ssh --disable-autostart <workspace> -- python3 ~/.kirocrew-worker/worker.py
attach --port <port>`, and the gateway's copy of the secret is a 0600 file
created with `O_EXCL` in a 0700 directory and unlinked by the bridge. A fresh
worker is started for each runtime, because the bridge consumes its secret, and
each gets its own worker id (`<binding>-<8 hex>`): a parent, its respawn and its
subagents share one binding, and a shared id let one runtime's start overwrite
another's port and key files and its stop kill the other's worker.

The workspace server's bridge never dials or authenticates itself. It is an MCP
child of the engine, inside the engine's sandbox (kiro-cli's own sandbox on a
macOS host that delegates to it), which cannot use the gateway's `coder login`
(the CLI keeps it in the login keychain), and the engine launches a stdio
server more than once per runtime (a tools probe, a reload), which a one-use
secret cannot serve. `remote_session.relay.WorkspaceRelay` therefore runs in the gateway and does the remote side per connection: for each
bridge that presents the runtime's relay token, it starts a fresh guest worker
with a fresh one-use secret, runs the integrity-checked `RemoteLink` to it over
the gateway's `coder ssh`, and pipes plain bytes to the bridge
(`client --pipe 127.0.0.1:<port> --token-file <f>`). The token is a 0600 file in
the 0700 secrets directory, read without unlinking so every launch can use it,
and removed when the relay closes. The relay binds loopback only, compares the
token in constant time before starting anything, serves at most four
connections, stops each connection's worker when it ends, logs a link that ends
other than finished, and closes when the runtime it serves ends (or a restart
replaces it), and otherwise after 15 idle minutes. It keeps one guest
worker pre-started (its own fresh secret, handed to exactly one connection and
replaced after it is taken), so a launch pays only the tunnel handshake; a spare
older than 15 minutes is stopped and replaced rather than dialled, and closing
the relay waits for the spare's stop instead of cancelling a start in flight,
which would leave a setsid'd guest worker running. Guest images can ship an
older Python than the gateway (the MicroVM image ships 3.9), so the stdlib-only
guest files must run on it, and a test runs the worker under Python 3.9.

### Spawn audit boundary

The Coder lifecycle and transport children execute on the gateway with fixed
local CLI argv built from saved operator configuration and validated registry
workspace names. Guest commands are quoted remote arguments, not local shell
programs; file requests and worker payloads travel on stdin. Their host login
must remain accessible, so these functions have individually justified entries
in `test/test_spawn_audit.py` rather than the agent sandbox wrapper. A provider
must route any gateway-side agent-influenced command, argv or cwd through the
sandbox chokepoint instead of borrowing that classification.

The gateway's Git credential and readiness CLI calls also have individual audit
entries: their verbs and host names come from the fixed credential-source and
host tables, never guest argv. Git helper discovery uses trusted Git in its
installation directory, stops repository discovery there and strips inherited
Git configuration overrides. The operator's global helpers and host logins remain
available; readiness discards every output stream without reading a secret.

The reusable guest-script rule is per-function enumeration in that same audit:
only execution that runs in the remote guest qualifies. `worker.run` starts the
guest tool process, while `workspace_tools.run_command` and the nested
`_git_read.run` operate inside the disposable guest, which supplies the isolation
boundary. Those
stdlib-only files cannot import the gateway sandbox. A provider CLI running on
the gateway does not qualify merely because it reaches a guest. The two `main`
entries are separately justified as in-process `asyncio.run`, not subprocesses.
No directory-wide exemption suppresses future spawn sites.

Guest file writers share a persistent empty lock sidecar. The Windows branch
polls the non-blocking byte-range lock until acquired or the bounded ceiling
expires, and refuses the write on expiry; it never writes a byte through a lock
file that another writer can hold. Its poll and ceiling mirror
`platform_lock_compat` while preserving the guest's stdlib-only deployment.

A session runs remotely only when the owner bound it to a profile through
`PUT /api/chat/slots/{slot}/environment` (owner-only; `{"profile": id}` binds,
`null` clears). The choice is stored per session key in a session-choices file under the data
home's Coder directory, owner-only, and takes effect at the session's next
runtime start; a running runtime is never moved. `providers/acp.py` asks
`coder.session_launch.prepare_for_session` before spawning, only when Session
Environments has a Coder URL or a saved profile (otherwise no session can be
bound and the choices file is not read; a subagent resolves to its parent's
workspace, below), and the warm pool is bypassed for a bound session because a pooled child is a
local engine. Preparing reserves the session's workspace in the Coder ownership
registry before any compute exists, then finds, creates (with an auto-stop) or
starts it using the gateway machine's own `coder login`, and adopts it only
while its owner is the signed-in user, its template is the profile's, and its
uuid matches the registry row. A workspace deleted outside Kiro Crew is never
silently recreated. Every failure raises `SessionEnvironmentUnavailable`, and
an unreadable choice record fails closed, so a bound session never falls back to
running on the gateway machine. The engine keeps its local working directory;
the guest working directory is where the workspace tools operate.

The card's "Verify readiness" step calls `POST /api/session-environments/check`
(owner-only), offered only with no unsaved draft because it reads the SAVED
configuration. It is read-only: `coder whoami`, then `CoderClient.probe` (the
template list) per profile, and `git_gateway.login_status` per proxied git
host, which runs only status commands (`gh auth status`, `glab auth status`,
`az account show`) and reads their exit codes with output discarded. A host
that falls back to git's credential helper reports `unchecked` rather than
asking the helper, since asking would hand the gateway the secret. No
workspace is created, started or touched. The "Manage" step lists
`GET /api/session-environments/workspaces` (owner-only, registry-only, no Coder
command): each session's workspace with an archived session's deletion date,
polled every 15 seconds, beside a link to the Coder
deployment when one is saved (`safeHttpUrl`, new tab, `noopener noreferrer`).

`session_environments.default_profile` (a saved profile id; "" is this
machine) is the environment a NEW owner dashboard chat starts on.
`api_chat_slot_create` calls `apply_default_environment` for a freshly created,
non-peer slot, which writes the choice only when the chat has no choice and no
messages, so an existing session never moves and the composer can still change
it before the first message. A default that no longer names a saved profile,
or any failure, leaves the chat on this machine without failing the create. A
chat bound this way is not prewarmed, because a local spawn would run where the
session no longer lives. Scheduled jobs, channels and peer-bound chats are
untouched, and subagents follow their parent. The section validator refuses a
default that the same save drops, and the Settings card resets the default to
this machine when its profile is removed.

The composer's environment chip (`SessionEnvironmentChip`) is the user-facing
picker for that endpoint. It renders only once a profile exists, is disabled
while a turn runs, and a changed choice arms the slot's deferred reset so an
idle runtime cannot keep running where the session no longer lives. The choice
is fixed for the session's life once it has a message: `GET` reports
`locked`, `PUT` answers 409 `environment_locked` to any change (re-asserting the
current choice is not a change), and the chip becomes a non-interactive status in
the same place, because the session's files, shell state and history live in
that environment.

Archiving a session (the tab close, session control's close, or the idle
cleanup sweep) hands its keys to `coder/retention.schedule_archived` from inside
the close path's identity check, as a background task that never delays or
fails the close. That stops the workspace (not on a stop-destroys-compute
template, see Template contract) and stamps its registry row
`deletion_due_at` = now + `session_environments.archived_retention_days`
(0-7, default 7). At 0 there is no stop: the retention sweeper is woken and
deletes it on its next pass. `prepare` runs `retention.resume_check` first, so reopening the
session before the deadline clears the stamp and restarts the same workspace,
files intact. `retention.run_sweeper`, a gateway background task (every minute,
first pass 30 s after boot), runs `retention.sweep_due`: a due
row is flipped to `deleting` under the registry lock (`begin_deletion`, the
same critical section `cancel_deletion` takes, so a reopen and the sweep cannot
both win), its owner, template and uuid are proven before anything is touched, the
workspace is deleted, and only then is the row dropped. A row whose name now
names another workspace is forgotten without touching that workspace, a failed
delete stays `deleting` and retries next pass, and a reopen while `deleting` is
refused with "try again in a minute". Once the row is gone the session's choice
record still names the profile, so a later reopen provisions a fresh workspace.
Rows with no stamp are left to Coder's auto-stop.

Archiving checks for work that would be lost before it stops anything:
`retention.warn_unsaved_work` reads `git status` in each RUNNING remote
workspace (`coder ssh --disable-autostart`, so stopped compute is never woken), bounded by
`UNSAVED_CHECK_SECS`, and publishes one `system.agent` notice naming the
uncommitted files, unpushed commits or never-pushed branch
(`session_browse.summarize_unsaved`). It never blocks or fails the archive.

When a session that already has a conversation is given a NEW resource
(`PreparedRemoteSession.created`: the old one was deleted) the provider start calls
`session_environment.mark_workspace_recreated`, and the next `[PROJECT]` rail
says once that the workspace was recreated empty, so the model does not reason
about files from earlier turns. Every worker id carries the gateway process's
`_GATEWAY_EPOCH`; the first workspace start for a binding in a new process
(`RemoteWorkerLaunch.reap_stale_workers`) stops that binding's workers another
process started, so a worker a crashed gateway left running does not keep
working beside the resumed session's.

Remote environments stop with the gateway that drives them, unless
`session_environments.stop_on_shutdown` (default true, a Global defaults
toggle) is off, which keeps compute running while the gateway is down in
exchange for a warm restart. Kiro Crew itself stops a Coder workspace only on
archive and, with this toggle on, at a graceful shutdown; any other stop is
Coder's own auto-stop. On a graceful shutdown `_shutdown_and_exit` runs
`retention.stop_for_shutdown` beside its own graceful shutdown, under its own
`SHUTDOWN_STOP_SECS` (20 s) ceiling, never raising. It stops every running
Coder workspace the registry owns (uuid-proven, none `deleting`), leaving a
stop-destroys-compute one running (counted `skipped`). Nothing is
stamped for deletion, so a session reopened after the next start resumes the
same workspace. A gateway that dies without a graceful shutdown cannot do this,
so each guest also stops on its own: the worker stops the engine and exits after
`wire.ORPHAN_TIMEOUT_SECONDS` (600 s, above the link's 300 s lease so a gateway
restart reconnects first) with no authenticated gateway, the one value the
gateway's launch default and the worker's own defaults read, and the relay
retires a never-attached spare at `wire.SPARE_MAX_AGE_SECONDS` (four fifths of
it) so it never dials an exited worker; a Coder workspace stops at its deadline,
which only the gateway extends. Deletion of an archived session's workspace
after its retention needs the gateway: a stopped Coder workspace keeps billing
for storage while no gateway runs.

Dashboard views that read the gateway's project describe the wrong machine for a
remote session, so they switch on `useSlotRemoteEnvironment`: the composer drops
the project/branch chip, the Git view says it does not apply, and the Files tab
renders `RemoteFilesPanel` in place of the local browser. That panel reads the
guest through `GET /api/chat/slots/{slot}/environment/browse?op=list|read&path=`
(owner-only), which `coder.session_browse` answers by running
`workspace_tools.py --browse` over the gateway's `coder ssh --disable-autostart`.
The browse mode accepts only `list` and `read`, uses the agent tools' root
confinement and caps, and takes its request on stdin so a path never reaches a
shell. A stopped workspace answers 409 `workspace_stopped`, and a guest whose
files predate the browse mode gets them reinstalled once before a retry.

The same panel edits: a file opens in the shared code editor (`PierreEditor`)
with per-file unsaved drafts, and Save sends
`PUT /api/chat/slots/{slot}/environment/file` `{path, content, expected}`
(owner-only, SEL-audited), which runs the separate `workspace_tools.py --write`
mode so the browse mode stays unable to write. The guest refuses a save whose
`expected` text no longer matches the file (409 `file_changed`), so a stale
buffer never overwrites a newer edit by the agent, and a truncated or binary
read is never offered for saving. Remote files are edited in this panel rather
than in the panel's file tabs, because those tabs read, watch and diff through
the gateway's local-disk endpoints. The Git view becomes `RemoteGitPanel`:
branch, porcelain status, a per-file `git diff HEAD`, and the last 50 commits,
from fixed-argv read-only git runs in the guest with `core.fsmonitor` and the
pager disabled. A terminal opened from a remote chat (the `+` menu or the Files
header) carries `remote_slot` instead of `cwd`; the terminal handler resolves
that slot's session to `coder ssh --disable-autostart <workspace>` on the
gateway and refuses with a 409 rather than opening a local shell when the
session is not remote or its workspace is unavailable.

Once a remote session is fixed, its composer chip is a status, not a control.
Only a choice back to this machine prewarms, since prewarming a remote one would start
billed compute on a menu pick. Coder lifecycle calls (`coder list`, `create`,
`start`) run under the Coder client's output and timeout bounds, never the
64 KB guest-command bound, because their JSON grows with the deployment.

A first remote start can take minutes, so it reports provider-neutral stages
through `session_environment.report_start_stage`: `checking` (sign-in and
lookup), `provisioning` (only when compute must be created or started),
`connecting` (worker install and tunnel) and `starting_agent`. The progress
record also carries `entered`, every stage this start has been in, in order.
The setup steps show every stage before the current one as done, whether or
not it ran: a stage never entered still has its outcome (a workspace that is
already running satisfies "Starting workspace"), so a dash there would read as
a failure. The setup
card is for a session's first connection: once the transcript holds a reply, or
the progress record says `reconnect` (decided when the start begins: this
gateway already had the session attached, `_remote_workspaces`), a later engine
restart that reconnects to a running workspace shows the turn as usual. The
composer chip reads the same hook with no transcript, so `reconnect` is what
keeps it from saying "Starting agent" while the transcript says "Thinking…", and only a restart that enters `provisioning` (a stopped workspace being
started again) brings the card back. Every `coder`
call on this path is a round trip of a second or more, so the path keeps them
few and overlapped. The worker files go up in one `coder ssh`, as a JSON header
of `[name, length]` pairs followed by the bytes, written by a short `python3`
program in the guest (`remote_worker._install_command`), through per-process
temp names so two starts installing into one workspace cannot clobber each
other. The stage record is
in-memory, bounded, and cleared in the `finally` of
`AcpProvider._start_kiro_runtime`, so ready, failed and cancelled starts all end
the stage. `GET /api/chat/slots/{slot}/environment` returns it as `starting`,
and the chip polls once a second only while a turn runs on a remote profile,
showing the stage and elapsed time in place of the profile name and announcing
each stage once through a separate live region.

A subagent of a remote session inherits its environment rather than running on
the gateway machine. Before the spawn, `inherit_environment_for_subagent` writes
the child's choice with a `parent` owner key (resolved to the root session, so a
grandchild lands in the same workspace), and `prepare_for_session` prepares the
OWNER's registry row, which reuses that workspace instead of allocating a second
one. The child takes the dedicated-process arm and gets its own guest worker and
fence: a shared runtime's workspace server was launched for the parent with a
spent one-use secret, and the companion runtime is a local engine. A choice that
cannot be read or written refuses the run, and a malformed `parent` value fails
closed. Because the child's heavy work runs in the workspace, its spawn is not
held to the gateway's `spawn_min_memory_gb` floor, only to the reserve for its
local engine ([subagent](subagent.md)). The per-message context names the guest directory for a remote session
(`session_environment.remember_remote_workspace`, set and cleared at every
runtime start) in place of the gateway's `[PROJECT]` path, and rewrites the
`[RESOURCES]` line (`ResourceStatus.context_line(remote=True)`) to scope the
reading to the gateway. A tight gateway, which runs the engine and its
subagents, is named as the gateway machine and only flags sub-agent waves; the
line says the workspace is a separate machine it does not measure, so builds,
tests and shells there are not narrowed for it. The line stays silent when the
gateway is clear.

A reversed transport, in which the worker dials out to the gateway with a
single-use enrolment token, would remove the provider tunnel from the data path
but needs a gateway endpoint the worker can reach; it is not part of this
contract.

Every session environment provider meets these lifecycle requirements before a
session can be offered on it. A worker serves one owner: the first session binds
it to the owning operator, and it never runs another owner's session or reuses
another owner's workspace. The gateway holds a lease on each remote session and
renews it with a heartbeat on the session connection, so a worker that misses
its lease is treated as lost rather than slow. A lost or released worker never
loses the conversation, because the gateway restores the session on a fresh
worker with `session/load` from its own transcript at the next prompt, and only
uncommitted workspace changes are at risk, which the UI states. A worker that
knows its compute will be reclaimed retires cleanly: it accepts no new turns,
finishes or cancels the turn in flight, reports release to the gateway, and
exits. Teardown is owned by the provider adapter and is idempotent, it deletes
only resources whose ownership record the gateway wrote, and it never stops a
shared parent resource while another session still uses it.

The Coder ownership registry requires a non-empty `deployment_id` at
allocation and writes it before workspace creation; reusing a row requires the
same deployment, lifecycle updates cannot retarget it, and retention leaves rows
of another deployment untouched.

CLI failures report the exit code and fixed operation timing, without copying
stderr into logs or exceptions. Stderr is drained without retention, since remote
commands can emit credential values without recognizable labels. This trades
raw failure detail for a diagnostic boundary that does not depend on recognizing
every credential spelling. Timeout and cancellation terminate the process tree,
while successful structured stdout remains bounded and validated by operation.

### Template contract

A Coder template for remote sessions provides:

- a pinned image and architecture;
- an unprivileged user with a writable home and workspace root;
- the same workspace across starts;
- the Coder agent, Python 3 and a POSIX shell;
- no gateway, model, Git or storage credential in the image;
- outbound access for development work, and no public ingress to the guest.

Its storage must survive a workspace stop unless the profile sets
`compute_survives_stop: false` (a template whose stop destroys the compute and
its files, such as MicroVM). For such a profile neither archive nor
`stop_for_shutdown` stops the workspace; the deletion stamp and sweep are
unchanged, and the unsaved-work notice says the files go when the compute stops
(the template's own deadline or the retention deletion).
`retention.compute_survives_stop` reads the flag at the time of the stop from
the session's bound profile when it still names the binding's template, else
from every profile on that template; no match reads `false`, so the workspace is
never stopped. Recovering a session on such a template after a stop needs the
S3 Files checkpoint integration, which is not part of this contract.

The worker launcher prefers a transient `systemd --user` service and falls back
to `setsid`/`nohup`, which may not survive Coder's SSH session cleanup on an
image without systemd.

### Kiro Cloud Sessions

Kiro Cloud profiles (`session_environments.kiro_cloud_profiles`: `id`, `name`,
`repositories`, each `name` or `owner/name`, at most 10, no other key) are the
one provider whose AGENT runs remotely, so the "engine stays on the gateway"
rule above does not hold for them. A profile id names one provider: the config
writer refuses an id a Coder profile also uses, and `session_launch.profile_provider`
is the one resolver the launch path, `session_browse`, the picker (`_profiles()`)
and the choice lock share. An id a hand edit saved for both providers names
neither: the picker omits it, a launch or browse raises
`SessionEnvironmentUnavailable`, and a started session bound to it stays fixed.
The choice row records the provider the id resolved to when it was picked
(`coder` or `kiro_cloud`; a subagent's row copies its parent's). A launch or
browse derives the provider from the id and refuses with
`SessionEnvironmentUnavailable` when it differs from the row's
(`session_launch.chosen_provider`), so an id reused across providers never
moves a started session. The choice lock also reads the row's recorded provider,
so deleting a cloud profile or reusing its id does not unlock a started cloud
session. `prepare_for_session` provisions nothing and returns
`PreparedCloudSession` after the same `before_compute` KAS check once the
governance ceiling allows it. The provider passes its execution agent through
to active-profile resolution, including task/agent bindings. An explicit
policy or profile denial of `capabilities.kiro_cloud_sessions` always refuses
the start, even without any tool-execution restriction. When the policy or
profile governs a tool-execution scope, only a policy opt-in can permit the
start; a profile can withdraw that allowance. An unreadable ceiling or profile
refuses. The check emits a SEL governance-decision event for grants, denials
and fail-closed evaluation errors, naming the session, execution agent,
capability and reason (see governance.md). `AcpRuntime.set_cloud_sandbox` (KAS backend
only, before spawn) installs `KasCloudSandboxHarness`
(`acp/harness/kas_cloud.py`) as that runtime's harness and declares the runtime
MCP-free. Every cloud answer comes from that harness through the existing
seams (`session_extras`, `session_mcp_servers`, `notification_aliases`) and the
`session_request` seam, which every host the backend table serves answers by
returning the request and agent unchanged; `load_session` asks it only inside
its KAS branch, so the session start path of an ordinary session holds no cloud
conditional (harness-parity H13). The cloud harness makes `session/new` carry the `_meta.kiro` keys kiro-cli's
own `--cloud` flag sends: `executionTarget: {kind: "cloud-sandbox"}`,
`sessionSource: "remote"` when KAS lists it in `sessionSources`, and
`repositories` or `isEmptyWorkspace: true`. `session/load` carries only
`sessionSource: "remote"`. KAS relays such a session to Kiro's cloud and
forwards only the mode, target and repositories, so Kiro Crew sends
`mcpServers: []`, no `customAgents` and no `set_mode`, and waits for no MCP
readiness reports. The session runs Kiro's default agent with that sandbox's
tools; Kiro Crew's MCP servers, agent spec and PreToolUse gate do not apply to
it, which is what the operator opts into by choosing the profile (the change
card catalog classes `kiro_cloud_profiles` and `default_profile` as code-exec). A start is refused,
never run locally, when KAS's `initialize` does not list `cloud-sandbox` in
`agentCapabilities._meta.kiro.executionTargets`. The provider binds the sandbox
through the same `attach_remote_workspace` seam as a workspace, so an engine
respawn re-binds it, and records the session with an empty workdir: the
`[PROJECT]` rail then names the cloud sandbox instead of a gateway or guest
path. `session_browse._resolve` raises `CloudSandboxOnly` (a
`SessionEnvironmentUnavailable`) before any Coder call, so files, git, the
terminal and the unsaved-work check all refuse it; the browse and file routes
answer 409 `cloud_sandbox`. A cloud session with messages is locked even though no
registry row exists, since its history lives in Kiro's cloud store. Archiving
leaves the cloud session to Kiro.

## Essential-context delivery contract

Application code imports `ContextPromptProvider`, `ContextStreamEvent` and
`context_provider_of` through `kiro_crew.agent_sdk`, never through the provider
or ACP packages. The SDK driver admits real provider classes without evaluating
dynamic proxy attributes. Event protocols expose only the read-only evidence the
receipt needs; the receipt stream preserves the caller's concrete event type.

`kiro_crew.agent_sdk.drivers.acp.projected_session_mcp_servers(agent, *, work_dir=None)`
is a synchronous driver helper for the deterministic workflow test scenario,
not a top-level SDK export. It returns the existing filtered list of dictionaries
and preserves resolver errors; async callers offload it. Projection itself neither
authenticates the caller nor starts a server. Ordinary provider capability checks
and authenticated transport remain responsible for admission; canonical execution
records supply memory routing.

A caller holding its normal session lease supplies the actual `LLMProvider` as
`context_provider=provider` to `ContextBuilder.build_message`, together with the
trusted `session_key`, resolved `memory_store`, execution `agent`, project and
scope flags. The builder validates and renders a complete snapshot and stages it
in `provider.essential_delivery`; building or previewing is never acknowledgment.
Previews omit `context_provider`. Shared-runtime callers pass their own project
explicitly, rather than inferring a per-session cwd from the shared runtime.

The returned prompt remains complete and may undergo the caller's normal prefix
transforms. Send it through `provider.stream(prompt)` or `stream_and_collect`.
No caller-side acknowledgment call is needed. ACP implementations match the exact
staged envelope, remove it only when the same scope/content is acknowledged in
that native conversation, and acknowledge only a raw successful end-turn after
non-control output or tool activity. Empty, synthetic, cancelled and failed
attempts cannot commit a receipt. Closing a stream after its genuine terminal is
supported; closing earlier invalidates the receipt. Compaction, conversation
clear, agent switch and client/process replacement invalidate prior evidence,
including an in-flight candidate. `kiro_crew.agent_sdk` names every invalidating
observation (`CONTEXT_EVENT_COMPACTION`, `CONTEXT_EVENT_CLEAR`,
`CONTEXT_EVENT_AGENT_CHANGED`; the set is `essential_delivery
.RECEIPT_INVALIDATING_EVENTS`). Both ACP providers also invalidate BEFORE
dispatching a history-discarding slash command (`/compact` and `/clear`, the
`RECEIPT_RESETTING_COMMANDS` set, applied through `EssentialDelivery
.prepare_command`), because the status notification can be absent (a backend
that treats the command as plain text) or arrive only inside the next turn's
stream; other slash commands keep prior evidence. A `/clear` empties the native
history while the session id and process stay the same, so without this the
next warm turn would deduplicate against an acknowledgment the conversation no
longer holds. The receipt observes events yielded inside a `stream` or
`stream_command`. If a between-turn notification is yielded only after the next
prompt is written, an otherwise unchanged warm prompt can omit essentials before
that event invalidates the receipt. Whether kiro-cli emits such between-turn
notifications, and whether the queued frame surfaces before or after the next
prompt is written, has not been established by a live run (see "Native harness
notifications relied on" below).

### Native harness notifications relied on

This dedup depends on the harness reporting every native context loss it
performs. That is not an ACP-spec guarantee: the public ACP protocol defines no
compaction or truncation notification, and `_kiro.dev/*` is a Kiro-private
extension without a published contract. What the code relies on, per backend:

- kiro-cli: `_kiro.dev/compaction/status` (every `status.type`, including
  started, completed, failed and the recovery variants, maps to
  `compaction_status`), `_kiro.dev/clear/status` (→ `clear_status`) and
  `_kiro.dev/agent/switched` (→ `agent_switched`). Evidence is the shipped
  binary's own method strings (kiro-cli 2.21.4) plus the local parsers in
  `acp/client.py` and `acp/session_handle.py`; no end-to-end run against a real
  kiro-cli has confirmed that every silent trim path emits one of these.
- KAS: `summarization_started/completed/failed` session-info frames map to
  `compaction_status`; `/compact` and `/clear` are plain prompt text there,
  which is why the command path pre-invalidates instead of waiting.
- claude-agent-acp: the adapter announces a compaction with a `Compacting`
  started text but emits `completed` only for a manual `/compact`; an
  automatic mid-turn compaction leaves that started dangling (only a
  `usage_update` follows), and `AcpClient` synthesizes the `completed`
  `compaction_status` at the turn's `end_turn` terminal. This is a heuristic on
  adapter text output, the weakest of the three.

History loss without any observed invalidation is not detected by the receipt:
unchanged warm turns can continue omitting the snapshot until another invalidation,
content change or conversation replacement requires delivery. A notification
observed only after a prompt was dispatched cannot repair that already-sent
prompt; it invalidates the receipt for subsequent delivery. Neither behavior is
a guarantee that a silent trim recovers on the next turn.

`context_provider_type` reports the actual backend label, independent of the
installation's `agent.provider` setting. `native_context_documents` defaults to
empty. Kiro member launches capture admitted selected-template sources after
materialization and governance checks. Successful activation of that same agent
in the same cwd transfers source responsibility to the handle. This relies on
Kiro's supported `--agent` prompt/resources contract, not a per-file model receipt.
Changes to a launch-version source receive a complete manual replacement.
Kiro's implicit workspace-root AGENTS and default/always steering in project and
global steering directories also belong to that launch contract while the
workspace inherits kiro-cli's default resources. In an opted-out workspace, the
native skill projection keeps the authored projected view unchanged. For a
non-member Kiro runtime, successful selection of that view records the paths of
the markdown files its declared `file://` resources match, from the exact
`NativeSkillProjection.specs` entry the child will load; no file body is read.
A rejected view aborts startup. A missing projection, missing view, or a path
walk error records no launch documents and does not stop the spawn.
SOUL is native-owned only when explicitly declared.
The mirrored steering reference records engine/version differences for conditional
modes, so Kiro uses the fallback selector instead of claiming full native support.

Folder steering is deliberately NOT part of that launch contract. A chat filed
into a folder that declares `steering_dirs` (see [config](config.md)) has those
directories read by `kiro_crew.folder_steering.collect_folder_steering` and the
resulting documents placed into session-start context by the context builder, for
EVERY provider — kiro-cli, Claude Code, Codex, KAS, and any config-authored
harness — with no provider branch on that path. `SpawnContext`, `AcpRuntime` and
the harnesses carry no folder-steering field, so every harness produces an
identical `SpawnPlan` for a folder chat and a non-folder chat; delivery cannot be
lost by adding a provider. A file whose realpath lies under the chat project's
`.kiro/steering` or under `~/.kiro/steering` is skipped ONLY where the active provider
already delivers those trees -- kiro-cli loads them natively while the workspace
inherits kiro-cli's default resources, the Claude Code seam receives the explicit
steering load, KAS reports `native_steering` -- so they are not sent twice; on a
harness with no such path (Codex, OpenCode, Pi, Goose, DeepSeek) the folder
delivers them like any other document rather than skipping rules nothing else
would carry. The context builder's entry points (`build_message`,
`build_session_context`) pass a harness to the member essentials builder only
when the caller or its context provider names one, so an unnamed harness keeps a
private member inheriting. One rule holds everywhere the value is read: a harness
that is not named is not assumed to be kiro-cli, so the folder dedup on that
same turn skips nothing as delivered and the folder carries the trees whole. For an
opted-out non-member kiro-cli chat, fresh folder rendering and compaction
reinjection skip only the canonical document paths recorded from the projected
view when the process launched. kiro-cli re-reads its declared resources for
every request, so the record holds paths alone: a recorded guide reaches the
model through kiro-cli's own read, as the file reads at that request, and the
folder never sends it in any version. A guide the record does not name, one
created after launch among them, is collected as usual. Nothing is recorded when
the files the view declares total more than 128,000 bytes on disk
(`_LAUNCH_RECORD_MAX_BYTES`), whatever their extension: kiro-cli drops whole
files once they pass three bytes per token of the model's context window
(kiro-cli 2.27.0 kept 490,000 bytes and dropped a file at 494,000 on the
164,000-token window, the smallest it lists), and the model can change
mid-session. The total is checked at launch and again each time the session's
provider hands out the record, so a declared file that grows past the bound
mid-session, or a new one a declared glob picks up, hands the folder every guide
again from the next render on. The record applies only while the session
activates the same projected agent view it was read from: when `set_mode` ran,
the alias it sent must be the launch alias; otherwise the projection must still
name the launch alias for the agent. After the agent's declared resources
change, the folder sends every guide. The projected view itself is unchanged, so
a chat with no folder steering keeps kiro-cli's declared project tree. A missing
provider, projection, view, or launch record makes the folder carry its trees
whole, preferring a duplicate over losing a guide; a launch record that cannot
be taken is logged as a warning naming the agent and the error, and leaves the
record empty. A view the projection rejects stops the session from starting, so
no context is delivered at all. Documents honor the steering
`inclusion` frontmatter (`always`, or absent, is included; `manual`, `auto` and
`fileMatch` are skipped and left to their native trigger), and each file is
admitted against its own declared directory as the trust base, so a symlink can
never read outside the root the operator pointed at. A root that is, contains,
or lies inside a memory store (every configured V1 workspace -- the default
`workspace/` under the crew data home and each `workspaces` entry, wherever its
directory points -- or the `memory_stores/` tree) is refused by the folder API
and skipped by the collector (compared casefolded, so an alternate-case spelling on a case-insensitive filesystem is the same silo): a named memory store is a silo, and folder
steering must not carry one store's Markdown into another store's prompt. The
fence is built from the configuration's `workspaces` table, and while that table
cannot be read (an unparseable `config.json` or a non-object `workspaces` value,
which the loader records as a degraded section rather than repairing to `{}`) the
fence is INCOMPLETE and fails closed: the folder API refuses to admit any steering
directory (clearing to `[]` still works), and the collector reads nothing and
emits a `[FOLDER STEERING OMISSION: ...]` line saying so -- the default
directories alone are not the fence when an operator's external workspace may be
missing from it. An unparseable `config.json` is remembered for the life of the
gateway (fail-closed and sticky), so the fence stays incomplete until the file is
repaired AND the gateway restarted; a degradation of the `workspaces` value alone
clears once the file is repaired. The
stored value is bounded as STORED (the canonical spelling, after `~` and link
expansion), and re-validated per entry on every resolve: a directory that has
since vanished or become refused is skipped with a warning while the rest of the
chain still steers; only a malformed stored shape fails the resolution.
The section's own frame,
`[FOLDER STEERING — …]` / `[END FOLDER STEERING]`, is a prompt-boundary marker in
the same set as `[CURRENT USER REQUEST —]`: a copy planted in a channel message,
a memory line or a steering body is neutralized by the session-context scrub and
by the renderer's body scrub, and the genuine frame is minted AFTER that scrub
(outside the `[SESSION CONTEXT …]` block on a fresh session, and around the
already-scrubbed bodies on compaction reinjection), so only the operator-selected
section ever carries it. The non-member section is
capped like the existing steering section; a member (private-memory) chat carries
the documents inside its essentials envelope instead, which applies its own
document-count and per-source byte bounds; a private-member chat in Temporary
memory mode receives no folder steering, exactly as that mode withholds the
member's project documents (the envelope is built with reads blocked), whereas a
non-member Temporary chat still receives it. Collection itself is bounded twice --
a 64-document ceiling on what is read and a 4096-entry ceiling per root on what
is enumerated -- and a ceiling that fires is never silent: the section ends with
one `[FOLDER STEERING OMISSION: …]` line per fired ceiling stating what the model
is not seeing in the terms the walk saw it -- unexamined Markdown candidates past
the document ceiling; and, past the entry ceiling, the Markdown files the listing
had already produced but will never read (counted as files) separately from the
directories never listed or entered (counted as directories, a floor) -- and a member
envelope that had to drop a tail carries the same counts as one extra
`folder-steering://omitted` essentials document, so a capped tree never reads
like a complete one. After provider compaction the section is re-injected under
a `[REINJECTED AFTER COMPACTION — folder steering]` line. The tree is read only
through descriptor-pinned directory handles (parent chain pinned, each child
opened relative to its parent with `O_NOFOLLOW`, one descriptor per level of the
active ancestry); on a platform that cannot open a directory relative to a
descriptor (native Windows) the folder API refuses a non-empty `steering_dirs`
before any filesystem call and the collector skips a stored value with a
warning, because a by-name `isdir`/`realpath` on a swapped junction is itself
the outbound SMB probe.

KAS inline prompts and file resources come from the actual `customAgents`
definition sent by `session/new`. File expansion uses that definition, not a
reread of a possibly different project template. Successful activation publishes
those source versions to the direct `AcpSessionProvider`; other agents' definitions
cannot supply its ownership. Resource URIs and their order remain unchanged on
the wire, and conditional, skill and knowledge resources keep native selection.
Exact template and body matches omit initial manual copies. The complete-envelope
budget is checked BEFORE omission: 64,000 characters including wrappers, with
whole guides left out and named when it is exceeded
([memory-skills-hooks](memory-skills-hooks.md)); reads
refuse above 256,000 bytes per source, and resource expansion is bounded to 64
unique documents. KAS's existing registration ceiling is 50 custom agents, not a
file-body budget. An id in `agent_files.KAS_RESERVED_AGENT_IDS` (`default` and
the built-in mode ids `vibe`, `spec`, `quick-spec`, `bug-fix`, `plan`,
`autonomous`; exact, case-sensitive) is sent on the wire as
`agent_files.kas_wire_agent_id(id)` (`<id>-crewmate`) -- the engine accepts an
entry under the id itself and either drops it or keeps its own built-in under
it, so it would surface only as an unadvertised mode or as the built-in running
under the crewmate's name. The projection and every Crew-side record keep the
crewmate's id (`crew-mode.md`, "Template names the harness cannot activate"). This repository does not establish a universal native model or
resource truncation limit; real harness versions still need that integration check.
Resume and replacement snapshots retain complete text. Frameworks without native steering receive a
conditional discovery index: the agent reads a guide only after its explicit
manual, file-pattern or description-relevance condition holds, never as an
always-on body.

### Managed-source root checks

Each `_refuse_managed_source` call loads configuration anew and resolves each
unique declared admin/workspace root once through `_comparable_root`. The local
results serve only that call's overlap and containment checks. Later calls
revalidate configuration and link targets; no authorization is cached across
requests or turns. Candidate paths remain lexical until the existing path gate
admits them, and rejected UNC roots are never resolved. Every admin-overlap and
managed-memory exclusion remains in force.

### Reproduce essential-context wire measurements

`test/test_essential_delivery.py` keeps the fresh-plus-20-warm receipt assertions
in the normal suite. Its companion no-receipt test also runs automatically and
captures the exact submitted strings as JSON, with 21 complete essential
snapshots and one current request per turn. The fake transport replaces only the
external subprocess; context construction and the provider stream run for real.
These are submitted UTF-8 prompt bytes, not model token counts or proof of native
harness ingestion.

For a before/after measurement, run that same capture test in a separate trusted
Git snapshot, then pass its log as `KIROCREW_ESSENTIAL_BASELINE` to the current
receipt test. This variable now names a UTF-8 capture log, **not a Git ref**.
The current interpreter reads JSON data only; it never executes source from the
baseline or replaces loaded modules/classes. All baseline production imports come
from the complete snapshot, not old context code mixed with current providers.

From the current checkout, the following Bash recipe uses an existing interpreter
with the project's test dependencies. Set `PYTHON` to its absolute path and
`BASELINE_SHA` to the full commit SHA of a reviewed, trusted pre-change revision.
A separate checkout/process prevents module mixing, not hostile-code execution;
never use an untrusted revision. No dependency install or live gateway is needed.

```bash
set -euo pipefail
: "${KIROCREW_SCRATCH:?}" "${PYTHON:?}" "${BASELINE_SHA:?}"
root=$(git rev-parse --show-toplevel)
sha=$(git rev-parse --verify "${BASELINE_SHA}^{commit}")
[ "$sha" = "$BASELINE_SHA" ]
measurement=$(mktemp -d "$KIROCREW_SCRATCH/wire.XXXXXX")
snapshot="$measurement/snapshot"
mkdir "$snapshot"
git archive --format=tar "$sha" > "$measurement/source.tar"
tar -xf "$measurement/source.tar" -C "$snapshot"
# Copy test instrumentation only; leave all production sources at the baseline.
cp "$root/test/test_essential_delivery.py" "$snapshot/test/"
cp "$root/test/test_member_essential_context.py" "$snapshot/test/"
export PYTHONPYCACHEPREFIX="$measurement/pycache"
export TMPDIR="$measurement"
export KIROCREW_HOME="$measurement/home"
export KIRO_HOME="$measurement/kiro"
export KIROCREW_WORKSPACE="$measurement/workspace"
export PYTEST_ADDOPTS=
unset KIROCREW_ESSENTIAL_BASELINE
printf 'BASELINE_COMMIT=%s\n' "$sha" > "$measurement/before.log"
(
  cd "$snapshot"
  PYTHONPATH="$snapshot/src" "$PYTHON" -m pytest -n0 --no-cov -q -s \
    -o addopts= --timeout=60 -p no:cacheprovider \
    --basetemp="$measurement/before-tmp" \
    test/test_essential_delivery.py::test_wire_without_receipt_fresh_and_twenty_warm_turns
) >> "$measurement/before.log" 2>&1
PYTHONPATH="$root/src" KIROCREW_ESSENTIAL_BASELINE="$measurement/before.log" \
  "$PYTHON" -m pytest -n0 --no-cov -q -s -o addopts= --timeout=60 \
  -p no:cacheprovider --basetemp="$measurement/after-tmp" \
  "$root/test/test_essential_delivery.py::test_actual_wire_fresh_and_twenty_unchanged_warm_turns" \
  > "$measurement/after.log" 2>&1
printf 'Reports: %s/before.log %s/after.log\n' "$measurement" "$measurement"
```

The after log prints `BASELINE_BYTES` and `WIRE_BYTES`: fresh bytes, all 20 warm
sizes, total bytes and envelope count. The baseline log retains the commit and
all 21 strings so their byte lengths can be checked independently. Byte totals
include real temporary-path lengths; the envelope-count and content assertions
do not depend on those lengths. Missing, duplicated or incomplete captures fail
rather than silently dropping the baseline. The recipe leaves only session-owned
scratch output for inspection and normal scratch cleanup.

## LLMProvider ABC (`providers/base.py`)

`providers/base.py` is the surface, and it is the only honest copy of it — a
hand-maintained member list here goes stale without anything going red. Read the
module.

The members that carry *contract* meaning, rather than plumbing, are the ones a
harness can get wrong:

| Member | Contract |
|---|---|
| `start` / `shutdown` / `stream` | The turn lifecycle every consumer depends on. |
| `prompt_timeout_for_deadline` | Optional immutable transport budget for an outer deadline. The default `None` preserves legacy one-argument `stream(message)` calls; capable providers receive the explicit timeout through `agent_sdk.capture_prompt_timeout`. |
| `approve_tool` / `reject_tool` | Tool-approval responses; `approve_tool` returns whether an allow answer was sent, and a provider that cannot answer must still refuse, never hang. |
| `context_usage_pct`, `context_usage_unknown`, `context_window_tokens`, `context_used_tokens` | The context meter. `context_usage_unknown` is what distinguishes "0%" from "not measured". |
| `session_id`, `cleanup_session`, `cwd` | Session identity and cleanup routing; a wrong `cwd` persists the wrong workspace on resume. |
| `served_model`, `available_models` | The model actually served, which can differ from the id Crew stored. |
| `maybe_refresh_available_models(catalog_ids)` | Revalidate the advertised snapshot before the model picker narrows the catalog with it. The default returns the current snapshot unchanged; ACP kiro sessions re-probe a suspect snapshot that would hide a row and raise `EntitlementRevalidating` on a deadline miss. `AcpProvider` forwards only when it wraps an `LLMProvider` (the shared-runtime path); on the dedicated `AcpClient` transport it re-probes itself through `AcpClient.refresh_available_models` when a catalog row would drop, rate-limited by `_picker_probe_at` and bounded by the read deadline (raising `EntitlementRevalidating` on a miss). See [model-selection](../common/model-selection.md). |
| `steer` / `supports_steer` / `last_steer_monotonic` | The steer extension. Non-implementers answer `-32601`, so `supports_steer` must be honest. |
| `supports_refusal_steer` | Whether a deny notice steered mid-turn reaches the refused turn's model; gates deny-notice steers. Narrower than `supports_steer`. Default `False`. |
| `steer_needs_loss_recovery` | Whether a steer the provider accepted can still be dropped (codex drops injected text when a later approval is denied or the turn is cancelled); when `True`, `AcpProvider` refuses wrapper steers and only a caller that keeps and requeues the text may steer. Default `False`. |
| `has_active_turn`, `has_unfinished_turn`, `wait_turn_done` | Turn-state probes the session layer reads before reusing a process. |
| `is_session_sharing_eligible` | Whether one process may host multiplexed sessions. |
| `manual_compact_unsupported_backend`, `compaction_self_managed`, `compaction_unmanaged_backend`, `harness_managed_compaction_backend`, `mcp_config_hot_reload`, `uses_kiro_identity_store` | Capability answers, each defaulting to the safe value so a Kiro path never needs a `hasattr` probe (harness-parity H14). The three compaction answers are deliberately separate questions, because a backend Crew cannot hand `/compact` to is one of three things: the harness bounds its own context (`compaction_self_managed`, default `True`), nothing bounds it and Crew recycles (`compaction_unmanaged_backend`, default `None` — the one destructive answer, so it is granted by `ACP_BACKENDS_CONTEXT_RECYCLE` membership and never by exclusion), or nobody has established which, in which case Crew declines and logs the gap. Collapsing them told a deepseek user their harness summarizes on its own, which it does not. `harness_managed_compaction_backend` (default `None`) is asked only by the threshold: it names a harness that summarizes on its own even when Crew can also serve its manual `/compact` (KAS), so the threshold leaves it to its engine. |
| `member_capabilities_supported`, `loaded_capability_template` | Full member-spec support defaults to false and is granted only by `ACP_BACKENDS_MEMBER_CAPABILITIES` membership (harness-parity H6); the observed loaded template defaults to empty. Only a dedicated Kiro or KAS runtime with a confirmed active template provides evidence; the session layer also validates the saved version and MCP registration report before showing applied. |
| `billing_stats`, `child_fidelity_aware` | Accounting and subagent-fidelity reporting. |

## LLMEvent (`providers/base.py`)

Provider-agnostic event dataclass (aliased from `AcpEvent`). The table below is
not exhaustive; the `EVENT_*` constants in `acp/types.py` own the kind list:

| Kind | Description |
|------|-------------|
| `text_chunk` | Text output from agent |
| `thinking_chunk` | Extended thinking |
| `tool_call` | Tool invocation |
| `tool_result` | Tool output |
| `permission_request` | Tool approval request (ACP only) |
| `complete` | End of turn |
| `compaction_status` | Compaction result |
| `clear_status` | Clear display |
| `agent_switched` | Agent mode changed |
| `mcp_oauth_request` | MCP server needs OAuth (has `server_name`, `oauth_url`) |
| `mcp_server_initialized` | MCP server ready after OAuth (has `server_name`) |
| `mcp_server_init_failure` | MCP server OAuth/init failed (has `server_name`, `text`) |

Terminal events also carry `synthetic_completion`. It is false for a provider's
raw result frame and true when Kiro Crew fabricates a compatibility terminal
because the result frame never arrived; consumers that account completed work
must require the raw form.

## AcpProvider (`providers/acp.py`)

The ACP provider carries its owning session key through the `session_key`
argument of `AcpRuntime.create_session` and `load_session`. The same key names
the session's broker stubs and passes through the harness's session extras;
KAS uses it when projecting native managed MCP servers on creation and resume.
The key is not read from the agent's editable environment and does not enable
member-DM control tools. Kiro and Codex accept this common harness argument
without adding custom-agent payloads; their session extras remain empty.

For KAS, `start` completes only after the active agent's required
managed MCP servers and tool exposure are ready. A failed or timed-out
initialization surfaces an error before any prompt; it does not return a
tool-less session. The session-scoped barrier and its budget are specified in
[acp-client.md](acp-client.md#kas-managed-mcp-readiness).

The one concrete provider. It spawns a long-lived harness subprocess — by default
`kiro-cli acp --agent <name>` — and speaks JSON-RPC 2.0 over stdio.

`AcpProvider`'s own behaviour lives in `providers/acp_*.py` owner modules, one
responsibility each. A moved member's body is a module-level function there whose
first parameter is `self` (annotated `AcpProvider` for type checkers only), and the
class binds it by name under one comment header per owner (`change_effort =
_acp_model_effort.change_effort`, `staticmethod(...)` for `_to_llm_event`). Every
member therefore stays an attribute of the one class, so a patch of a method on the
class or on an instance reaches every caller, an `AcpProvider.__new__` build and a
`MagicMock(spec=AcpProvider)` see the same members, and the session's state stays on
the instance; each owner's docstring names the instance fields its members write and
read. A bound member's `__qualname__` and `__module__` name its owner (nothing reads
them), and `typing.get_type_hints` on one needs `AcpProvider` passed in (`localns`).
`_ACP_PROVIDER_OWNERS` lists the owners lowest layer first: an owner imports only
owners listed before it. Owners log under the `kiro_crew.providers.acp` logger, never
import `providers/acp.py` at run time, and read their globals in their own
namespaces: `providers/acp.py` keeps binding every name it imported, serves each moved
module-level helper its own code does not read (`_write_cli_overlay`,
`_RESUME_MAX_ATTEMPTS`, and the rest of `_SERVED`) from the owner that defines it, and
a write through it reaches every owner binding the same object
(`kiro_crew._facade_mirror`). `test/test_acp_bindings.py` pins the member inventory,
the binding and the reach.

| Provider owner | Owns |
|---|---|
| `providers/acp_cli_overlay.py` | the workspace `cli.json` overlay a kiro-family spawn reads: the effort and Tool Search writers, the effort clear and the recovery read, and `_apply_effort_overlay` / `_apply_tool_search_overlay` |
| `providers/acp_model_effort.py` | the model and reasoning effort a session runs at: the picker's advertised-model revalidation, `set_model`, and the effort vocabulary, resolution, live push, rollback and clear |
| `providers/acp_runtime_start.py` | the cold start of a runtime-backed session: spawn, resume past a stale native session lock and the fresh-session fallback, the configured model pin, the startup telemetry, the session identity handed to the runtime, and the start-time check for an MCP server that gave the session no tools |
| `providers/acp_lifecycle.py` | the start and shutdown sequence, the registry claim on a derived run work directory and its reclaim, and the native session-file cleanup |
| `providers/acp_turns.py` | turns on the inner client: prompt and slash-command streams and their event translation, manual compaction and the wait for its result, cancel and steer |

`AcpProvider` keeps its constructor, its properties and class defaults, `approve_tool`
(the transport approval floor keys it on this path), `is_process_alive` and
`process_instance` (the pid-reader ratchet counts them), and the thin delegations to
the inner client; `is_claude_backend` and `provider_label` stay module functions of
`providers/acp.py`. New `AcpProvider` behaviour goes to the provider owner whose row
names its responsibility, bound in the class under that owner's header.

**The backend seam:** `AcpProvider`/`AcpClient` take an `acp_backend` id from
`ACP_BACKENDS_KNOWN`; the empty id denotes kiro-cli. Construction rejects an id
outside that registry, so a value that falls through every identity check cannot
spawn kiro-cli under a foreign label. Which registered ids an operator can select
is the separate, live `BASELINE_SELECTABLE_BACKENDS` set in
`agent_sdk/backends.py`, not a duplicated list in this file. Binary resolution and
config isolation per backend live in [`acp-client.md`](acp-client.md); do not add
a second provider or a provider-level selector (see `AGENTS.md`, "Harness parity").

Adding an id to `ACP_BACKENDS_KNOWN` also obliges a frame-replay corpus under
`test/fixtures/acp_frames/<id>/`; the requirement and what it buys are stated once,
in [agent-host-contract.md](agent-host-contract.md).

**Key APIs:**
- `start()` → `AcpClient.ensure_ready()` (spawns process, handshake, session/new)
- `stream()` → maps events from `stream_events()`; `allow_image=False` makes the turn text-only (see [acp-client](acp-client.md#image-support))
- `stream_command()` → native slash command execution
- `approve_tool()`/`reject_tool()` → JSON-RPC response
- `context_usage_pct()` → reads `last_prompt_stats.context_pct`
- `context_window_tokens()` → reads `last_prompt_stats.context_window_tokens` (the real served window from `usage_update.size`, 0 if unknown). Used by the dashboard token text instead of re-deriving the window from the model id. A mid-session `set_model` (live switch on both `AcpClient` and `AcpSessionHandle`) rebases these stats via `AcpPromptStats.rebase_to_window`: the window is re-derived from `model_registry.model_window` (0 on a registry miss), `context_used_tokens` is kept, `context_pct` is recomputed and clamped, and `context_tokens_from_usage` is cleared so the next metadata `contextUsagePercentage` can backfill against the NEW model instead of being gated forever by the old model's `usage_update`. The dashboard model-switch endpoint then broadcasts one `context_usage` WS event with `reset: true` (both live-switch and session-reset paths, single and bulk), which lets the frontend reducer replace or delete its stored per-slot token counts — per-turn events without `reset` never delete. The post-compaction pct-0 broadcast carries the same flag.
- `compact()` → sends `/compact` via `send_command()`. The **dashboard's** manual `/compact` gates on `ACP_BACKENDS_COMPACT` first, as a pre-acquisition local command: the live session's `manual_compact_unsupported_backend` capability property (declared on the `LLMProvider` ABC with a `None` (supported) default per harness-parity H14, answered by the ACP implementations from set membership) is read when the session's provider is positively alive, else for the backend `select_provider_backend` would build (the member backend for a member session, otherwise `agent.acp_backend`) — so a refused `/compact` behaves as if the turn never started (no session created, no Slack OPTIONS expired, no one-shot turn state consumed). The reply is informational — the backend manages compaction automatically, mirroring the `cc_managed` relationship — not an error: kiro-cli answers the prompt with `_kiro.dev/compaction/status`, claude-agent-acp compacts natively in-prompt, and codex-acp intercepts the prompt as the `compact` command it advertises and reports the compaction as a `tool_call` pair marked `_meta.contextCompaction` (`_dispatch.parse_codex_compaction_update`), and on KAS `AcpSessionHandle.prompt` sends the engine's own `_kiro/session/compact` verb in place of the prompt (as prompt text, the KAS model only writes a summary and the context does not shrink). That request's `{success}` answer ends the turn and the turn carries a compaction status: the engine's `summarization_completed`, else a synthesized `completed` (nothing to summarize, meter left alone) or `failed` (refused while a turn or another compaction runs). A backend outside the set would strand `wait_for_compaction()` for the full `COMPACT_WAIT_TIMEOUT_SECS` (#7800). The **auto-compact** path consults the same capability from the compaction gate ladder (`session_compaction._compact_unsupported_backend`) and then takes one of THREE arms, because a backend it cannot dispatch to is not one thing. A member of `ACP_BACKENDS_HARNESS_MANAGED_COMPACTION` (KAS, read through `harness_managed_compaction_backend`, so it declines here even though its manual `/compact` is served) declines with `"compact_unsupported"` before the compaction task is scheduled, so no `/compact` is dispatched and the turn semaphore is never acquired — an ungated dispatch stranded the status wait for the whole `COMPACT_WAIT_TIMEOUT_SECS` while HOLDING that semaphore and then recycled the session (#7812) — and declining costs nothing there because its `summarization_completed` frame resets the meter. A member of `ACP_BACKENDS_CONTEXT_RECYCLE` (deepseek) is **recycled** instead, via `_recycle_unmanaged`: no compaction reaches it from either side, so a decline bounds nothing and its context grows into the harness's own window. That arm falls THROUGH the decline rung rather than returning from it, so `unconfirmed`, `in_progress` and `cooldown` still run first — an ambiguous reading must not spend a recycle. A backend in NEITHER set declines and is logged at WARNING naming the missing membership, because ending a conversation is not something a harness earns by never having been classified; `compaction_self_managed` is what tells that case apart from KAS's. The callback reports which arm ran (`compacted` / `recycled` / `restarted_uncompactable` / `cancelled`, plus `waiting_for_subagents` during a hold) so a surface cannot announce a summary that never happened, and only `restarted_uncompactable` may say the backend cannot compact — a kiro-cli session whose in-place `/compact` merely timed out reaches `recycled`. `cancelled` means a user Stop ended the compaction: the cooldown is armed and the session is not recycled. Every context restart — a failed in-place `/compact` and an uncompactable restart alike — is first HELD while session-sharing sub-agents still run on the parent's process, because shutting that process down would end them unreported (`CompactionCoordinator._await_cotenants`): the callback reports `waiting_for_subagents` with `success=False`, the hold re-checks every `cotenant_poll_secs` (the manager's per-key completion event is shared, so another waiter may release it), and past `cotenant_wait_secs` only those runs are stopped with the ordinary per-run `SubagentManager.cancel`, bounded and shielded, so each still reports "stopped" into the conversation, which carries on after the restart (the parent-end helper would mark them out of delivery). A user Stop during the hold ends it: the stale restart is abandoned, the sub-agents are not stopped, and the compaction settles `cancelled` (cooldown, no recycle). Channel messages queued behind the held turn are not dropped with the old session: `CompactionCoordinator._restart_held` starts the successor first (past the recycling marker, holding its lease), moves the old session's live queue into the successor's real queue, then recycles the old one and wakes each entry's channel drain before releasing the lease, so queue verbs, teardowns and ordering behave as for any queue. Slack registers a drain for this (`_drain_slack_queue`). If the successor cannot start, the old session, its queue and its resume id are kept and the failure cooldown applies. A report that waits on the parent during the hold cannot reach the old process: every waiting acquire re-validates the session's identity, so it lands on the fresh session. The **messaging-surface** `/compact` commands (Slack, Telegram, Discord, Webex, Teams, Feishu, iMessage, WeCom, Weixin and WhatsApp) gate on the same capability through `messaging.commands.compact_unsupported_backend` before dispatching, answering with `compact_unsupported_reply` (translated on the Chinese-language surfaces, plain-voiced on iMessage and WhatsApp); their context-threshold notices decline silently on such a backend — no forced hard-threshold compaction to run, and no soft nudge whose `/compact` advice cannot work (#8156) — with one exception: a member of `ACP_BACKENDS_CONTEXT_RECYCLE` is WARNED once, before its session is restarted, with a message that offers `/new` instead (`messaging.commands.context_recycle_warning`, `context_recycle_warning_zh` on the Chinese surfaces). That warning is keyed to the session's live `session.autocompact_pct` rather than the channel's `soft_threshold_pct` — it fires in `[threshold - CONTEXT_WARN_MARGIN_PCT, threshold)` (`recycle_warning_due`), because the channel soft default (80) sits above the recycle default (70) and a warning keyed to it would land after the restart — and the per-conversation latch is cleared outside that band so the next fill after a restart warns again (#11948). Gating covers only command dispatch — KAS auto-summarization frames keep mapping to compaction status.
- Dashboard manual `/compact` while conversation-history replay is still pending (the replay lease is armed and the provider session holds nothing yet): it answers at once with `COMPACT_REPLAY_PENDING_NOTICE` ("Nothing to compact yet: the restored history is delivered with your next message."), awaits no compaction status, does not re-arm skills context, and leaves the replay lease armed for the next prompt.
- `cancel()` → sends `session/cancel` notification
- `supports_effort()` / `change_effort(level)` / `clear_effort()` → reasoning-effort control (see below)
- `is_alive()` → `AcpClient.is_responsive()` (600s stale threshold)
- `is_process_alive()` → OS-level process check

**Reasoning effort** (Opus/Sonnet/Fable **and GPT-5.x** — shared vocabulary in `effort.py`: levels `low|medium|high|xhigh|max`, resolution via `resolve_effort_for_model` with priority slot-override > workspace default > None). Capability authority is per harness. For kiro and claude the registry answers (`model_supports_effort`). For members of `ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION` (pi, deepseek, goose, opencode) the session's advertised config option answers `supports_effort` (False when the advertised list contains only `off` — an off-only option signals no real thinking level; for every member `_sync_effort_levels` admits the advertised levels to the validation set through `register_reasoning_effort_values` and never replaces the global fallback order, which a slot whose provider advertises no effort option of its own, kiro-cli, is served; after a live `set_model` the option read is the one the `model` write's own `session/set_config_option` result carries, which `AcpClient.set_config_option` stores as `AcpSessionHandle.set_config_option` does, because the `config_option_update` that precedes it is only buffered by `_wait_for_response`) and the allowed levels: `_resolve_effort` passes `levels=` (the advertised list) and `normalize=` (`effort_config_option_value`, the backend's spelling of a level) to `resolve_effort_for_model`, so the fold runs before the advertised-list filter and before the write. A Crew level a member has no spelling for (deepseek's `medium` and `xhigh`) is hidden, not mapped: `AcpProvider.accepts_effort_level` answers False for it, the dashboard effort route refuses it with a 400 `effort_level_not_offered` before anything persists, `change_effort` raises on it, and `_set_effort_config_option` writes nothing for it and walks its step-down over advertised rungs only, so every value pushed to a member is one that session advertised. The registry capability is a conservative allowlist of known-capable families (`opus`/`sonnet`/`fable`/`gpt`, minus a hard `haiku` exclusion), verified against kiro-cli 2.12/2.13 over ACP — kiro rejects `/effort` on the other third-party models (deepseek/minimax/glm/qwen/auto) with "Effort configuration is currently not available on <model>". A new model family lands as unsupported until confirmed (safe default: the slider hides). Applied via a workspace `cli.json` overlay at `<work_dir>/.kiro/settings/cli.json` → `chat.modelDefaults.<model>.<key>.effort`, written before every spawn (`_write_cli_overlay`) and recovered on init (`_read_cli_overlay`) for server-restart resilience. The `<key>` sub-object is **family-specific** (`effort_settings_key`): `output_config` for Claude models, `reasoning` for GPT models — kiro silently ignores the wrong key, so a mismatched shape would survive a live push but drop on respawn. `_write_cli_overlay` removes stale effort from the other family key while preserving unrelated settings; `_clear_cli_overlay_effort`/`_read_cli_overlay` sweep both keys. Every in-product writer of that file takes the shared `.kirocrew-cli-settings.lock` sidecar (see [acp-client](acp-client.md)), under one of TWO ceilings: a native launch takes the startup one (it cannot wait, and a launch that loses the lock falls back to authored agents), while an operator action takes a ceiling far above projection's own sub-second critical section. `_clear_cli_overlay_effort` is an operator action, so it runs through `asyncio.to_thread` — waiting that long on the loop would freeze every session — and losing the lock there means a stuck holder rather than routine contention. The WRITE side is the same shape and gets the same treatment: `change_effort` applies the overlay off-loop under the action ceiling and refuses to push live at all unless it persisted, rolling the per-model override back and raising, because the file is what a respawn reads — reporting a live change over a write that did not land promises a level the next reset undoes. The pre-spawn overlay writes keep the startup ceiling on the loop: a launch cannot wait, and a launch whose overlay is missing still pushes at spawn. That is what keeps its answer two-valued: a failure that happened by design would need a third state for "nothing changed", and one that means something is wrong does not. It returns whether the file stopped naming the model: an absent file, an absent entry and a malformed file are all successes (`_read_cli_overlay` reads a malformed file as `{}` too, so nothing re-seeds), while a lock failure and a read that fails on a file that EXISTS under the held lock leave the level on disk. `clear_effort` answers that case with a THIRD outcome (`None`), not a bool: both bool values make the handler commit the cleared slot value — the reset branch before its teardown, and the success path before its final 200 — so either would show "default" over an overlay that still holds the level, and `False` additionally resets into re-reading that same level. `None` means nothing changed on either side, and the handler answers it with a retryable 409 `effort_overlay_busy` that commits nothing and resets nothing. `change_effort` has no such case: it raises, because a change that did not persist is a failure rather than a no-op. Clearing to a resolvable workspace default runs under the same write-first discipline: it applies the overlay off-loop under the action ceiling and pushes `/effort` only once that landed, because a live push over a write that did not persist reports a default the next spawn replaces with the level just cleared — a failed write there restores the popped override and answers `None` as well. `clear_effort` must not report a clear on that answer — the level is still on disk and `_read_cli_overlay` re-seeds `_effort_per_model` at construction, so the session reset a clear asks for would re-apply it. It restores the popped entry and logs a warning naming the model, and returns True — not False: the handler resets the session on False, and that reset would re-read the same level, so the operator would pay a reset and still run the old effort. True here means "no reset needed", which is the true answer when nothing changed on either side; the warning is what asks for a retry. The `change_effort` rollback path restores the level the same way for the same reason, in both of its shapes: the one that clears an overlay its live push never applied, and the one that rewrites the overlay back to the prior level. When either write loses the lock the file keeps the attempted level and construction re-seeds from it, so the map follows the FILE and a warning names the level a respawn adopts — reporting an undo the next spawn contradicts would be worse than admitting the rollback is incomplete. Live change pushes `/effort` with the TuiCommand args form (`send_command(args={"level": …})`). The factory threads `reasoning_effort_override` → `effort_per_model[current_model]`, keyed by the configured spelling; on the claude client path `start` moves that entry to the model the session records after `ensure_ready` (claude records the advertised spelling, e.g. `claude-opus-5-5[1m]` for a `claude-opus-5.5` pin), and leaves it in place when every spelling was refused and the session runs the backend default. For an advertised-option harness the factory carries the level forward and leaves judgement to `_resolve_effort` once `session/new` has advertised the option, so a cold start does not drop it on the registry's answer. When nothing is pinned (the resolved model is empty, as with an explicitly empty `agent.model` or a pin that collapses to `auto`), the factory passes a valid level, or any level on an advertised-option harness, as `unbound_effort` instead of keying it, because no model id exists until the backend answers `session/new`. Every effort read and write keys on `_effort_model`: the pinned model, else the backend-reported `served_model` once known. `_bind_unbound_effort` runs on every (re)start, after the session is ready and before `_apply_initial_effort`. It stores the level under the served model, replacing any level already stored there: a live change moves the unbound level with it, so a stored level that differs can only be one a previous run left in the workspace overlay, which construction seeds the map from on the kiro family, and keeping it would run the session at a level the caller did not ask for. On the kiro family, whose level otherwise rides the spawn-time overlay, it pushes the level live through `change_effort` whenever the overlay does not already hold it, which also writes the overlay for the next spawn. That covers an unpinned kiro-cli, which is launched without `--model` and serves its own `chat.defaultModel` when one is set. Before that call the map holds the level the session was spawned with (the overlay's, or none), so a push kiro-cli refuses rolls back to that level rather than leaving the requested one in the map and the overlay while the session runs another. kiro-cli reports `currentModelId` `auto` for an `auto` session, and `served_model` filters that to `""`, so such a session still offers no effort, as before. A level the serving model does not take, or one with no served model at all, is logged as not applied once per provider. `clear_effort` drops the unbound level so the next restart does not bring it back, and a successful `change_effort` replaces it. Otherwise, when a valid requested effort cannot be threaded on a cold start (the resolved model is not effort-capable) the factory's gate logs one warning naming the level, the session, and the resolved model — reporting its own drop decision on surfaces that construct a fresh provider, an explicit `reasoning_effort_override` always warns (a caller's own dropped request is the event the gate exists to surface), while a drop sourced only from the config default (`agent.reasoning_effort`) is deduped once per (model, level) for the factory's lifetime so one static configuration fact does not repeat on every construction. A `reasoning_effort_override` on a warm-pool claim is applied post-claim via `provider.change_effort` (updating `_effort_per_model` and the `cli.json` overlay write) rather than bypassing the pool, recovering pool-hit startup latency; if the claimed model does not support effort, `change_effort` returns False and a corresponding drop warning is logged. The dashboard handler routes through `change_effort`/`clear_effort` and only resets the session when there is no live provider. Non-effort-capable models persist the slot value without a live apply or reset.

**MCP Tool Search** (kiro-family backends — see https://kiro.dev/docs/cli/mcp/tool-search/): loads MCP tool specs on demand ("search-and-call") instead of sending every tool definition each turn, keeping the context window clear when many MCP servers are configured. Gated by the `agent.tool_search` config toggle (default **on**; auto-surfaces as a Settings toggle since the schema is generated from the dataclass).
- **kiro-cli (Rust engine):** applied via the **same** workspace `cli.json` overlay used for effort (`<work_dir>/.kiro/settings/cli.json`), written deterministically before every spawn and on each restart by `_write_tool_search_overlay` (called from `AcpProvider.__init__` and `start()`), scoped to `ACP_BACKENDS_TOOL_SEARCH_OVERLAY`. The engine activates deferral only when the agent's `tools` also grants `tool_search`, so on this channel the loader invariant is the engine's. When enabled it writes the flat keys `toolSearch.enabled=true` plus `toolSearch.minPct`/`toolSearch.minTokens`, taken from `agent.tool_search_min_pct` / `agent.tool_search_min_tokens` (defaults `5` / `50000`, mirroring kiro-cli's own thresholds; clamped to 0-100 and >= 0, non-numeric falls back to the default); when disabled it writes `toolSearch.enabled=false` and drops both thresholds.
- **Why the thresholds are not forced to 0:** deferral costs a round-trip — a deferred tool's spec is absent from the model's tool list, so the first direct call fails with `A tool with the name '<name>' does not exist` and has to be recovered with `tool_search`. That only pays once the specs are genuinely large, which is what the thresholds express (kiro-cli defers when EITHER is exceeded). Setting both to `0` restores unconditional deferral for operators who want it. The thresholds are always written **explicitly** rather than omitted, so a value already in the file is replaced rather than silently kept.
- Writing both `true` and `false` makes the Kiro Crew toggle authoritative over any value in the user's global `~/.kiro/settings/cli.json`. The write is merge-safe with the effort `chat.modelDefaults` keys in the same file.
- kiro-cli reads workspace settings only from `<cwd>/.kiro/settings/cli.json`, so the overlay cannot move out of the work dir. When the work dir is a git work-tree top, the shared overlay lock adds the overlay and its lock to the repository's local `info/exclude`, keeping the user's `git status` clean (see [acp-client](acp-client.md)); an already-tracked `cli.json` is not hidden.
- **KAS:** never opens `cli.json`; it reads the setting from the ACP `initialize` request, `clientCapabilities._meta.kiro.settings.toolSearch` — a process-wide channel filled by `AcpRuntime._handshake_client_capabilities`, entered from the spawn path only when the harness answers `client_meta_settings` (the harnesses in `ACP_BACKENDS_CLIENT_META_SETTINGS`; every other host reads its constant with no new step), from the same `agent.tool_search*` values (`agent_sdk/tool_search.py`). KAS defers **every** MCP spec when told to and does not check that a loader is mounted, so the client keeps the invariant: `enabled` is sent as `true` only when the spawn agent's spec grants `tool_search` — named in `tools`, or via `"*"` / `@builtin` — and does not take it back in `excludedTools`; it is sent as an explicit `false` otherwise — never omitted, so a host default cannot flip a session into "deferred, unreachable". The spec judged is the one the KAS projection puts on the wire (`AcpRuntime._projected_spawn_spec`: the freshness gate's snapshot for a derived agent, else `acp/harness/kas.resolve_projected_spec`, the call the projection itself makes, after the same `ensure_agent_materialized` self-heal the projection runs) — so a project checkout's `.kiro/agents` spec is judged exactly when it is the one projected; an unreadable spec grants nothing. The setting is process-wide, so every later `create_session` on a deferral-enabled process judges the payload it is about to send: a projection that grants no loader — another agent, or the same agent whose spec has since lost the grant — is refused with `AcpToolSurfaceBindingError` (an `AcpWorkspaceBindingError`), which the run-runtime caller already answers by giving that session a runtime of its own. The check lives in `AcpRuntime._kas_custom_agents`, the projection seam both `create_session` and `load_session` go through, so a resumed session is judged the same way. It is deliberately one-directional: a process whose handshake sent `enabled: false` may serve a later session whose agent does grant the loader — that session simply runs with full tool specs, today's behaviour and never a loss of tools — whereas the reverse pairing loses every MCP tool, which is the only shape worth a refusal. Every KAS-capable runtime constructor threads the same resolved settings — the foreground provider, its resume-respawn fallback, the companion-runtime kwargs mirror (`session_allocation._collect_parent_runtime_kwargs`, reading the parent's `LLMProvider.tool_search_settings` capability — default `None`, answered by `AcpProvider`; harness-parity H14) and the background runtime (`session_background`) — so no KAS process is left to the host's default. KAS does not honour the two thresholds as an activation floor — deferral there is all-or-nothing, with `toolSearch.neverDefer` as its keep-list — but they are forwarded verbatim so the setting means one thing.
- **Non-kiro-family backends** — no-op on both channels. `_apply_tool_search_overlay` returns early when the backend does not read the overlay, the handshake channel is filled only for members of `ACP_BACKENDS_CLIENT_META_SETTINGS`, and both are silent when no toggle value was threaded in (`tool_search is None`).
- **Native-resume compatibility:** for a direct dashboard turn with Tool Search enabled (dashboard session identity and no resumable linked-channel identity), the kiro backend does not use `session/load`. Before provider acquisition, dashboard chat resolves the slot's dedicated Slack field unconditionally and uses the channel-neutral `SessionMap.mirror` link only when `mirror_accepts_inbound` is true. Thus inbound-capable Telegram/Discord links remain distinguishable when they reuse a `dashboard:*` key after restart, while outbound-only iMessage/WhatsApp mirrors still take the direct-dashboard recovery path. A loaded transcript can return without Tool Search's activated schemas: `tool_search` reports a match, but the next inference still cannot invoke that tool. The provider instead creates a fresh native session and sets `_history_replay_needed`, so `SessionManager` marks conversation replay pending against the rebuilt tool registry. That decision is the pure module-level `resume_takes_tool_search_replay` in `agent_sdk/tool_search.py` — below both callers, because the dashboard may not import the ACP layer (`scripts/check_agent_sdk_boundary.py`) — (Tool Search on, `ACP_BACKEND_KIRO`, no channel identity, a key `telemetry_channel_of` classifies as `dashboard`) — the ONE definition, read by `_start_kiro_runtime_impl` once a resume sid is in hand and by the dashboard's resume prefetch (`chat_runner._eager_spawn`) BEFORE any runtime exists: a speculative load the provider replaces this way can only come back `resumed=False` and be refused after a full spawn and teardown, so the prefetch logs `left to first turn (tool-search replay)` and spawns nothing, and the first real turn takes the fresh-session path described here. On that turn the replay lease also outranks a `resumed` observation: `chat_runner._run_chat` treats the provider as holding no history whenever the lease is armed (`_replay_pending`), so the turn builds the full first-turn context with the replay and never takes the slim-resume path that tells the model its history was restored. Only this direct-dashboard compatibility branch also makes `defer_replay_sid_promotion` true; `SessionMap` keeps the prior full-history SID durable while that lease is pending, allocation does not publish the fresh SID yet, and `close_all()` refuses to overwrite the retained mapping while `provider_switch_replay` remains armed. Generic `session/load` recovery still requests history replay but publishes its fresh SID immediately because non-dashboard dispatchers do not own the dashboard settlement contract; a later channel restart therefore resumes the recovered native transcript rather than the stale pre-recovery SID. Replay settlement runs from the dashboard turn's `finally`, so exceptions, task cancellation, early returns, and synthetic terminals cannot bypass it. A landed, non-synthetic replay-bearing `end_turn` promotes the fresh SID, as does confirmed `/clear` after native history deletion. Cancellation, incomplete streams, and every other non-committed terminal leave the prior SID in place and re-arm replay, so a second gateway restart cannot strand a slash-only or discarded transcript. That lease drives every replayable dashboard session-start prompt block (history, ContextBuilder, member context, folder, persona, and context telemetry). `AgentSpawn` hooks remain keyed to the actual provider spawn so script side effects execute once; a slash-first turn does not re-fire them during replay. Non-destructive native slash commands bypass `ContextBuilder` and leave the lease intact; a confirmed `/clear` consumes it at `EVENT_CLEAR_STATUS` so later replay cannot restore deleted history. Authorization, shutdown, or pre-dispatch Stop aborts preserve it. Async stream creation is not acceptance: a replay-bearing non-slash turn records acceptance in runner-local state only when its stream yields the first provider event, while the shared lease remains armed throughout the in-flight turn. Empty streams and pre-output failures therefore retain replay without settlement, and a concurrent shutdown can observe only the still-pending old SID. Final settlement synchronously promotes the fresh SID and consumes the lease only for a landed, non-synthetic, non-empty `end_turn`; every empty-response verdict is unlanded for replay durability, including the terminal give-up rung when retry budget is exhausted or auto-continue is disabled. If an accepted turn ends cancelled, the still-armed lease carries forward because kiro-cli discards that turn; the next prompt receives the full older replay plus its cancelled-turn preamble. This trades the native resume latency win for a usable dashboard tool surface without losing prior conversation or undoing an explicit clear. Setting `agent.tool_search=false` keeps dashboard-native `session/load`; a dashboard-keyed resumable channel turn and every channel dispatcher remain on native resume regardless of Tool Search.

- **Resume guard:** `session/load` (resume) is only attempted when Tool Search is disabled and the prior session transcript exists on disk (`~/.kiro/sessions/cli/<sid>.json`). A stale persisted sid with no transcript falls back to `session/new`, preventing a fresh conversation from replaying old turns (which inflated base context).
- **Working dir:** `AcpProvider.cwd` overrides the `LLMProvider` ABC default so `session_map` persists the real workspace path. AcpProvider's work_dir lives on the inner client (`_client._work_dir`), so a consumer reading `_work_dir` off the provider gets `""` for every ACP session; `provider.cwd` is the member to read. It reports the directory THIS session was bound to, not the runtime's: `AcpRuntime` records `bound_cwd` on the `AcpSessionHandle` at `session/new` and `session/load`, `AcpSessionProvider.cwd` reads it off the handle, and `AcpProvider.cwd` forwards that, falling back to `_client._work_dir` only before startup, on a raw `AcpClient`, or for a handle with no recorded directory. A shared runtime hosts sessions opened against different projects, so the runtime's own directory would make reuse validation evict a live session that bound elsewhere.

## Config (`config/loader.py`)

```json
{
  "agent": {
    "provider": "acp",
    "model": "auto"
  }
}
```

- `agent.provider` is fixed to `"acp"` (enum `["acp"]`); the provider is not a choice.
- `agent.acp_backend` is the harness choice, resolved through `agent_sdk.backends.resolve_selected_backend` (the top-level `acp_backends` module is a re-export shim kept for existing call sites).
- `create_provider_factory()` returns a `Callable` that builds an `AcpProvider` for the resolved backend.

An agent spec's model is consumed by kiro-cli before Kiro Crew reaches
`session/new`, so the live-session entitlement guard cannot diagnose a wrong
wire spelling at spawn time. Agent create/update validate a pin before
persisting it: they reuse the role-model validator for advertised ids and
`model_registry.acp_id_correction` for the offline positive case where the
registry recognizes a non-ACP spelling and can name its ACP id. Unknown ids are
allowed because they may be valid regional or newly released ids; empty and
`auto` continue to defer. Doctor applies the same correction audit to every
discoverable user- and project-scoped spec.

## MCP Server Registration

MCP servers are passed directly in the `session/new` params. The managed servers
are the entries of `agent.py:_MANAGED_MCP_SERVERS` (see also
[`docs/architecture/mcp.md`](../../architecture/mcp.md)): `kirocrew-core` and
`kirocrew-cron` are unconditional, `kirocrew-computer` is spec-gated
(`_computer_use_spec_gate`), and the others (`kirocrew-dashboard`,
`kirocrew-work`, `kirocrew-crew-log`) are opt-in per their entry.
User-configured servers from the agent config are merged in.

## SessionManager (`session.py`)

- Provider-agnostic via factory (one provider, `AcpProvider`, over the resolved backend)
- Calls `repair_agent_configs()` on gateway startup and periodically
- Resume: calls `set_resume_session_id()` before `start()`

### Workflow one-shot session teardown is best-effort

`workflows/agent_pool.py::_run_unpooled` (a `ctx.agent(session=...)` named
call, or the identity-cap overflow valve) tears its session down in a
`finally`: `release(key, cleanup=False)` for a named conversation (the turn
lease is returned, the conversation is kept), `destroy(key)` for a one-shot
`wf-unpooled:` key. That teardown is best-effort: a `release`/`destroy`
exception is caught and dropped so it can never replace the step's real
outcome. A successful result is still returned, and the body's own exception
(a provider failure, a `WorkflowScope.validate()` rejection after the step)
propagates unchanged; `CancelledError` is not caught, so a cancel still
propagates after the teardown attempt. The failure is logged at WARNING with
the exception TYPE only, no message and no `exc_info`, because this logger sits
on the task diagnostics path and a session error's text can carry conversation
content or credentials. Pinned by `test/test_workflows_agent_pool_unpooled_teardown.py`.

### Workflow sessions publish their own turn identity

A workflow worker's kiro-cli process has no ambient `KIROCREW_SESSION_KEY`
(`AcpRuntime` does not export one). Its eligible managed MCP elements carry an
ordinary signed per-session token, including when the broker is disabled.
Every workflow send surface also calls
`messaging.identity.publish_turn_identity(sessions, key)` at the same point in
the turn as the chat and channel dispatchers: after `get_or_create` returned the
session, before the prompt is built or streamed. `_WorkflowSessionWorker.send_message`
publishes per turn (a hard reset respawns the process, so the pid can change) and
`_run_unpooled` publishes for a named `session=` chain and for the identity-cap
overflow session. The key published is always the worker's own
(`wf-pool:{run}:{n}`, `wf-unpooled:{run}:{n}`, the named key, or the
`WorkflowScope.worker_key` hash), never the parent chat's. The writer keeps its
fail-safe contract: a session without a pid, or a fake without `get_pid`,
skips publication and never fails the turn. Pinned by
`test/test_workflow_memory_backend_reset.py::test_workflow_worker_publishes_identity_before_mcp_http`
(real child transport) and `::test_unpooled_paths_publish_identity_before_prompt`.

## Subagent Approval Mode Inheritance (`subagent.py`)

Subagents inherit the global `approval_mode=auto` config as a final fallback when:
1. No parent session key exists (spawned independently), OR
2. Parent session key exists but the session is no longer in the store (garbage-collected)

If the parent session is alive but returned no policy, deny-by-default applies — the session is intentionally non-auto. This ensures subagents spawned from dashboard sessions still get auto-approval even if the parent session is GC'd before the subagent executes.

## Automatic recovery

Provider-level recovery mechanisms that fire automatically without user intervention:

**Interactive transient-5xx retry:** the interactive dashboard/Slack `chat_runner` stream loop retries a transient backend 5xx (InternalServerError / DispatchFailure / ConnectionReset, JSON-RPC `-32603`) through the shared `llm_helpers` transient classifier + backoff, **without** resetting the still-alive session. Auth/validation errors are excluded (fail-fast); on retry-budget exhaustion a clean error surfaces on a still-resumable session. The unattended `stream_and_collect` path uses the same classifier, but its same-model and fallback-chain retries replay the original prompt only while no assistant text or tool call has occurred across any attempt. Any fired tool call makes a later transient error terminal on that path, including when the tool completed without producing text.

A transient 5xx that arrives *after* the turn already emitted output (the `_turn_emitted` guard is set once any assistant token streams or a tool call fires) does not drop the turn: it **RECOVERS ONCE**: the streamed partial is preserved as a finalized assistant message, a brief recovery notice is appended, and a *continue* instruction (not the original prompt) is re-queued onto the SAME live ACP session — which still holds the interrupted turn's context (original prompt, streamed partial, and any completed tool results) — so the model resumes from where it stopped rather than restarting. The recovery is one-shot per genuine user turn: the allowance is consumed only when a recovery is actually enqueued and is refreshed at the start of the next real user turn, never on the synthetic recovery turn, so a repeated post-token 5xx during recovery surfaces a clean error instead of looping. When Stop is active or the turn is nested (`_prompt_depth != 0`) the partial + notice are still shown but nothing is re-queued (the allowance is left unconsumed). This recovery **also applies to turns that already fired a tool call** — an ACCEPTED TRADEOFF (owner decision), rather than failing fast: a mid-stream 5xx is rare, and the continue instruction tells the model to resume and not re-run tools that already completed. A residual double-execution risk remains only for a side-effecting/destructive tool that was still *in flight* when the 5xx hit; the owner accepts that narrow risk in favor of recovering the turn.

**Compaction-failure notice backoff** (dashboard-chat; `dashboard/chat_utils._broadcast_compaction_result`): repeated per-turn compaction failures are collapsed rather than repeated. Per slot, `_compaction_fail_streak` counts consecutive failures and the first `_COMPACTION_NOTICE_SHOW_FIRST_N` (=2) are shown verbatim ("❌ Compaction failed: …"); further failures within the `_COMPACTION_FAIL_COOLDOWN_SECS` (60s) `_compaction_fail_cooldown_until` window are suppressed, and when the cooldown elapses a single collapsed "failed Nx in a row … Consider `/compact` manually" message is shown with `/compact` guidance. A `completed` status resets the streak/cooldown. `acp/client_usage.py:_handle_compaction_status` logs the raw failed-compaction notification params at WARNING (kiro-cli carries no dedicated error field on failure). The reason a failed notice shows comes from ONE reader, `acp/transport_errors.compaction_failure_detail` — a ranked walk of the payload's reason-bearing keys, redacted and capped, falling back to the raw payload shape when none is named — so the notice and the retry verdict (`compaction_failure_is_transient`) read the same view of the frame. The streaming dispatch loops (`AcpClient`, `AcpSessionHandle` and its KAS `summarization_failed` branch) put that reason in the event's `title`, and both `wait_for_compaction()` implementations (`AcpClient` and `AcpSessionHandle`; `AcpProvider` and `AcpSessionProvider` pass the result through) put it in the result's `summary` when the notification's own `summary` is empty — kiro-cli populates `summary` on success but leaves it empty (or `null`) on failure, which otherwise left the manual `/compact` notice at a bare "Compaction failed." while the automatic one named the cause. A `failed` notification whose `summary` is non-empty is returned as-is (redacted), a `completed` result is unchanged, and a payload naming no reason yields the reader's own fallback text ("no reason reported by the agent (raw: …)") on both paths. This is a UX/spam guard only — the underlying compaction still runs every turn on kiro-cli's schedule — and is distinct from SessionManager's proactive auto-compact cooldown.

**Compaction resets — then accurately re-reports — the context meter**: a `completed` `_kiro.dev/compaction/status` drops the stale token stats at the provider chokepoints — `AcpClient._handle_compaction_status` (every dispatch loop plus `wait_for_compaction`) and the mirrored sites in `AcpSessionHandle` (prompt dispatch loop and its `wait_for_compaction` queue-drain path) — via `AcpPromptStats.reset_after_compaction()`: `context_used_tokens`/`context_pct` zero out and `context_tokens_from_usage` clears (so fresh metadata can re-derive instead of being gated by the pre-compaction `usage_update`), while `context_window_tokens` is kept (the model did not change, so the served window still holds). kiro-cli then emits a fresh `_kiro.dev/metadata` with the real post-compaction `contextUsagePercentage` about a second after the completed status (live-probe confirmed), so `wait_for_compaction` grace-drains up to `_POST_COMPACTION_METADATA_GRACE_SECS` (5s) for it on `AcpClient`, `AcpSessionHandle`, and `AcpProvider`'s cached mid-turn result path (which delegates to the inner client via the `AcpSessionProvider` pass-through); the drain only ends on a metadata frame actually carrying a `contextUsagePercentage` (a credits-only frame is consumed but does not end it), re-queues non-metadata frames before any poison sentinel, and lets process death (`AcpError`) propagate; `_backfill_context_window` prefers the **kept served window** over the model registry when deriving tokens from that percentage, since the served size can differ from the static entry (e.g. opus served at [1m] vs a 200K registry row). The dashboard's manual `/compact` path then broadcasts the REAL post-compaction numbers when the drain captured them, and only falls back to `context_usage {pct: 0, reset: true}` (the same contract as the threshold auto-compact callback and the in-turn `_broadcast_compaction_result` chokepoint) when no metadata arrived — the meter then self-corrects on the next turn's telemetry. A failed/timed-out compaction leaves the counts untouched and re-sends them as-is. `_context_usage_payload` treats `used == 0` with a known window as "not measured yet" and omits the token fields, so the unconditional end-of-turn broadcast cannot overwrite a reset with a false "0 / W tokens" claim.

## Installation

Kiro Crew drives `kiro-cli` over ACP — install it per its own docs, ensure it is
on `PATH`, and run `kiro-cli login`. `kirocrew doctor` reports its status.


## AcpProvider: shared-runtime startup

`AcpProvider.start()` branches on the backend. Every shared-runtime branch below
enters the same `AcpRuntime.spawn()` cold-start coordinator (default 2 concurrent
spawn+initialize handshakes per gateway loop); admission is backend-neutral, so an
adapted runtime harness neither bypasses the bound nor changes the Kiro path.

- **A runtime backend (`is_acp_runtime_backend`, i.e. membership in
  `ACP_BACKENDS_ACP_RUNTIME` — kiro, KAS and codex)** → `_start_kiro_runtime()`.
  This spawns an `AcpRuntime` (carrying the provider's sandbox mode, extra env,
  and MCP-gateway overlay/socket), resumes via `runtime.load_session()` when a
  prior transcript exists or otherwise `runtime.create_session()`, applies the
  configured model, and replaces `self._client` with an `AcpSessionProvider`
  (which implements the same interface as `AcpClient`, so downstream callers are
  unchanged). Any failure after `spawn()` kills the runtime so a half-initialised
  session never leaks an orphaned `kiro-cli`.

  This path builds a mirrored host's MCP array through that host's mirror.
  `AcpRuntime._mirrored_session_mcp` answers `None` for a backend with no mirror
  (`providers.mirrors.registry.has_mirror`), so kiro and KAS keep the pooled path
  they always had — byte-identical, no new conditional and no new failure mode on
  the shared construction path (harness-parity H13). For a mirrored backend it
  resolves the broker stubs, mints this session's stub token onto them, and hands
  them to `mirror.session_projection(...)` as `stub_elements` so ONE owner narrows
  both halves of the array: a stub carries the same `name` as the spec entry it
  rewrites, so appending stubs after a projection withheld that name would re-add
  the server as the unrestricted one of the two. `session_key` and `channel_id`
  ride the elements because a codex stdio server starts from `env_clear()` plus an
  allowlist and can learn its session no other way. `permission_surface_owned` is
  False: this runtime authors no native permission file, so a mirror in claude's
  class fails closed here rather than delivering tools Crew's gate cannot see. The
  harness still narrows transports afterwards (`session_mcp_servers`), which is
  the last word on what this session's handshake advertised.

  Two things come out of the projection that are not wire data. The per-tool deny
  set lands on `AcpSessionHandle.spec_denied_tools`, and
  `AcpSessionHandle._deny_spec_disabled_tool` refuses those calls at
  `session/request_permission` — before the event is yielded, so no consumer
  auto-approve and no human is asked to re-decide what the spec settled. It reads
  the call's identity through the same `_dispatch.identified_mcp_call` the
  `AcpClient` path reads, so the restriction cannot hold on one transport and not
  the other. The derived-spec snapshot the array was built from is re-checked once
  `session/new` / `session/load` returns
  (`AcpRuntime._require_unchanged_mirrored_spec`): for a mirrored host the array
  IS the derived spec, so a spec write landing between the build and the host
  consuming it ends the session instead of running restrictions nobody agreed to.
- **A non-runtime backend (not a member)** → `AcpClient.ensure_ready()`, one
  process per session with no shared runtime. The branch is expressed as
  positive membership, not `not is_claude_backend`, so a harness added later
  does not inherit the kiro-family path (harness-parity H5).

`AcpProvider.is_acp_runtime_backend` reads `ACP_BACKENDS_ACP_RUNTIME` directly.
There is no indirection function and no env switch in front of it, so the
FOREGROUND start path and the background path (`session._bg_runtime_backends`)
read the same frozenset — background narrows it further with
`ACP_BACKENDS_SESSION_EVICTION`, for the reason given in `session.md`,
"Multiplexed _bg runtime". Two sites narrow by that set, not one: warm pooled
reuse (`AcpSessionProvider.new_conversation`) reads it for the same reason from
the other direction — running on the shared runtime is what makes a resident
session possible, and only an evicting teardown makes reusing one cheap rather
than cumulative.

That predicate answers ONLY the transport question: which start path a session
takes. It does **not** answer who reads the kiro-family workspace `cli.json`
overlay. Codex runs on the shared runtime, reads no such file, and takes effort
over `session/set_config_option`, so the two sites that keyed overlay work off
the runtime predicate key off `ACP_BACKENDS_KIRO_SLASH_COMMANDS` instead — the
set that owns that question (harness-parity H6). A harness author looking for
"the one gate" is looking at the frozensets themselves; `harness_for()` serves a
host whether or not a capability set names it.

**A provider switch on codex is process-wide, not per session.** codex-acp's
`providers/set` restarts the shared `codex app-server`: it waits on every active
prompt, stops every session's async tasks, restarts the child, then resumes each
session individually — and a per-session resume can FAIL while the restart as a
whole reports success. On a per-session adapter the blast radius is the one user
who asked for the change; on the shared runtime it is every session on that
process, so one chat's provider change is a liveness event for all the others.
Nothing sends `providers/set` per session, and nothing should start.

`AcpProvider.is_session_sharing_eligible` is membership in
`ACP_BACKENDS_SESSION_SHARING` (harness-parity H6), not `not is_claude_backend`:
a capability granted by the absence of one backend is inherited by every backend
added later. It is what `SessionManager.is_session_sharing_eligible()` consults
to decide whether a parent session can host multiplexed subagent sessions. Kiro
and codex are the members.

Membership is about the PERSISTED THREAD, not about a session that stays alive.
Teardown still sends the disposing verb on both members — a subagent session left
resident on the shared process after its parent ends would hold its own MCP fleet
on a runtime nobody is using — and `spawn_continue` re-reaches the conversation by
loading the record the host kept: kiro-cli's transcript under
`<kiro home>/sessions/cli`, or the thread `codex` persists under `CODEX_HOME`. So
the question membership answers is: after this backend's teardown verb, can a
`session/load` still restore the thread?

Codex answers yes, measured on codex-acp 1.11.0 against codex 0.154.0:
`session/close` evicts (the sessionId stops answering), a `session/load` on that
closed id succeeds and the session then answers a question about the first turn,
and the same load succeeds from a RESTARTED adapter process over the same
`CODEX_HOME` — which is the shape a continuation actually takes, since the runtime
that served the subagent is usually gone. `session/delete` archives the thread and
a load afterwards refuses, so release has a verb that genuinely disposes and
`close` is not it. `ACP_BACKENDS_HARNESS_OWNED_SESSIONS` is what carries the
restore: codex resolves a load from the sessionId alone.

KAS answers no, and that is the whole of its exclusion: `_kiro/session/delete`
REMOVES the persisted record, so there is nothing for a load to restore and a
shared subagent would strand `spawn_continue` on `conversation_gone`. A different
gap, owned by whoever gives KAS a non-destroying teardown. The invariants governing what an
added harness may and may not change are in
[harness-parity.md](harness-parity.md).

Native Kiro CLI spawning also prepares a bounded skill discovery view, shared by
the direct client and runtime. Its workspace settings suppress implicit native skill
inheritance; authored mappings stay available to Crew scoped search/list/read.
See [ACP client](acp-client.md#native-skill-startup-views).

### Codex dashboard session mount

For an agent explicitly granted `kirocrew-dashboard`, the Codex mirror rebuilds
its direct launch from the gateway-managed entry and injects that session's
identity. Both creation and resume use this projection. The Codex harness sets
`DISABLE_MCP_CONFIG_FILTERING=true`: codex-acp otherwise drops a session entry
when global Codex configuration declares the same name, leaving an unbound
server in place of the verified mount. Spec-selected launchers
never receive that identity. Disabled, ungranted and per-tool-restricted dashboard
servers remain withheld. A granted gateway broker stub is also admitted through
the same restriction checks; gatewayd verifies its claim and supplies per-call
identity to the managed backend.

On an enforced sandbox, credential-bearing host readers need the broker route:
configure `mcp_gateway.stub_servers` to include `kirocrew-core` and
`kirocrew-dashboard`. Direct children cannot read the gateway credential or SEL
trust root there; the Codex dashboard mount does not relax those masks. Global Codex MCP entries
are not a substitute for this session mount.
