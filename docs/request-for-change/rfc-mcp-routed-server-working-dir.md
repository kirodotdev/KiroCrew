---
title: "MCP gateway: routed servers and the session working directory"
status: draft
author: kirocrew agent session, directed by the repository owner
created: 2026-11-12
last-audited: 2026-11-12
audited-at: b2aac45f3a
doc-pr: null
implementation-prs: [36277]
tracking-issues: [36209]
supersedes: []
superseded-by: []
---

# RFC: MCP gateway: routed servers and the session working directory

**Status.** Two parts with two states. Decision F (section 4) was made by the
repository owner on 2026-11-12 in the conductor chat ("the key is correctness")
and is built in [#36277](https://github.com/kirodotdev/KiroCrew/pull/36277)
(branch `fix/mcp-share-by-cwd`); section 4 describes F as that PR builds it, and
its spec section "The working-folder question gates routing" in
`docs/system-specs/modules/mcp-shareability.md` is the authority. Proposal C (section 5)
is a draft: acceptance is requested from a maintainer, and the status flips when
one records it here. This document changes no code. Every code reference below
names a symbol as it stands on main at `b2aac45f3a`.

## 1. Problem

A server the gateway routes (its kiro-cli entry rewritten into a Crew stub, its
real process spawned by `gatewayd`) always runs in one fixed directory, never in
the session's working directory.

- `_backend_work_dir` in `src/kiro_crew/mcp_gateway/daemon/launch.py` names the
  gateway's own default dir, `workspace_root()/_default`. `env_target_resolver`
  in the same file spawns there whatever directory the stub's register frame
  carried, and logs the mismatch.
- The rewriter writes that same default as every stub's `--work-dir` (docstring
  of `_restore_default_work_dir` in `src/kiro_crew/mcp_gateway/backend.py`), so
  `PoolKey.work_dir` (`src/kiro_crew/mcp_gateway/pool.py`, filled by
  `PoolKey.from_register`) is the same value for every session.
- The process runs outside every session sandbox, as the user. This is an
  accepted risk, stated in the "Security boundary" paragraph of `spawn_backend`
  (`backend.py`) and in `docs/system-specs/modules/security.md` under the launch
  approval: "a shared backend runs in `gatewayd` outside every session sandbox,
  as the user".

An unrouted server is launched by kiro-cli inside the session sandbox, in the
session's directory. So routing a repo-bound server (git, file search, code
index, a test runner) silently changes what it reads: every session gets the one
fixed directory. Nothing errors.

Real case: a user ran per-worktree sub-agents. Each sub-agent's repo-bound server
was routed and shared, so all of them answered from one repository.

## 2. Rejected: honour the session's directory as it is today

The obvious fix is to spawn each backend in the directory the session names. It
is a sandbox escape:

1. The backend is an unsandboxed interpreter. `python -m <module>` puts the cwd
   first on `sys.path`; `npx <bin>` resolves a repository's own
   `node_modules/.bin` first. A worktree is agent-writable, so an agent inside
   the sandbox could plant `<module>.py` or a bin and have it run outside the
   sandbox, as the user.
2. The directory would come from a register or claim frame. Those are trusted
   only because the peer is the same user (`check_peer_is_self` in
   `src/kiro_crew/mcp_gateway/socketsec.py`); any same-user process, including
   sandboxed code, can send one. The docstring of `_backend_work_dir` gives
   exactly this reason for ignoring the frame, and `_restore_default_work_dir`
   refuses to create a frame-chosen path for the same reason.
3. The launch approval pins command, args and env only (`security.md`, same
   paragraph), not the directory, so it would not cover the new risk.

The cwd is pinned for every routed server, shared or not. Sharing is not the
cause; routing is. One exception: when the gateway's default dir cannot be named
(`_backend_work_dir` returns `None`), `env_target_resolver` falls back to the
frame's directory. C does not keep that fallback for a `uses_cwd` server: with
no sandboxed spawn available it refuses, and the server stays on the session's
own launch.

## 3. Rejected: guess from the server's config which ones use the directory

A heuristic (relative paths in `args`, a `.` argument, a known command name)
decides correctness. A miss is a silent wrong answer, which is the bug itself.
Signals it cannot see:

- a wrapper script (`./run-mcp.sh`, `uvx some-wrapper`) that reads the cwd inside;
- a server that calls `os.getcwd()` or reads `$PWD` with no argument naming it;
- a server that asks the client for MCP roots (section 6.1);
- `args` edited after the routing decision was made.

`src/kiro_crew/mcp_gateway/shareability.py` already holds the same line for
sharing: its module docstring, "Why a recommendation and never an automatic
switch", treats evidence as an invitation to a human decision.

