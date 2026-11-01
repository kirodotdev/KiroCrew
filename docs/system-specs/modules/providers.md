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
floor (`ACP_BACKENDS_KNOWN`) and the selectable baseline, and re-exports every
capability set a backend opts into from the `backends_*` owner that defines it. Do not re-describe that seam here —
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

Session Environments (a developer feature preview) chooses where a session's
workspace tools execute; Coder, Fly.io Sprites and boxd are the workspace
providers. Shared helpers live in `session_environment.py` (the start-stage and
remote-workspace records, the local-only tool fence,
`SessionEnvironmentUnavailable`), apart from the LLM provider selector:
placement adds no `agent.provider` value and selects no model. Gateway hosting
and whole-Crew deployment are out of scope.

Configuration lives in Settings > Session Environments, shown by the per-device
toggle under Settings > Developer > Feature Previews (schema in
[config](config.md)): a Coder URL and named profiles, never credentials. Save
writes the section atomically; edits stay local until Save and survive refetches
and rejected writes. Saving starts no compute, removing a profile stops or
deletes nothing external, turning the preview off changes nothing, and an older
gateway shows an unsupported notice. "Verify readiness"
(`POST /api/session-environments/check`, owner-only, saved configuration only)
runs `coder whoami` and `CoderClient.probe` per profile, plus
`git_gateway.login_status` per proxied git host, which runs only status
commands (`gh auth status`, `glab auth status`, `az account show`) and reads
their exit codes with output discarded. A host that falls back to git's
credential helper reports `unchecked` rather than asking the helper, since
asking would hand the gateway the secret; the card says that row cannot be
checked and says to clone a private repository over HTTPS on the gateway so
the helper saves the login (a bare `git credential approve` waits on stdin),
and a `not_signed_in` row names the
sign-in command to run on the gateway. Under the `strict` tier
`git_gateway.git_readiness` runs no command and reports every host `withheld`,
and the card shows one row saying no gateway login is shared. It touches no
workspace. The Sprites check lists at most one sprite per profile (proving its
token) and the boxd check runs one `boxd machine list` (proving the CLI's
login); neither creates or wakes anything. "Manage"
(`GET /api/session-environments/workspaces`, owner-only) lists registry rows
without a Coder command.

### Placement and the tool fence

A session environment is a disposable worker, never a second crew. The
conversation, memory, lessons, skills, approvals, tool gate, audit log and
engine stay on the gateway; the guest holds only the workspace, a tool server
for it and no model credential, so losing a worker loses at most uncommitted
changes. The whole-Crew Fargate lane (`cloud/fargate_engine.py`) is not a session
host, and engine-in-guest was rejected: both put memory and approvals out of the
gateway's reach.

The guest runs `remote_session/workspace_tools.py`, a stdlib-only MCP server
(`read_file`, `write_file`, `edit_file`, `list_directory`, `glob`, `grep`,
`run_command`) confined to the profile working directory: paths resolve through
symlinks and are refused outside the root, reads cap at 256 KB and command
output at 64 KB (tail kept as it drains), and `run_command` is an unrestricted `/bin/sh` with
a timeout of at most 30 minutes and credential variables withheld; a timeout
kills the process group before reaping the shell. The guest needs only Python 3,
and guest files must run on Python 3.9 (a test runs the worker under it). Guest
writers share a lock sidecar; on Windows the writer polls a non-blocking
byte-range lock up to a bounded ceiling (as `platform_lock_compat` does) and
refuses the write on expiry.