## 4. Decision F: route only what the user has cleared

Built in #36277.

1. **Routed without asking: Kiro Crew's own servers only**
   (`mcp_cleanup.KIROCREW_BIN_MCP_SERVERS`), whose launch the rewriter re-derives
   from the install.
2. **Every other server** routes only after the user answers in MCP Management
   that it does not use the project folder. This includes a third-party server
   that declares `kirocrew.caller-identity` (the extension says nothing about the
   folder) and a stdio remote proxy such as `mcp-remote <url>` (a proxy cannot be
   told apart from a local server by its command alone, which is the
   command-name heuristic section 3 rejects).
3. **Servers already routed before F** keep their approval and keep running
   routed until answered. Their row asks once. They are never unrouted silently,
   because that would change a working setup without telling anyone.

The answer is stored per server name in `mcp_gateway.cwd_use`: `uses_cwd`,
`not_used`, or absent (unanswered). Answering `uses_cwd` unroutes the server at
the next start. A renamed server is unanswered. F does not change where a routed
process runs. It only stops routing a server nobody has said is safe to pin.

## 5. Proposal C: per-directory backends inside the session sandbox

The goal is to route a `uses_cwd` server too, without the escape in section 2.

### 5.1 The answer is the switch

The answer F records decides the pool key:

| Answer | `PoolKey.work_dir` | Where it runs |
|---|---|---|
| `not_used` | the gateway default (as today) | one shared trusted backend, unsandboxed (as today) |
| `uses_cwd` | the session's directory, from the Crew claim | one backend per directory, inside the session sandbox |
| absent, server not routed before F | not routed (F) | the session's own launch, as today |
| absent, server routed before F | the gateway default, until answered (F) | as today, until answered |

C never builds a per-directory key from an absent answer: only `uses_cwd` gets
one, and only `not_used` gets the shared trusted backend.

**Why the answer alone cannot put cwd in the key before C exists.** A per-cwd key
with the backend still spawned in the fixed dir splits the pool and changes
nothing the server sees. Spawning it in the session dir without the sandbox is
the escape of section 2. The key change and the sandboxed spawn must ship
together.

### 5.2 Spawn

- Wrap the backend argv with `wrap_argv` (`src/kiro_crew/sandbox_wrap.py`). It is
  used for agent processes today; no gateway backend calls it (the only mention
  under `src/kiro_crew/mcp_gateway/` is the comment above `_SENSITIVE_ENV_PREFIXES`
  in `manager.py`).
- Its `exec_cwd` option fails closed unless the directory is read-only and empty
  (its entry in the `wrap_argv` docstring), so it cannot carry a worktree as-is.
  C either spawns the wrapped argv with `cwd=<session dir>` the way the agent
  launch does, or adds a launcher option. To be settled in the implementation PR.
- The sandbox must give the backend the same view the session has, so it reads
  what the agent reads and writes no more.

### 5.3 Where the directory comes from

- From the claim Crew itself sends before `session/new` (`send_claim`, called by
  `_own_stub_session` in `src/kiro_crew/acp/runtime_session_setup.py`), never from
  the stub. The frame has no directory field today (`build_claim_frame` in
  `src/kiro_crew/mcp_gateway/claim.py`); C adds one.
- Validate it: it exists, it is a directory, it is under the session's allowed
  roots. Anything else is refused: a `uses_cwd` server then stays on the
  session's own launch, never the unsandboxed default dir.
- The sandbox, not the claim, is what makes this safe: a forged directory gets a
  sandboxed process that sees only what that sandbox allows.

### 5.4 What the trusted side gets today, and must not leak in

The comment above `_SENSITIVE_ENV_PREFIXES` in
`src/kiro_crew/mcp_gateway/manager.py` keeps `SLACK_BOT_TOKEN`/`APP`/`USER` and
`KIROCREW_OWNER_ID` in every pooled backend's env on purpose, because backends
sit on the trusted side. The boot `SSH_AUTH_SOCK` is added back for named
backends (`boot_agent_socket` in `src/kiro_crew/mcp_gateway/agent_socket.py`).
`secret://NAME` env values are resolved at spawn (`resolve_secret_uris` in
`src/kiro_crew/mcp_gateway/secret_uri.py`).

A sandboxed per-directory backend runs next to agent-writable files. C must
decide, per item, whether it keeps it:

- Slack tokens and owner id: dropped, as `wrap_argv` drops them for agents.
- SSH agent socket: only when the server is named for it, as today.
- `secret://` values: resolved only for the server that declares them, and the
  resolved value must not be readable by the agent (env of a sibling process in
  the same sandbox can be).

### 5.5 Pool lifecycle