The fence is on the gateway. A remote session's agent definitions unmount every
engine built-in (`AcpRuntime._remote_workspace_agents` keeps `@server` grants,
expands `*` to the servers sent, and replaces each built-in with its workspace
equivalent: `fs_read`/`glob`/`grep` with the read tools, `fs_write` with
`write_file`/`edit_file`, `execute_bash` with `run_command`, `@builtin` with all
of `@workspace`; an empty or read-only allowlist stays one, `_workspace_grants`).
Built-in exclusions also exclude their replacements, and core wildcards keep the
complete requested pattern. Workspace tools cannot skip the gateway gate: shell
arguments meet command policy and the deny floor, and file tools carry their
read/write scopes, matched as the guest's POSIX paths on any gateway OS
(`platform/guest_paths.py`; governance.md, "Path matcher — remote-workspace
targets"), so a Windows gateway attaches a workspace too. The link stops reading (so acknowledging) while the session's
reader is behind, holding the guest at its unacked window. The fence needs a
host whose tool allowlist travels over the wire; only KAS qualifies, so any
other backend is refused before a workspace is touched
(`check_remote_workspace_supported`). Crew tools that act on the gateway machine
are fenced too: the `kirocrew-core` tools in `LOCAL_ONLY_CORE_TOOLS` are left out
of the grants (a whole-server grant is narrowed per tool by
`_without_local_only_grants`, from the declared titles table),
`LOCAL_ONLY_SERVERS` (computer use, and cron, whose jobs run here) are not sent,
and each of those servers refuses its tools in band
(`refuse_local_only_tool`) when the choice record says remote or cannot be read.
`test_every_core_tool_is_classified_for_remote_sessions` makes each new core
tool local-only (`task_run` and `workflow_run`, which start agents here) or
reviewed remote-safe. `deploy_artifact` refuses only `local_dir`; the
computer-use refusal is about placement, not governance.

### Transport

Git from a remote workspace to GitHub, GitLab, Bitbucket and Azure DevOps is
proxied through the gateway, so the credential never goes to the guest. Session
start runs `begin_git_proxy` in the background (it never delays the start, and
a failure only costs that git access): a guest worker runs `git_proxy.py`, a
stdlib HTTP server on guest loopback that writes the port to the file the
gateway names with the required `--port-file` (one per launch; there is no
shared default on either side), and the gateway's
`git_gateway.GitProxyChannel` answers it over that worker's own authenticated
link. `workspace_tools.run_command` gives each command git's environment config
(`GIT_CONFIG_COUNT`, so the workspace template needs git 2.31 or newer: an
older git ignores those variables and reaches the host directly, without the
gateway's login), appended after any `GIT_CONFIG_*` entries the command's
environment already sets, rewriting each host's spellings
(`workspace_tools.GIT_PROXY_REWRITES`, pinned equal to `git_gateway.HOSTS`:
`https://`, `git@host:` and `ssh://git@host/` for the first three, HTTPS only
for `dev.azure.com`, whose SSH path shape differs) to `/<host>/` on the proxy,
for the lendable hosts only, and only while the port answers (a stale file from
a dead proxy must not break public clones). Which hosts are lendable is the
gateway's call: once the guest proxy sends a `lend` frame, the channel
publishes `{"t": "lend", "hosts": [...]}` (`GitProxyChannel.lendable_hosts`:
empty under `strict` or an unknown tier, else each host whose credential
sources answer; values never leave the gateway) at once, every
`LEND_REFRESH_SECS` (60 s), and as soon as an upstream 401 drops a cached
login. Hosts are looked up concurrently, each under its own lock, and until
the first full list is out each host is published as soon as its login is
found, in a frame marked `"partial": true`, so a slow source for one host does
not hold back another's; a refresh publishes only its full list. A login the host refused is not lent again until its source returns a
different value (a source such as `gh auth token` can keep printing a revoked
token), so that host goes direct until a fresh sign-in. The proxy keeps the list as `<port> <host>...` in `<port file>.lend`
(`<port> * <host>...` for a partial one),
removes it on exit, and `git_proxy_env` re-reads it for every command; a list
that is unreadable routes nothing. A list that is missing, or names another
port, means the gateway has not answered this proxy yet (the proxy removes the
file before it asks): a command run then waits for the first answer, up to
`GIT_PROXY_FIRST_LEND_WAIT_SECS` (20 s, above one credential CLI's 15 s limit),
so a first private clone right after the session starts still gets the login.
A partial list keeps the command waiting, since a host still being looked up
would be left out of its rewrite; a complete list, even an empty one, is used
at once. Past the wait the command routes what has been found (nothing if no
list came), and if the proxy stops it routes nothing. The proxy itself starts in
the background, and writes its port file only once it listens (its server binds
without a host-name lookup, which can stall on macOS). Each new tool server is
told by the launch whether a proxy start is in flight (`starting:<n>`, from
`RemoteWorkerLaunch.git_proxy_state`) or not (`none`). Only during an in-flight
start, and within the tool server's first `GIT_PROXY_FIRST_LEND_WAIT_SECS`, does
a command with no listening port file wait for it. The wait ends as soon as the
port answers, or when the launch writes start `n`'s number to
`<port file>.failed` because that start failed. With no start in flight, a
missing proxy costs no wait. A host that
is not routed keeps the workspace's own git login (its SSH key, its
`GIT_ASKPASS`), exactly as without Kiro Crew, so under `strict` everything goes
direct. `GIT_ASKPASS` and the terminal prompt are never changed; a credential
helper scoped to the proxy's own URL answers `quit=1`, so a routed request
that ends in a 401 prompts no one. The protected-branch rule below covers
routed pushes only: a push made with the workspace's own login goes straight
to the forge, as from any machine holding that key. No
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
policy ceiling even on an unrestricted dashboard surface. A push asks no one:
before a `git-receive-pack` POST goes upstream, the channel reads its
ref-update commands (`PushCommands`: optional `shallow` lines, then
`<old> <new> <ref>` pkt-lines up to the flush, capped at 8 MiB) and refuses the
whole push when any command updates or deletes a protected branch
(`is_protected_ref`, which reuses the local git-publish floor's
`_PROTECTED_BRANCHES` and `_normalize_ref` on the lower-cased ref, so
`refs/heads/main`, `refs/remotes/<r>/main` and any case of them match exactly
as `git push origin <ref>` does locally; parity is at the destination-ref
level, since the local check's refusals of unresolvable spellings such as a
bare push or `--all` reach the proxy already resolved to concrete refs). The refusal is a receive-pack result
that marks every command `ng` (in the side band the client asked for, every
packet within its size limit, with the reason on a `remote:` line), so git prints `! [remote rejected]` per ref; a
client that asked for no report gets a 403. Nothing of a refused push goes
upstream. A body that cannot be read (a bad pkt-line, a short ref name, a
compressed body, a body that ends before its flush, a signed push's
certificate) is refused with a 403. Every other push, a force-push or delete
of a feature branch included, goes through unprompted, as it would locally,
and any process in the workspace can make one, as any local process can. Every
push decision writes one SEL `governance_decision` row (`_audit_push`: scope
`git.push`, the repository and refs as the item, outcome `allowed` or
`denied`, rule `no-protected-branch`, `protected-branch`, `unreadable-push` or
`strict-sandbox`), best-effort, so a failed write never changes the answer. A
request refused before the egress check (a host or endpoint the proxy does not
carry, or a channel with no calling agent) writes one denied row too (scope
`git.request`, rule `not-proxied` or `no-agent`). The
channel then takes the credential from that host's sources on this
machine, in order (`host_credential`): `gh auth token` for GitHub (Basic
`x-access-token`), `glab config get token` then git's credential helper for
GitLab (Basic `oauth2`), git's credential helper for Bitbucket (Basic
user:password, an app password or API token), and `az account get-access-token`
for the Azure DevOps resource then the helper for Azure (Bearer, or a PAT as
Basic). The helper is asked with `GIT_TERMINAL_PROMPT=0` and
`GCM_INTERACTIVE=never`, so it answers from what is stored and never opens a
prompt or a browser on the gateway. Under the `strict` agent sandbox tier, which
hides those logins from local agents, the proxy lends none
(`gateway_logins_lent`: the configured tier and its readability from one
`KiroCrewConfig.load()`, with the governance floor applied, read per request; an unreadable tier counts as strict, and so does a
load that `KiroCrewConfig.load()` marked degraded -- an unparseable
`config.json` or `config.local.json`, or a degraded `agent` section -- because
that load stands field defaults in rather than raising). Under `strict` the
guest routes nothing here (the published list is empty); a request that still
arrives, from a list published before the tier changed, is read upstream
anonymously, a private read's 401 reaching git as a 403 naming the strict tier,
and a push is refused with that message before its body is read. Other tiers
lend as described here, by the maintainer's choice of "strict lends no gateway
git login at all" over per-tier lending: under `standard` and `cc` a remote
session is also lent logins those tiers hide from local agents (`~/.azure`
under both, `~/.git-credentials` under `cc`), so its git reach can exceed a
local agent's on the same tier.
Each host's value is cached in memory for
five minutes, never logged or framed, sent upstream only, with no redirect
followed, and the response is streamed back. An upstream 401 drops the cached
value. For a push it reaches git as a 403 naming that host's sign-in step; a
read retries once anonymously (so a public repository still clones), and if
that also gets a 401 it reaches git as that 401, which prompts no one because
the proxy's URL has its own `quit` credential helper. Azure's legacy `ORG.visualstudio.com` URLs and clone
URLs carrying a user (`https://ORG@dev.azure.com/...`) are not rewritten. The proxy lives as long as the session's CURRENT
workspace relay: a start can make more than one (an early tools probe, a
respawn), so the git relay's `close` stops the proxy only when the closing relay is
the newest one `workspace_server` made (`_git_proxy_relay`, which survives
`close_relay` forgetting `launch.relay` at a runtime's normal end; a restart's
replacement close leaves the proxy to the restart's own `begin_git_proxy`),
ending its input first so it removes its own port file,
so an older relay idling out cannot leave a live session without git. The agent's
remote-workspace notice says git to the four hosts works as usual: through the
gateway, with its login, for a host it can lend for, and with the workspace's
own login otherwise (only that under `strict`), and that a protected-branch
push through the gateway is refused; it tells the agent not to hunt for keys or
unset the rewrites, and does not mention `GIT_ASKPASS`, which stays the
workspace's. `git_proxy.py` ships in the installed worker
files, and the gateway starts it with `--build`, the SHA-256 of its own copy:
a guest of another build (or started without one) exits before it frames
anything, and a guest link that ends before its first frame marks its start
failed, so version skew ends git's wait at once instead of hanging. A
request's upload flows on credit: the guest sends at most
`git_proxy.UPLOAD_WINDOW` body frames ahead of the `credit` frames the gateway
returns as it takes each piece, so the gateway's frame reader never waits on
one request and a stalled upload cannot hold back its own `cancel` or another
request; a guest past its window fails only that request. Pinned end to end by `test_remote_git_proxy.py`,
which runs a real `git` clone and push through the real proxy and channel
against `git http-backend`, and asserts the token never crosses to the guest.
The git-only `GitProxyLink` binds its output reader to a byte watermark: when
unread bytes fill the reader, it stops feeding the wire endpoint before more
output can be acknowledged. It pauses through the base link's own consumer
gate, so the link's stall check counts a busy git reader as reading, not as a
silent connection. The worker's unacknowledged-byte limit then stops
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
receive flow.

A vendor tunnel is not assumed to deliver ACP byte for byte (an SSM port forward
rewrote LF into CR; ECS Exec does not forward piped stdin), so tunnels use
integrity-checked framing with no fixed per-message timeouts, in
`kiro_crew.remote_session`:

- `wire` (sans-IO): an HMAC-SHA256 handshake over fresh nonces and the resume
  position, base64 data pieces of at most 32 KB, and control lines (ack,
  resend, ping, retire, close), each MACed under a per-connection,
  per-direction key, so a frame cannot be forged, reflected or replayed.
  Sequence numbers and unacked data persist across connections, so a
  reconnect delivers every byte exactly once; the unacked window is bounded.
- `worker` (guest): reads and unlinks its secret file, listens on loopback,
  attaches only an authenticated gateway (a newer one replaces the old),
  withholds credential variables from its child, stops the child after
  `wire.ORPHAN_TIMEOUT_SECONDS` (600 s) without authenticated inbound traffic,
  and retires on SIGTERM. It should run as a different OS user from agent tools.
- `link` (gateway): sends keepalives, redials with jittered backoff, renews its
  300 s lease only on authenticated traffic, and reports LOST, REFUSED,
  RETIRING or FINISHED.

`AcpRuntime.set_remote_workspace` adds the workspace server to every session's
`mcpServers` (new and resumed). Its command is the bridge
`python -P -m kiro_crew.remote_session.client --pipe`, exiting 0 when the relay
ends the stream or 75 when it cannot reach the relay. The bridge never dials: it
runs in the engine's sandbox, which cannot use the gateway's `coder login`, and
is launched more than once per runtime. `remote_session.relay.WorkspaceRelay`
runs on gateway loopback instead: for each bridge presenting the runtime's relay
token (a 0600 file in a 0700 directory, compared in constant time) it starts a
guest worker with a fresh one-use secret, runs `RemoteLink` over
`coder ssh --disable-autostart` and pipes bytes to the bridge. It serves at most
four connections, stops each one's worker when it ends, keeps one pre-started
spare (replaced at `wire.SPARE_MAX_AGE_SECONDS`), and closes with its runtime or
after 15 idle minutes. `coder.remote_worker` installs the guest files in one
`coder ssh` under `umask 077` through per-process temp names, starts the worker
as a transient systemd user service or under `setsid`/`nohup`, and gives each
worker its own id (`<binding>-<8 hex>`) carrying `_GATEWAY_EPOCH`, so runtimes
sharing a binding cannot overwrite each other and the first start in a new
process (`reap_stale_workers`) stops workers a previous gateway left running.

### Binding a session

A session runs remotely only when the owner binds it through
`PUT /api/chat/slots/{slot}/environment` (owner-only; `{"profile": id}` binds,
`null` clears). The choice is stored per session key in an owner-only
session-choices file under the data home's Coder directory and applies at the
next runtime start; a running runtime never moves. Startup skips placement
reads only when both configuration and the choices file are absent. Once the
session has a message the choice is locked unless its remote workspace was
never created (`GET` reports `locked`; a changing `PUT` answers 409
`environment_locked`), and the composer chip (`SessionEnvironmentChip`) becomes
a status. A changed choice arms the slot's deferred reset; only a choice back to
this machine prewarms. `session_environments.default_profile` binds a new owner
dashboard chat (`apply_default_environment`, only while it has no choice and no
messages; any failure leaves it local); scheduled jobs, channels and peer chats
are untouched. A bound session bypasses the warm pool, whose children are local
engines (`kirocrew.session.pool.decision` outcome `bypass_remote_environment`).

Preparing reserves a row in the Coder ownership registry before any compute
exists (a non-empty `deployment_id` is required and never retargeted; rows of
another deployment are left alone), then finds, creates (with an auto-stop) or
starts the workspace with the gateway's `coder login`, adopting it only when
its owner, template and uuid match. A workspace deleted outside Kiro Crew is
never silently recreated. Every failure raises `SessionEnvironmentUnavailable`;
an unreadable choice record, or a bound session whose profile is gone, is
refused, so a bound session never falls back to the gateway machine. When a
session with history gets a new workspace (`PreparedRemoteSession.created`),
`mark_workspace_recreated` makes the next `[PROJECT]` rail say once that it is
empty. The per-message context names the guest directory instead of the
gateway project (`remember_remote_workspace`), and
`ResourceStatus.context_line(remote=True, workspace_memory=...)` judges each
machine on its own reading in `[RESOURCES]`. A tight gateway, which runs the
engine and its subagents, only flags sub-agent waves. The workspace's memory is
the last `session_health` reading (`remember_remote_memory`), read from cache
and refreshed in the background at most once a minute per session whatever the
read found, and never while the `[RESOURCES]` line is switched off
(`refresh_remote_memory_soon`) on the gateway loop `remember_remote_workspace`
recorded, through the health read the session's provider registered
(`register_remote_health`; Coder registers `session_health` when it prepares the
session, and a session with none is never read; the read belongs to the session's
placement, so a runtime's end, including a resume respawn that registers none,
keeps it and only a session switched back to local or a finished subagent drops it;
when the table is full a session with no live runtime is evicted first), so a turn never waits on the
remote machine and a stopped one is
never woken. A reading older than five minutes is ignored. A workspace is tight
when its free memory is at or below the smaller of the gateway's pressure floor
and a quarter of its own total, so a small idle workspace is not. A tight workspace
gets the "targeted tests, serialize builds" advice, a healthy one reports its
free memory, an unmeasured one says so, and the line is silent when both
machines are clear.

A subagent of a remote session inherits its environment:
`inherit_environment_for_subagent` writes the child's choice with a `parent`
owner key resolved to the root session, and `prepare_for_session` reuses the
owner's workspace. The child takes the dedicated-process arm with its own
worker and fence; an unreadable choice or a malformed `parent` refuses the run.
Its spawn owes only the local engine's reserve ([subagent](subagent.md)).

### Start progress

A first start can take minutes, so `session_environment.report_start_stage`
records `checking`, `provisioning` (only when compute is created or started),
`connecting` and `starting_agent`, plus `entered` (stages so far) and
`reconnect` (this gateway already had the session attached). The record is in
memory, bounded, and cleared in the `finally` of
`AcpProvider._start_kiro_runtime`. `GET /api/chat/slots/{slot}/environment`
returns it as `starting`; the chip polls once a second only while a turn runs.
The setup card shows every stage before the current one as done, and appears
only for a first connection or a restart that enters `provisioning`. Coder
lifecycle calls run under the Coder client's output and timeout bounds, not the
64 KB guest-command bound.

### Archive, retention and shutdown

Archiving a session (tab close, session control's close, the idle sweep) calls
`coder/retention.schedule_archived` as a background task that never delays or
fails the close. `warn_unsaved_work` first reads `git status` in each running
workspace within `UNSAVED_CHECK_SECS` and publishes one `system.agent` notice
naming unsaved work. The workspace is then stopped and stamped
`deletion_due_at` = now + `ARCHIVED_RETENTION_DAYS` (7, fixed: a stopped workspace
still holds its disk). Reopening before
the deadline clears the stamp: a History resume awaits `retention.keep_reopened`
before it publishes the tab (`retention.resume_check` for Coder, `reopen_session`
for a sprite or boxd machine, which identity-checks the live VM first and leaves a
same-name replacement alone). A cancellation that fails refuses the resume with
`reopen_retention_failed` (503, retryable); so does a reopen whose registry
write fails (only a replacement VM under the recorded name is left alone and
not kept). A resume refused after the
cancellation archives the still-closed session again; a refused revive of a
session that was never closed archives nothing. `prepare` runs the check
again and restarts the same workspace. `retention.run_sweeper` (every minute, first pass
30 s after boot) runs `sweep_due`: a due row is flipped to `deleting` under the
registry lock (`begin_deletion`, the critical section `cancel_deletion` also
takes), its owner, template and uuid are proven, the workspace is deleted, and
only then is the row dropped. A failed delete retries next pass, a reopen while
`deleting` is refused with "try again in a minute", and a later reopen
provisions a fresh workspace. Unstamped rows are left to Coder's auto-stop.

On a graceful shutdown with `session_environments.stop_on_shutdown` on (the
default), `_shutdown_and_exit` runs `retention.stop_for_shutdown` under
`SHUTDOWN_STOP_SECS` (8 s, inside the 10 s graceful budget so the hard-exit
cleanup and audit flush still run before SIGKILL), never raising: it stops every running
registry-owned workspace except stop-destroys-compute ones and stamps nothing.
That covers rows whose profile was since removed, so the settings card shows the
stored switch as it is and keeps an "on" switch usable with no profile left.
Without one, the worker's orphan timeout (above the link lease, so a restart
reconnects first) and the Coder deadline, which only the gateway extends, stop
the guest. Retention deletion needs a running gateway; a stopped workspace keeps
billing for storage meanwhile.

After a disconnect the in-flight turn is lost until the user presses Resume;
pending approvals are dropped and their commands never ran remotely. An active
`run_command` dies with its process group when the guest worker retires or
orphans; detached background processes are not tracked. The next message
reconnects to the same workspace.

### Sprites and boxd: one persistent VM per session

Fly.io Sprites profiles (`session_environments.sprites_profiles`) and boxd
profiles (`session_environments.boxd_profiles`) give each session its own VM
that keeps its disk when idle. A choice row records the resolved provider beside
its profile id; `session_environments/routing.py` (`profile_provider`) derives
the provider from the id for `prepare_for_session`, `session_browse` and the
dashboard (legacy rows are tagged `coder`). `prepare_for_session` refuses a row
whose recorded provider
is neither `coder` nor the one the id now names, so a removed profile whose id
another provider reuses never moves the session onto a fresh VM. A `coder` row
whose session already owns a Coder workspace row is refused the same way, so
an archived workspace never stays due for deletion behind a moved session. An id in more
than one provider list is unavailable at launch and omitted from the picker.
The start gate counts Sprites and boxd profiles as configuration beside the
Coder URL and profiles. Unlike a Coder profile, a Sprites or boxd profile may
leave `workdir` blank: each start reads the machine's login home (`$HOME`, which
must be absolute) and records it on the session's row (`SessionResource.home`),
which the session, Files and the terminal then use.

The provider-neutral `session_environments/` package holds what more than one
provider shares, so `coder/` keeps only Coder's own code: `routing.py` (the
provider table, the cross-provider id rule and the guard above), the VM branches
of browse and retention (`browse.py`, `retention.py`), and `persistent_vm.py`.
A test keeps `sprites_session` and `boxd_session` from importing any of it from
`coder/`. Both providers share `persistent_vm.py`: `SessionResourceRegistry` (one file per
provider under the data home, fail-closed on a corrupt or unknown-version file,
reading only declared row fields, written atomically under a blocking
cross-process lock, so every call runs through `asyncio.to_thread`) and
`PersistentVmEnvironments`, which owns when a VM is created, kept or deleted:

- `prepare` bumps the owner's `retention.session_lifecycle` generation and runs
  under its lock, as the Coder path does. `check_ready` (a Sprites token in the
  vault, the boxd CLI installed) runs before anything is reserved.
- A name (`kirocrew-` or `kc-` plus 16 random hex digits) and its row are
  written BEFORE the create call, so a failed create is reclaimed immediately or
  by a later prepare or archive; cleanup reads the live VM first. The returned
  provider id is recorded, and a VM answering under the name with another id is
  refused, never adopted. The row records the Sprites `token_secret`, so a
  sprite whose profile was removed is still deleted when due. The retention task
  starts when either registry exists, even with no profiles or Coder URL (an
  off-loop probe; a metadata failure keeps maintenance enabled, and live config
  is rechecked after it), so archived-row sweeps and boxd shutdown hibernation
  survive profile removal. A VM deleted outside Kiro Crew is replaced under a
  new name (`created`).
- `retention._archive` archives Sprites and boxd VMs inside the same lifecycle
  lock and generation check: the row is stamped due in `ARCHIVED_RETENTION_DAYS`
  before any remote call. boxd arms its own `delete-after` timer for the same
  days and hibernates the machine; a sprite goes cold
  on its own. Live identity is checked before arming a timer and before
  deletion, so a same-name replacement is never touched. A reopen before the
  deadline clears the stamp (and boxd's timer): a History resume does so at once
  (`retention.schedule_reopened`, under the lifecycle lock, superseding a queued
  archive, creating and waking nothing), and `prepare` again on the next start.
  A failed boxd undo keeps the stamp so that start retries it.
- `retention.sweep_once` runs `PersistentVmEnvironments.sweep_due` for both
  providers before the Coder sweep, with or without a Coder URL; it is
  registry-only while nothing is due and re-reads each due row under the
  session's lifecycle lock before deleting. One provider's failure is logged
  once per distinct message and never stops the other.
- `stop_for_shutdown` hibernates every boxd machine (rows untouched); sprites
  need no call.
- The environment health card and the unsaved-work check resolve the session
  with `wake=False`: a VM whose live record is not `running` (a cold or warm
  sprite, a hibernated boxd machine) reports `stopped` with no exec, since an
  exec would wake it. The Files panel and terminal are explicit requests and
  may wake one.

Sprites is reached through its API alone (`sprites_session/client.py`): the
token is read from the vault per call by the profile's `token_secret`, sent only
as the `Authorization` header, and never framed, logged or passed to the sprite.
`SpritesShell.run` is one non-TTY exec WebSocket with the worker secret on
stdin, and `SpritesWorkerLaunch.dial` opens the proxy WebSocket to the worker's
`127.0.0.1` port, so `RemoteWorkerLaunch`, the workspace relay and the git proxy
run unchanged. A sprite is created with URL auth `sprite`, never `public`. The
dashboard terminal runs `sprites_session.terminal` as its PTY child
(`bridge_argv` takes only a minted sprite name, its recorded id and a secret
name; the bridge re-checks that id under the token it loads) on a POSIX
gateway. boxd uses the gateway's own `boxd auth login`; a one-way fingerprint
of `boxd auth --json` (account and active org) is recorded per machine, so a
lookup miss under another account or org is refused, never read as a deletion,
and so is a miss after which the account read differently than before it.
The fingerprint (Sprites: of the token) is saved with the reservation before
the create, and the create refuses under any other one, so a create cut short
before its record is bound still names the account its machine lives in.
`boxd machine exec <m> -- sh -c <cmd>` streams stdin and stdout and returns the
remote exit code like `coder ssh`, so `BoxdShell` drops into the unchanged
launch, and the terminal is `boxd machine exec --tty <m>` changing into the
session's working folder before it starts a login shell. Machine names are
validated before any argv is built. The Files and terminal views look the VM up
(`GET /v1/sprites/{name}`, `boxd machine get`) and never create one. The
Settings card stores a Sprites token through a masked field straight into the
vault (`POST /api/secrets`) and never deletes vault entries; a failed vault
listing renders `ErrorNotice` with a Retry that preserves drafts.

### Dashboard views

Views that read the gateway's project switch on `useSlotRemoteEnvironment`:
the composer drops the project chip, the Git view gets no folder, and the Files
tab renders `RemoteFilesPanel`. It reads through
`GET /api/chat/slots/{slot}/environment/browse?op=list|read&path=`
(owner-only; `coder.session_browse` runs `workspace_tools.py --browse` with the
request on stdin and the tools' confinement and caps) and saves through
`PUT /api/chat/slots/{slot}/environment/file` `{path, content, expected}`
(owner-only, SEL-audited, the separate `--write` mode). A save whose `expected`
differs from the file answers 409 `file_changed`, a truncated or binary read is
not offered for saving, a stopped workspace answers 409 `workspace_stopped`, and
outdated guest files are reinstalled once before a retry. A terminal opened
from a remote chat carries `remote_slot` and opens
`coder ssh --disable-autostart <workspace>`, or closes the socket with a coded
error (`terminal_wrong_machine`, or `terminal_placement_unavailable` while the
chat's placement cannot be read) rather than opening a local shell.

Once a remote session is fixed, its chip opens `EnvironmentHealthCard` on hover
(or focus and click): workspace name, guest directory, state, CPU cores and
architecture, load averages, memory and disk meters, and uptime. It reads
`GET /api/chat/slots/{slot}/environment/health` (owner-only), which runs the
browse mode's read-only `stats` op (no environment, process list or paths),
polling every 15 seconds only while the card is open. A guest whose files
predate `stats` answers with an unknown-op error, so `session_health` installs
the current worker files once and asks again. Its header says it is a read-only
status. An unavailable read leads with a plain "isn't responding" title over
the guest's own reason, kept for the agent hand-off; an oversized or
undecodable memory sample is omitted rather than published partially; in a
container workspace the cgroup memory limit and CPU quota replace the host
node's figures when they are lower, usage being the working set without
reclaimable page cache, and the node's load average and uptime are left out; a
stopped workspace reports `state: stopped` and is never started to be measured.

### Provider requirements

Every provider meets these before a session is offered on it. A worker serves
one owner. The gateway holds a heartbeat-renewed lease, so a worker that misses
it is lost, not slow. A lost worker never loses the conversation: the gateway
restores it on a fresh worker with `session/load`. A retiring worker accepts no
new turns, ends the turn in flight, reports release and exits. Teardown is
idempotent, deletes only resources whose ownership record the gateway wrote,
and never stops a shared parent resource another session still uses.

CLI failures report the exit code and operation timing, never stderr, which is
drained unretained because remote commands can print unlabelled credentials.
Stdout and stderr are capped while reading, timeout and cancellation kill the
process tree, and structured stdout is validated per operation.

### Spawn audit boundary

The Coder lifecycle and transport children run on the gateway with fixed argv
from saved configuration and validated registry names; guest commands are
quoted remote arguments with payloads on stdin. They need the host login, so
each has a justified entry in `test/test_spawn_audit.py` instead of the agent
sandbox. The guest-script rule covers only execution inside the guest
(`worker.run`, `workspace_tools.run_command` and `_git_status`, which cannot
import the gateway sandbox), never a gateway CLI reaching a guest; gateway-side
agent-influenced commands use the sandbox chokepoint. The two `main` entries
are in-process `asyncio.run`. No directory-wide exemption exists.
The boxd lifecycle CLI is a gateway spawn with fixed machine verbs, validated
gateway-created names, saved snapshot/CPU settings and a validated retention
duration, no local shell or agent-selected cwd, and stdin closed; it needs the
host login. The Sprites terminal's bridge argv runs isolated Python with `-P`
so the terminal's working directory cannot shadow the standard library.

The gateway's Git credential and readiness CLI calls also have individual audit
entries: their verbs and host names come from the fixed credential-source and
host tables, never guest argv. Every CLI and Git helper executable passes
`github_runner.validate_provider_executable(require_protected=True)` off-loop:
user-writable installs and overrides are refused, even outside a project. Children
receive a minimal environment with a trusted system PATH, no interpreter injection,
and a non-project cwd. Git helper discovery uses protected Git in its installation
directory and stops repository discovery there. Global/system config is read only
as data: fixed helper selectors (`store`, `cache`, `osxkeychain`, `manager`,
`manager-core`, `libsecret`, `wincred`, or the exact `gh auth git-credential` shape)
are rebuilt with protected absolute executables, found in Git's exec-path or, for a
helper installed outside it (Git Credential Manager in `/usr/local/bin`), in the
trusted system directories. Shell snippets, configured paths
and extra arguments are not executed. `store` and `cache` run only Git's `get`
action against their default locations, never a write. Host-scoped keys and helper
resets are retained and passed to `credential fill` as `GIT_CONFIG_KEY_n` /
`GIT_CONFIG_VALUE_n` pairs, never as `-c key=value` text (a free-text subsection
can hold `=`, which git would split at; a key holding `=` or a control character
is skipped). `credential fill` reads only that synthesized configuration,
with the original global/system files disabled, preventing a config swap from
changing what executes. A host with no usable protected helper fails to authenticate;
readiness still discards every output stream without reading a secret.
Known limitation: the helper is asked per host, with no repository path, so a
helper set for one path under a host (`credential.https://dev.azure.com/<org>.helper`)
is not matched; a helper set for the host (`credential.https://dev.azure.com.helper`
or `credential.helper`) is. In that case the host is not lent (its git uses the
workspace's own login), and the refusal and the gateway log name the setting to use.
On a Windows gateway the same validated helpers are run without git's shell:
git runs every helper through `sh -c`, and Git for Windows keeps `sh` in its own
`usr\bin`, which is not on the trusted `PATH`. `_fill_directly` runs each
helper's validated executable with `get`, in git's config order, honouring an
empty value's list reset and the host-scoped keys `credential.https://<host>.helper`
(`_helper_applies`), and speaks the helper protocol itself: a user name one helper
returns goes to the next, an expired password is dropped, `quit` stops. The secret
is read from the helper's stdout pipe only; it is in no argv, environment or
file. Git Credential Manager, which Git for Windows ships beside `git.exe`
(`mingw64\bin`), is a candidate there under the same protected-install walk. A
lookup that times out or is cancelled kills the CLI's whole process tree (`az` is
`az.cmd`). The relay token file gets an owner-only, inheritance-protected DACL
(Owner Rights and the gateway user's SID), read back before the token is
written; otherwise the file is removed and the launch fails.
A Windows gateway that cannot read its own SID or load the security-descriptor
API lends nothing (`lending_unverifiable`): the lend list is empty,
`host_credential` and the git-helper source refuse with `WINDOWS_LENDING_UNVERIFIED`,
a push is refused whole (audited as `windows-unverified`), and readiness reports
`unsupported`, which the card states in one line. With remote workspace paths
matched as POSIX on any gateway (#23498), a remote workspace starts on a Windows
gateway too, so lending works there end to end: workspace, guest proxy, gateway
lookup and lent login.

### Template contract

A Coder template for remote sessions provides a pinned image and architecture,
an unprivileged user with a writable home and workspace root, the same
workspace across starts, the Coder agent, Python 3 and a POSIX shell, no
gateway, model, Git or storage credential, and outbound access with no public
ingress. Its storage must survive a stop unless the profile sets
`compute_survives_stop: false` (such as MicroVM): neither archive nor
`stop_for_shutdown` stops that workspace, and the unsaved-work notice says its
files go when the compute stops. `retention.compute_survives_stop` reads the
flag from the bound profile when it still names the template, else from every
profile on that template; no match reads `false`. Without systemd the
`setsid`/`nohup` worker fallback may not survive Coder's SSH session cleanup.

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
  `acp/client.py` and `acp/session_handle_dispatch.py`; no end-to-end run against a real
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
under the mate's name. The projection and every Crew-side record keep the
mate's id (`crew-mode.md`, "Template names the harness cannot activate"). This repository does not establish a universal native model or
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
cp "$root/test/member_essential_context_helpers.py" "$snapshot/test/"
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
- `compact()` → sends `/compact` via `send_command()`. The **dashboard's** manual `/compact` gates on `ACP_BACKENDS_COMPACT` first, as a pre-acquisition local command: the live session's `manual_compact_unsupported_backend` capability property (declared on the `LLMProvider` ABC with a `None` (supported) default per harness-parity H14, answered by the ACP implementations from set membership) is read when the session's provider is positively alive, else for the backend `select_provider_backend` would build (the member backend for a member session, otherwise `agent.acp_backend`) — so a refused `/compact` behaves as if the turn never started (no session created, no Slack OPTIONS expired, no one-shot turn state consumed). The reply is informational — the backend manages compaction automatically, mirroring the `cc_managed` relationship — not an error: kiro-cli answers the prompt with `_kiro.dev/compaction/status`, claude-agent-acp compacts natively in-prompt, and codex-acp intercepts the prompt as the `compact` command it advertises and reports the compaction as a `tool_call` pair, marked `_meta.contextCompaction` on 1.11.0 and unmarked on 2.2.2 (`_dispatch.parse_codex_compaction_update`; the unmarked shape only for `ACP_BACKENDS_UNMARKED_COMPACTION_PAIR`), and on KAS `AcpSessionHandle.prompt` sends the engine's own `_kiro/session/compact` verb in place of the prompt (as prompt text, the KAS model only writes a summary and the context does not shrink). That request's `{success}` answer ends the turn and the turn carries a compaction status: the engine's `summarization_completed`, else a synthesized `completed` (nothing to summarize, meter left alone) or `failed` (refused while a turn or another compaction runs). A backend outside the set would strand `wait_for_compaction()` for the full `COMPACT_WAIT_TIMEOUT_SECS` (#7800). The **auto-compact** path consults the same capability from the compaction gate ladder (`session_compaction._compact_unsupported_backend`) and then takes one of THREE arms, because a backend it cannot dispatch to is not one thing. A member of `ACP_BACKENDS_HARNESS_MANAGED_COMPACTION` (KAS, read through `harness_managed_compaction_backend`, so it declines here even though its manual `/compact` is served) declines with `"compact_unsupported"` before the compaction task is scheduled, so no `/compact` is dispatched and the turn semaphore is never acquired — an ungated dispatch stranded the status wait for the whole `COMPACT_WAIT_TIMEOUT_SECS` while HOLDING that semaphore and then recycled the session (#7812) — and declining costs nothing there because its `summarization_completed` frame resets the meter. A member of `ACP_BACKENDS_CONTEXT_RECYCLE` (deepseek) is **recycled** instead, via `_recycle_unmanaged`: no compaction reaches it from either side, so a decline bounds nothing and its context grows into the harness's own window. That arm falls THROUGH the decline rung rather than returning from it, so `unconfirmed`, `in_progress` and `cooldown` still run first — an ambiguous reading must not spend a recycle. A backend in NEITHER set declines and is logged at WARNING naming the missing membership, because ending a conversation is not something a harness earns by never having been classified; `compaction_self_managed` is what tells that case apart from KAS's. The callback reports which arm ran (`compacted` / `recycled` / `restarted_uncompactable` / `cancelled`, plus `waiting_for_subagents` during a hold) so a surface cannot announce a summary that never happened, and only `restarted_uncompactable` may say the backend cannot compact — a kiro-cli session whose in-place `/compact` merely timed out reaches `recycled`. `cancelled` means a user Stop ended the compaction: the cooldown is armed and the session is not recycled. Every context restart — a failed in-place `/compact` and an uncompactable restart alike — is first HELD while session-sharing sub-agents still run on the parent's process, because shutting that process down would end them unreported (`CompactionCoordinator._await_cotenants`): the callback reports `waiting_for_subagents` with `success=False`, the hold re-checks every `cotenant_poll_secs` (the manager's per-key completion event is shared, so another waiter may release it), and past `cotenant_wait_secs` only those runs are stopped with the ordinary per-run `SubagentManager.cancel`, bounded and shielded, so each still reports "stopped" into the conversation, which carries on after the restart (the parent-end helper would mark them out of delivery). A user Stop during the hold ends it: the stale restart is abandoned, the sub-agents are not stopped, and the compaction settles `cancelled` (cooldown, no recycle). Channel messages queued behind the held turn are not dropped with the old session: `CompactionCoordinator._restart_held` starts the successor first (past the recycling marker, holding its lease), moves the old session's live queue into the successor's real queue, then recycles the old one and wakes each entry's channel drain before releasing the lease, so queue verbs, teardowns and ordering behave as for any queue. Slack registers a drain for this (`_drain_slack_queue`). If the successor cannot start, the old session, its queue and its resume id are kept and the failure cooldown applies. A report that waits on the parent during the hold cannot reach the old process: every waiting acquire re-validates the session's identity, so it lands on the fresh session. The **messaging-surface** `/compact` commands (Slack, Telegram, Discord, Webex, Teams, Feishu, iMessage, WeCom, Weixin and WhatsApp) gate on the same capability through `messaging.commands.compact_unsupported_backend` before dispatching, answering with `compact_unsupported_reply` (translated on the Chinese-language surfaces, plain-voiced on iMessage and WhatsApp); their context-threshold notices decline silently on such a backend — no forced hard-threshold compaction to run, and no soft nudge whose `/compact` advice cannot work (#8156) — with one exception: a member of `ACP_BACKENDS_CONTEXT_RECYCLE` is WARNED once, before its session is restarted, with a message that offers `/new` instead (`messaging.commands.context_recycle_warning`, `context_recycle_warning_zh` on the Chinese surfaces). That warning is keyed to the session's live `session.autocompact_pct` rather than the channel's `soft_threshold_pct` — it fires in `[threshold - CONTEXT_WARN_MARGIN_PCT, threshold)` (`recycle_warning_due`), because the channel soft default (80) sits above the recycle default (70) and a warning keyed to it would land after the restart — and the per-conversation latch is cleared outside that band so the next fill after a restart warns again (#11948). Gating covers only command dispatch — KAS auto-summarization frames keep mapping to compaction status.
- Dashboard manual `/compact` while conversation-history replay is still pending (the replay lease is armed and the provider session holds nothing yet): it answers at once with `COMPACT_REPLAY_PENDING_NOTICE` ("Nothing to compact yet: the restored history is delivered with your next message."), awaits no compaction status, does not re-arm skills context, and leaves the replay lease armed for the next prompt.
- Dashboard manual `/compact` on a fresh provider session no ordinary prompt has reached yet (a brand-new chat; the first-turn history debt `first_turn_history_owed` is still armed) on a backend that does not compact inline: kiro-cli ends that turn with "Conversation too short to compact." and no compaction status, so it answers at once with `COMPACT_FRESH_SESSION_NOTICE` ("Nothing to compact yet: the agent's session starts with your next message."), awaits no compaction status, does not re-arm skills context, and leaves the debt armed for the next prompt (#15722).
- `cancel()` → sends `session/cancel` notification
- `supports_effort()` / `change_effort(level)` / `clear_effort()` → reasoning-effort control (see below)
- `is_alive()` → `AcpClient.is_responsive()` (600s stale threshold)
- `is_process_alive()` → OS-level process check

**Reasoning effort** (Opus/Sonnet/Fable **and GPT-5.x** — shared vocabulary in `effort.py`: levels `low|medium|high|xhigh|max`, resolution via `resolve_effort_for_model` with priority slot-override > workspace default > None). Capability authority is per harness. For kiro and claude the registry answers (`model_supports_effort`). For members of `ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION` (pi, deepseek, goose, opencode) the session's advertised config option answers `supports_effort` (False when the advertised list contains only `off` — an off-only option signals no real thinking level; for every member `_sync_effort_levels` admits the advertised levels to the validation set through `register_reasoning_effort_values` and never replaces the global fallback order, which a slot whose provider advertises no effort option of its own, kiro-cli, is served; after a live `set_model` the option read is the one the `model` write's own `session/set_config_option` result carries, which `AcpClient.set_config_option` stores as `AcpSessionHandle.set_config_option` does, because the `config_option_update` that precedes it is only buffered by `_wait_for_response`) and the allowed levels: `_resolve_effort` passes `levels=` (the advertised list) and `normalize=` (`effort_config_option_value`, the backend's spelling of a level) to `resolve_effort_for_model`, so the fold runs before the advertised-list filter and before the write. A Crew level a member has no spelling for (deepseek's `medium` and `xhigh`) is hidden, not mapped: `AcpProvider.accepts_effort_level` answers False for it, the dashboard effort route refuses it with a 400 `effort_level_not_offered` before anything persists, `change_effort` raises on it, and `_set_effort_config_option` writes nothing for it and walks its step-down over advertised rungs only, so every value pushed to a member is one that session advertised. The registry capability is a conservative allowlist of known-capable families (`opus`/`sonnet`/`fable`/`gpt`, plus Haiku 5.5 and later through a version gate that refuses every older or unversioned `haiku` id before the registry is asked, since the claude_code registry folds `claude-haiku-4.5` onto an effort-capable Sonnet), verified against kiro-cli 2.12/2.13 over ACP and, for `claude-haiku-5.5`, kiro-cli 2.31.1 — kiro rejects `/effort` on the other third-party models (deepseek/minimax/glm/qwen/auto) with "Effort configuration is currently not available on <model>". A new model family lands as unsupported until confirmed (safe default: the slider hides). On kiro-cli (`ACP_BACKENDS_EFFORT_OVERLAY`) it is applied via a workspace `cli.json` overlay at `<work_dir>/.kiro/settings/cli.json` → `chat.modelDefaults.<model>.<key>.effort`, written before every spawn (`_write_cli_overlay`) and read back on init (`_read_cli_overlay`) so the provider's map names the level kiro-cli will run. The overlay's scope is the work dir, so every session in one project shares one entry per model. KAS never opens it and takes the level as the `effortLevel` config option, so the file is neither written, read back nor cleared for it: read back there, it started a new chat at whatever level another chat in the same work dir picked last. A KAS slot keeps its pick across a gateway restart through its own transcript metadata, which the dashboard hands the factory as `reasoning_effort_override`, and across a compaction restart or reset successor through the session's recorded override, which the dashboard effort route replaces with the live pick (`SessionManager.record_effort_override`). A session the dashboard does not start (cron, Slack, webhook) resolves its level from config (crew pin, role, `agent.reasoning_effort`) on every cold start: a level picked live on such a session from a dashboard slot reaches its successors, but a cold start from the channel side reads no dashboard slot, for effort as for the model. The `<key>` sub-object is **family-specific** (`effort_settings_key`): `output_config` for Claude models, `reasoning` for GPT models — kiro silently ignores the wrong key, so a mismatched shape would survive a live push but drop on respawn. `_write_cli_overlay` removes stale effort from the other family key while preserving unrelated settings; `_clear_cli_overlay_effort`/`_read_cli_overlay` sweep both keys. Every in-product writer of that file takes the shared `.kirocrew-cli-settings.lock` sidecar (see [acp-client](acp-client.md)), under one of TWO ceilings: a native launch takes the startup one (it cannot wait, and a launch that loses the lock falls back to authored agents), while an operator action takes a ceiling far above projection's own sub-second critical section. `_clear_cli_overlay_effort` is an operator action, so it runs through `asyncio.to_thread` — waiting that long on the loop would freeze every session — and losing the lock there means a stuck holder rather than routine contention. The WRITE side is the same shape and gets the same treatment: `change_effort` applies the overlay off-loop under the action ceiling and refuses to push live at all unless it persisted, rolling the per-model override back and raising, because the file is what a respawn reads — reporting a live change over a write that did not land promises a level the next reset undoes. The pre-spawn overlay writes keep the startup ceiling on the loop: a launch cannot wait, and a launch whose overlay is missing still pushes at spawn. That is what keeps its answer two-valued: a failure that happened by design would need a third state for "nothing changed", and one that means something is wrong does not. It returns whether the file stopped naming the model: an absent file, an absent entry and a malformed file are all successes (`_read_cli_overlay` reads a malformed file as `{}` too, so nothing re-seeds), while a lock failure and a read that fails on a file that EXISTS under the held lock leave the level on disk. `clear_effort` answers that case with a THIRD outcome (`None`), not a bool: both bool values make the handler commit the cleared slot value — the reset branch before its teardown, and the success path before its final 200 — so either would show "default" over an overlay that still holds the level, and `False` additionally resets into re-reading that same level. `None` means nothing changed on either side, and the handler answers it with a retryable 409 `effort_overlay_busy` that commits nothing and resets nothing. `change_effort` has no such case: it raises, because a change that did not persist is a failure rather than a no-op. Clearing to a resolvable workspace default runs under the same write-first discipline: it applies the overlay off-loop under the action ceiling and pushes `/effort` only once that landed, because a live push over a write that did not persist reports a default the next spawn replaces with the level just cleared — a failed write there restores the popped override and answers `None` as well. `clear_effort` must not report a clear on that answer — the level is still on disk and `_read_cli_overlay` re-seeds `_effort_per_model` at construction, so the session reset a clear asks for would re-apply it. It restores the popped entry and logs a warning naming the model, and returns True — not False: the handler resets the session on False, and that reset would re-read the same level, so the operator would pay a reset and still run the old effort. True here means "no reset needed", which is the true answer when nothing changed on either side; the warning is what asks for a retry. The `change_effort` rollback path restores the level the same way for the same reason, in both of its shapes: the one that clears an overlay its live push never applied, and the one that rewrites the overlay back to the prior level. When either write loses the lock the file keeps the attempted level and construction re-seeds from it, so the map follows the FILE and a warning names the level a respawn adopts — reporting an undo the next spawn contradicts would be worse than admitting the rollback is incomplete. Live change pushes `/effort` with the TuiCommand args form (`send_command(args={"level": …})`). The factory threads `reasoning_effort_override` → `effort_per_model[current_model]`, keyed by the configured spelling; on the claude client path `start` moves that entry to the model the session records after `ensure_ready` (claude records the advertised spelling, e.g. `claude-opus-5-5[1m]` for a `claude-opus-5.5` pin), and leaves it in place when every spelling was refused and the session runs the backend default. For an advertised-option harness the factory carries the level forward and leaves judgement to `_resolve_effort` once `session/new` has advertised the option, so a cold start does not drop it on the registry's answer. When nothing is pinned (the resolved model is empty, as with an explicitly empty `agent.model` or a pin that collapses to `auto`), the factory passes a valid level, or any level on an advertised-option harness, as `unbound_effort` instead of keying it, because no model id exists until the backend answers `session/new`. Every effort read and write keys on `_effort_model`: the pinned model, else the backend-reported `served_model` once known. `_bind_unbound_effort` runs on every (re)start, after the session is ready and before `_apply_initial_effort`. It stores the level under the served model, replacing any level already stored there: a live change moves the unbound level with it, so a stored level that differs can only be one a previous run left in the workspace overlay, which construction seeds the map from on kiro-cli, and keeping it would run the session at a level the caller did not ask for. On the kiro family, whose level otherwise rides the spawn-time overlay, it pushes the level live through `change_effort` whenever the overlay does not already hold it, which also writes the overlay for the next spawn. That covers an unpinned kiro-cli, which is launched without `--model` and serves its own `chat.defaultModel` when one is set. Before that call the map holds the level the session was spawned with (the overlay's, or none), so a push kiro-cli refuses rolls back to that level rather than leaving the requested one in the map and the overlay while the session runs another. kiro-cli reports `currentModelId` `auto` for an `auto` session, and `served_model` filters that to `""`, so such a session still offers no effort, as before. A level the serving model does not take, or one with no served model at all, is logged as not applied once per provider. `clear_effort` drops the unbound level so the next restart does not bring it back, and a successful `change_effort` replaces it. Otherwise, when a valid requested effort cannot be threaded on a cold start (the resolved model is not effort-capable) the factory's gate logs one warning naming the level, the session, and the resolved model — reporting its own drop decision on surfaces that construct a fresh provider, an explicit `reasoning_effort_override` always warns (a caller's own dropped request is the event the gate exists to surface), while a drop sourced only from the config default (`agent.reasoning_effort`) is deduped once per (model, level) for the factory's lifetime so one static configuration fact does not repeat on every construction. A `reasoning_effort_override` on a warm-pool claim is applied post-claim via `provider.change_effort` (updating `_effort_per_model` and the `cli.json` overlay write) rather than bypassing the pool, recovering pool-hit startup latency; if the claimed model does not support effort, `change_effort` returns False and a corresponding drop warning is logged. The dashboard handler routes through `change_effort`/`clear_effort` and only resets the session when there is no live provider. Non-effort-capable models persist the slot value without a live apply or reset.

**MCP Tool Search** (kiro-family backends — see https://kiro.dev/docs/cli/mcp/tool-search/): loads MCP tool specs on demand ("search-and-call") instead of sending every tool definition each turn, keeping the context window clear when many MCP servers are configured. Gated by the `agent.tool_search` config toggle (default **on**; auto-surfaces as a Settings toggle since the schema is generated from the dataclass).
- **kiro-cli (Rust engine):** applied via the **same** workspace `cli.json` overlay used for effort (`<work_dir>/.kiro/settings/cli.json`), written deterministically before every spawn and on each restart by `_write_tool_search_overlay` (called from `AcpProvider.__init__` and `start()`), scoped to `ACP_BACKENDS_TOOL_SEARCH_OVERLAY`. The engine activates deferral only when the agent's `tools` also grants `tool_search`, so on this channel the loader invariant is the engine's. When enabled it writes the flat keys `toolSearch.enabled=true` plus `toolSearch.minPct`/`toolSearch.minTokens`, taken from `agent.tool_search_min_pct` / `agent.tool_search_min_tokens` (defaults `5` / `50000`, mirroring kiro-cli's own thresholds; clamped to 0-100 and >= 0, non-numeric falls back to the default); when disabled it writes `toolSearch.enabled=false` and drops both thresholds.
- **Why the thresholds are not forced to 0:** deferral costs a round-trip — a deferred tool's spec is absent from the model's tool list, so the first direct call fails with `A tool with the name '<name>' does not exist` and has to be recovered with `tool_search`. That only pays once the specs are genuinely large, which is what the thresholds express (kiro-cli defers when EITHER is exceeded). Setting both to `0` restores unconditional deferral for operators who want it. The thresholds are always written **explicitly** rather than omitted, so a value already in the file is replaced rather than silently kept.
- Writing both `true` and `false` makes the Kiro Crew toggle authoritative over any value in the user's global `~/.kiro/settings/cli.json`. The write is merge-safe with the effort `chat.modelDefaults` keys in the same file.
- kiro-cli reads workspace settings only from `<cwd>/.kiro/settings/cli.json`, so the overlay cannot move out of the work dir. When the work dir is a git work-tree top, the shared overlay lock adds the overlay and its lock to the repository's local `info/exclude`, keeping the user's `git status` clean (see [acp-client](acp-client.md)); an already-tracked `cli.json` is not hidden.
- **KAS:** never opens `cli.json`; it reads the setting from the ACP `initialize` request, `clientCapabilities._meta.kiro.settings.toolSearch` — a process-wide channel filled by `AcpRuntime._handshake_client_capabilities`, entered from the spawn path only when the harness answers `client_meta_settings` (the harnesses in `ACP_BACKENDS_CLIENT_META_SETTINGS`; every other host reads its constant with no new step), from the same `agent.tool_search*` values (`agent_sdk/tool_search.py`). KAS 2.34's deferral path reads none of `enabled`, `minPct` or `minTokens`: it defers **every** MCP tool whose `<server>::<tool>` id is not on `toolSearch.neverDefer` (and whose server is not an operator mandatory server), and injects its own `tool_load` / `tool_call` to reach them. So the client keeps the invariant: for a spawn agent whose spec grants no `tool_search`, `neverDefer` lists every MCP tool that spec can call (`agent_sdk/tool_search.kas_never_defer_ids`) — `@server/tool` as `server::tool`, a tool glob and a whole-server `@server` grant expanded from the names Crew already holds for that server (`acp/runtime_spawn_launch._known_server_tool_names`: the last probe's `tools/list`, read from the probe cache; it never imports a Crew server's declaration module into the gateway, because that package is writable by the agent's uid), `"*"` as every server in the spec's `mcpServers`; KAS has no server-level or wildcard `neverDefer` form, so a server Crew holds no list for stays deferred and reachable through KAS's `tool_load`, which is the host's behaviour without the list. A spec that grants the loader is sent no `neverDefer`. `enabled` is still sent, as `true` only when the spawn agent's spec grants `tool_search` — named in `tools`, or via `"*"` / `@builtin` — and does not take it back in `excludedTools`; it is sent as an explicit `false` otherwise — never omitted, so a host default cannot flip a session into "deferred, unreachable". The spec judged is the one the KAS projection puts on the wire (`AcpRuntime._projected_spawn_spec`: the freshness gate's snapshot for a derived agent, else `acp/harness/kas.resolve_projected_spec`, the call the projection itself makes, after the same `ensure_agent_materialized` self-heal the projection runs) — so a project checkout's `.kiro/agents` spec is judged exactly when it is the one projected; an unreadable spec grants nothing. The setting is process-wide, so every later `create_session` on a deferral-enabled process judges the payload it is about to send: a projection that grants no loader — another agent, or the same agent whose spec has since lost the grant — is refused with `AcpToolSurfaceBindingError` (an `AcpWorkspaceBindingError`), which the run-runtime caller already answers by giving that session a runtime of its own. The check lives in `AcpRuntime._kas_custom_agents`, the projection seam both `create_session` and `load_session` go through, so a resumed session is judged the same way. It is deliberately one-directional: a process whose handshake sent `enabled: false` may serve a later session whose agent does grant the loader — that session simply runs with full tool specs, today's behaviour and never a loss of tools — whereas the reverse pairing loses every MCP tool, which is the only shape worth a refusal. Every KAS-capable runtime constructor threads the same resolved settings — the foreground provider, its resume-respawn fallback, the companion-runtime kwargs mirror (`session_allocation._collect_parent_runtime_kwargs`, reading the parent's `LLMProvider.tool_search_settings` capability — default `None`, answered by `AcpProvider`; harness-parity H14) and the background runtime (`session_background`) — so no KAS process is left to the host's default. KAS does not honour the toggle or the two thresholds — deferral there is all-or-nothing, with `toolSearch.neverDefer` as its keep-list, so an operator who turns `agent.tool_search` off still gets deferral on KAS for a spec that grants the loader — but they are forwarded verbatim so the setting means one thing.
- **Non-kiro-family backends** — no-op on both channels. `_apply_tool_search_overlay` returns early when the backend does not read the overlay, the handshake channel is filled only for members of `ACP_BACKENDS_CLIENT_META_SETTINGS`, and both are silent when no toggle value was threaded in (`tool_search is None`).
- **Native-resume compatibility:** for a direct dashboard turn with Tool Search enabled (dashboard session identity and no resumable linked-channel identity), the kiro backend does not use `session/load`. Before provider acquisition, dashboard chat resolves the slot's dedicated Slack field unconditionally and uses the channel-neutral `SessionMap.mirror` link only when `mirror_accepts_inbound` is true. Thus inbound-capable Telegram/Discord links remain distinguishable when they reuse a `dashboard:*` key after restart, while outbound-only iMessage/WhatsApp mirrors still take the direct-dashboard recovery path. A loaded transcript can return without Tool Search's activated schemas: `tool_search` reports a match, but the next inference still cannot invoke that tool. The provider instead creates a fresh native session and sets `_history_replay_needed`, so `SessionManager` marks conversation replay pending against the rebuilt tool registry. That decision is the pure module-level `resume_takes_tool_search_replay` in `agent_sdk/tool_search.py` — below both callers, because the dashboard may not import the ACP layer (`scripts/check_agent_sdk_boundary.py`) — (Tool Search on, `ACP_BACKEND_KIRO`, no channel identity, a key `telemetry_channel_of` classifies as `dashboard`) — the ONE definition, read by `_start_kiro_runtime_impl` once a resume sid is in hand and by the dashboard's resume prefetch (`chat_turn.eager_spawn._eager_spawn`) BEFORE any runtime exists: a speculative load the provider replaces this way can only come back `resumed=False` and be refused after a full spawn and teardown, so the prefetch logs `left to first turn (tool-search replay)` and spawns nothing, and the first real turn takes the fresh-session path described here. On that turn the replay lease also outranks a `resumed` observation: `chat_runner._run_chat` treats the provider as holding no history whenever the lease is armed (`_replay_pending`), so the turn builds the full first-turn context with the replay and never takes the slim-resume path that tells the model its history was restored. Only this direct-dashboard compatibility branch also makes `defer_replay_sid_promotion` true; `SessionMap` keeps the prior full-history SID durable while that lease is pending, allocation does not publish the fresh SID yet, and `close_all()` refuses to overwrite the retained mapping while `provider_switch_replay` remains armed. Generic `session/load` recovery still requests history replay but publishes its fresh SID immediately because non-dashboard dispatchers do not own the dashboard settlement contract; a later channel restart therefore resumes the recovered native transcript rather than the stale pre-recovery SID. Replay settlement runs from the dashboard turn's `finally`, so exceptions, task cancellation, early returns, and synthetic terminals cannot bypass it. A landed, non-synthetic replay-bearing `end_turn` promotes the fresh SID, as does confirmed `/clear` after native history deletion. Cancellation, incomplete streams, and every other non-committed terminal leave the prior SID in place and re-arm replay, so a second gateway restart cannot strand a slash-only or discarded transcript. That lease drives every replayable dashboard session-start prompt block (history, ContextBuilder, member context, folder, persona, and context telemetry). `AgentSpawn` hooks remain keyed to the actual provider spawn so script side effects execute once; a slash-first turn does not re-fire them during replay. Non-destructive native slash commands bypass `ContextBuilder` and leave the lease intact; a confirmed `/clear` consumes it at `EVENT_CLEAR_STATUS` so later replay cannot restore deleted history. Authorization, shutdown, or pre-dispatch Stop aborts preserve it. Async stream creation is not acceptance: a replay-bearing non-slash turn records acceptance in runner-local state only when its stream yields the first provider event, while the shared lease remains armed throughout the in-flight turn. Empty streams and pre-output failures therefore retain replay without settlement, and a concurrent shutdown can observe only the still-pending old SID. Final settlement synchronously promotes the fresh SID and consumes the lease only for a landed, non-synthetic, non-empty `end_turn`; every empty-response verdict is unlanded for replay durability, including the terminal give-up rung when retry budget is exhausted or auto-continue is disabled. If an accepted turn ends cancelled, the still-armed lease carries forward because kiro-cli discards that turn; the next prompt receives the full older replay plus its cancelled-turn preamble. This trades the native resume latency win for a usable dashboard tool surface without losing prior conversation or undoing an explicit clear. Setting `agent.tool_search=false` keeps dashboard-native `session/load`; a dashboard-keyed resumable channel turn and every channel dispatcher remain on native resume regardless of Tool Search.

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
- `agent.acp_backend` is the harness choice, resolved through `agent_sdk.backends.resolve_selected_backend` (the top-level `acp_backends` module is a re-export shim kept for existing call sites). A stored Kiro CLI pick (`"kiro"`) resolves to KAS once the gateway confirmed the host's kiro-cli offers v3 and while KAS is selectable, so the factory builds a KAS provider for it with KAS's launch, projection, permissions and compaction; it builds the Kiro CLI provider without that confirmation ([harness-parity](harness-parity.md) H1). The Kiro CLI provider, its skill-view aliases and the `cli.json` overlay stay for that case.
- `create_provider_factory()` returns a `Callable` that builds an `AcpProvider` for the resolved backend.

An agent spec's model is consumed by kiro-cli before Kiro Crew reaches
`session/new`, so the live-session entitlement guard cannot diagnose a wrong
wire spelling at spawn time. Agent create/update validate a pin before
persisting it: they reuse the role-model validator for advertised ids and
`model_registry.acp_id_correction` for the offline positive case where the
registry recognizes a non-ACP spelling and can name its ACP id. With no live
catalog on a backend in kiro-cli's `acp` namespace, they also refuse a pin
`model_registry.lacks_acp_id` flags: a registered model with no ACP id. Ids the
registry does not know are allowed with no live catalog because they may be
valid regional or newly released ids; empty and `auto` continue to defer. Doctor
applies the same correction audit to every discoverable user- and project-scoped
spec.

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

## Turn helpers: `llm_helpers.py`

`kiro_crew.llm_helpers` runs one prompt to its answer on any `LLMProvider`: the
unattended surfaces (cron, heartbeat, the task runner, sub-agents, workflows, one-shot
background prompts) call `stream_and_collect` or `run_bg_oneliner` through it, and the
interactive `chat_runner` loop reuses its error classifiers and backoff curves. The
module is a mirror facade (`kiro_crew._facade_mirror`) over the owners in
`llm_helpers_parts/`, so every name still imports and patches as
`kiro_crew.llm_helpers.<name>`, and no owner imports the facade. New code goes to the
owner of its responsibility:

| Owner | Owns |
|---|---|
| `backend_errors` | which backend failures a turn retries and how long it waits: the prompt-busy and transient budgets and their backoff (the throttle curve included), the transient, throttle, session-not-found, connection and context-overflow classifiers, the network-drop recovery window |
| `turn_usage` | what a turn cost and which backend served it: the billing-stats walk, the turn's summed `TurnUsage`, the usage row's backend label, the `interaction` telemetry event |
| `model_fallback` | the `agent.fallback_model` chain and its story, the restore probe, the substitute `set_model` seam, the route-baseline latch and the per-session model-switch lock |
| `tool_input_denials` | the deny checks a tool call's title and input run through before anything approves it |
| `tool_approval` | `ToolApprovalPolicy`, `_resolve_permission`, and refusing a call by steering the host first |
| `turn_results` | parsing JSON out of model text, and saving a turn to history off the event loop |
| `turn_stream` | `stream_and_collect` and `stream_and_collect_json` |
| `background_turns` | `run_bg_oneliner` and `background_turn` |

The owners read the ACP layer's names (the `EVENT_*` kinds, `LLMEvent`, `LLMProvider`,
`AcpError`, `TurnUsage` and the rest) from `agent_sdk/drivers/acp_turn_vocab.py`, so no
owner imports `kiro_crew.acp` or `kiro_crew.providers` itself
(`scripts/check_agent_sdk_boundary.py`).

## Automatic recovery

Provider-level recovery mechanisms that fire automatically without user intervention:

**Interactive transient-5xx retry:** the interactive dashboard/Slack `chat_runner` stream loop retries a transient backend 5xx (InternalServerError / DispatchFailure / ConnectionReset, JSON-RPC `-32603`) through the shared `llm_helpers` transient classifier + backoff, **without** resetting the still-alive session. Auth/validation errors are excluded (fail-fast); on retry-budget exhaustion a clean error surfaces on a still-resumable session. A throttle (`AcpError.throttle`, read by `acp_error_is_throttle`) waits on `throttle_retry_delay`, the same doubling curve from `THROTTLE_RETRY_BASE_DELAY` (6 s, about one KAS attempt's own internal retries) instead of 2 s, and each verbatim re-send of a throttled turn first calls the provider's `discard_unanswered_prompt`: on KAS that sends `_kiro/checkpoint/revertMultiple` for the failed prompt's row id (the `user_message_id_assigned` session_info_update), so the model's history keeps one copy of the prompt rather than one per attempt. It sends nothing off KAS, without the advertised `_meta.kiro.checkpoints`, after a later turn or a steer, and a failed rewind leaves the copy, so the retry goes out either way. The unattended `stream_and_collect` path uses the same classifier, but its same-model and fallback-chain retries replay the original prompt only while no assistant text or tool call has occurred across any attempt. Any fired tool call makes a later transient error terminal on that path, including when the tool completed without producing text.

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
the runtime predicate key off `ACP_BACKENDS_KIRO_FAMILY` instead, and each
overlay writer is scoped again to its own reader set (harness-parity H6). A
harness author looking for "the one gate" is looking at the frozensets
themselves; `harness_for()` serves a host whether or not a capability set names
it.

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

The work ledger (`kirocrew-work`) is admitted only as that gateway broker stub,
and only in its exact shape: a stdio element running the broker stub module
under one `--server kirocrew-work`, whose `env` is exactly one stub-token pair
equal to this session's nonempty token. The same restriction, `disabled` and
`tools` checks apply. A spec-described work-ledger entry, an unbound or foreign
stub, and every other identity-bound server stay withheld.

On an enforced sandbox, credential-bearing host readers need the broker route:
configure `mcp_gateway.stub_servers` to include `kirocrew-core` and
`kirocrew-dashboard`. Direct children cannot read the gateway credential or SEL
trust root there; the Codex dashboard mount does not relax those masks. Global Codex MCP entries
are not a substitute for this session mount.

### opencode work-ledger stub mount

The opencode mirror (`providers/mirrors/opencode.py`) withholds every
identity-bound Crew server, from both the spec-described half of the array and
the pooled broker stubs. The work ledger (`kirocrew-work`) is admitted only as
the gateway's broker stub, and only in its exact shape: a stdio element running
the broker stub module under one `--server kirocrew-work`, whose `env` is
exactly one stub-token pair equal to this session's nonempty token. It still
passes the `tools` allowlist and `disabled`, and a switched-off tool keeps the
stub mounted only while its deny rule is in force. A spec-described work-ledger
entry, an unbound or foreign stub, and every other identity-bound server stay
withheld. The admission is opt-in on the shared `place_single_binary_array`, so
goose, copilot and deepseek are unchanged.