- **Warm pool.** A warm-pool worker spawns before its session key is known and is
  named later by `rekey()` (docstring of `_own_stub_session`;
  `AcpSessionProvider.rekey` in `src/kiro_crew/acp/session_provider.py`). A
  `uses_cwd` stub must not attach to a backend until the claim naming its
  directory arrives; until then it uses the session's own launch.
- **Idle reclaim.** One backend per active directory means more processes.
  `_idle_sweeper` (`src/kiro_crew/mcp_gateway/daemon/sweepers.py`) and
  `BackendPool.evict_idle` (`pool.py`) already reclaim idle backends; C needs a
  shorter idle limit for per-directory keys, set in the implementation PR.
- **Prewarm.** The hot-key tally (`src/kiro_crew/mcp_gateway/daemon/warm_pool.py`)
  must not prewarm a per-directory key for a directory no session is in.

### 5.6 Harnesses

The claim is sent by Crew's ACP runtime, so every harness that receives Crew
stubs needs it: KAS and kiro-cli go through `runtime_session_setup.py`; the other
harnesses under `src/kiro_crew/acp/harness/` (claude, codex, goose, opencode,
pi, strands and the rest) must each be checked. A harness that sends no claim
gets the session's own launch for a `uses_cwd` server, never the default dir.

### 5.7 Per OS

| OS | Sandbox backend (`detect_backend`) | C for a `uses_cwd` server |
|---|---|---|
| Linux | `namespace` | sandboxed per-directory backend |
| macOS | `sandbox-exec` (Seatbelt) | sandboxed per-directory backend |
| Windows | none (`wrap_argv` warns "Windows has no OS sandbox backend") | not routed; stays on the session's own launch |

Windows has no OS sandbox, so C must not route a `uses_cwd` server there.

## 6. Open questions, verified as far as the code goes

### 6.1 How is MCP `roots/list` answered for a shared backend?

Partly answered.

- `_handle_initialize` (`backend_handshake.py`) forwards the first stub's
  `initialize` to the backend and replays the cached result to every later stub.
  So the backend sees only the first client's capabilities, including whether it
  offers `roots`.
- A gateway-originated `initialize` (`send_initialize`, same file) advertises no
  `roots` capability.
- `roots/list` has no special handling. It is a server-to-client request, routed
  by `_route_backend_message` (`src/kiro_crew/mcp_gateway/backend_routing.py`) to
  the stub named by `_meta.relatedRequestId`, or to the only attached stub; with
  several stubs and neither, the backend is recycled.

Still open: what kiro-cli and KAS answer to `roots/list` (the session's cwd, or
nothing), and whether `notifications/roots/list_changed` from a second client
reaches a shared backend. Not verified here. Until it is, a server that relies on
roots counts as `uses_cwd`.

### 6.2 Are workspace `.kiro/settings/mcp.json` servers ever stubbed?

Answered: no. `rewrite_agents` (`src/kiro_crew/mcp_gateway/rewriter.py`) reads
only the global settings file, `source_dir.parent / "settings" / "mcp.json"`,
where `source_dir` is `~/.kiro/agents`. A workspace file is read by kiro-cli or
KAS at session start, and its servers run per session, inside the sandbox
(module docstring of `src/kiro_crew/acp/kas_settings_mcp.py`).

One caveat: a session-level stub wins over a same-named workspace server (module
docstring of `src/kiro_crew/mcp_gateway/session_servers.py`; docstring of
`hoist_servers_for_session` in `src/kiro_crew/acp/kas_agents.py`). So a workspace
server that shares its name with a routed global one is replaced by the routed
copy. F's question covers the global entry, which is the one routed.

## 7. Wording the user sees

Most users do not know the term "cwd". The question in MCP Management must work
without it.

- **Ask in plain words.** "Does this server work on the files of the project you
  are in?" Not "Does this server use the current working directory?"
- **Give examples next to the choices.**
  - *Yes, it works on project files*: git, file search, code search, a test or
    build runner, a linter.
  - *No, it talks to a service*: Jira, Slack, a ticket system, a web or cloud API.
- **Offer "Not sure".** It is recorded as `uses_cwd` ("Treated as Yes. This is
  the safe choice." in #36277): the server stays on the session's own launch.
  That is the safe and correct answer; it only costs pooling.
- **Say what each answer does**, in one line under the choice: "Yes: each chat
  starts its own copy in its project" / "No: one shared copy serves all chats".
- **Existing routed servers** get the same question once, with today's behaviour
  kept until the user answers.

## 8. Not covered

- No change to how Kiro Crew's own servers are shared.
- No change to the launch approval contract in `security.md`.
- No automatic detection; section 3 rejects it.
