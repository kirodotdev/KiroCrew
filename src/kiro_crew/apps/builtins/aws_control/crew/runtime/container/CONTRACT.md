# Container contract

Three processes run in one ECS task and share a filesystem. This file is the
only place their boundaries are defined. Read it before writing code, and if you
need something here to change, say so rather than changing it: all three tracks
import from the same base and a local fix becomes the next disagreement.

Design authority: `../share-my-crew/CHORUS-DOC.md` section 4 and 9.1.

| Process | Owns | Exposure |
|---|---|---|
| Kiro Crew backend | sessions, conversations, transcripts, MCP, subagents, skills, memory | loopback only, no interface served |
| Front process | authenticating the remote caller, stripping the crew prefix, forwarding a turn | the only listener the network reaches |
| Backup sidecar | copying session state to S3 and restoring it | outbound to S3 only |

Nothing here serves a user interface. The owner's control plane runs on the
owner's own machine and is out of scope for the container.

## Shared base, owned centrally

`container/common/` is written and owned outside the three tracks:

- `common.load()` returns a frozen `Settings`. Read the environment through it,
  never through `os.environ` directly, or the processes will disagree.
- `common.Settings.backup_unit()` is the authoritative list of paths to back up
  and restore.
- **`config_dir` equals `data_home`.** Verified against a running gateway:
  Kiro Crew's `config_dir()` and `data_home()` resolve to the same directory, so
  `session_map.json` and `open_slots.json` sit at the home ROOT, not under a
  `config/` subdirectory. The default was `data_home/config` at first, and the
  way that fails is the reason this line exists: the sidecar backs up every
  transcript and neither of those two files, so the backup looks healthy and the
  restore has no resume and no conversation list. The supervisor refuses to
  start when the two disagree.
- `common.read_boot_secret(run_dir, port)` reads the backend's per-boot secret.
  **Never cache the return value.** It is `os.urandom(16)` per boot and is not
  persisted, so a cached copy works until the backend restarts and then fails
  every request with a 403 that looks like a client fault.
- `common.CONTROL_SECRET_HEADER` is the one control header name. Three systems
  have to agree on it and two of them are not Python.
- `common.BACKEND_HOST` is `127.0.0.1` and is not configurable.

## Track ownership, no file is shared

| Track | Owns | Public seam other tracks may call |
|---|---|---|
| S1 front | `container/front/**`, `tests/test_front_*.py` | `container.front.app:build_app(settings)`, `container.front.__main__:main()` |
| S2 backup | `container/backup/**`, `tests/test_backup_*.py` | `container.backup.restore:run_restore(settings)`, `container.backup.sidecar:run_sidecar(settings)` |
| S3 supervisor | `container/supervisor/**`, `tests/test_supervisor_*.py` | `container.supervisor.backend:start_backend(settings)`, `:wait_until_ready(settings, timeout)` |

Held centrally: `container/common/**`, `Dockerfile`, `requirements*.txt`,
`app.json`, `deploy/**`, this file.

Do not import another track's internals. Only the seams above are stable.

## Startup order, owned by S3

The order is a correctness requirement, not a preference:

1. `run_restore(settings)` runs **to completion**. Nothing else has started.
2. The backend starts, and `wait_until_ready` returns only when the port answers
   **and** the boot secret file exists. Process-alive is not ready.
3. The front process and the sidecar start.

Restore must finish before the backend starts. Nothing truncates
`open_slots.json` at shutdown, but the backend's periodic flush writes whatever
is in its in-memory slot table, so a flush landing before restore completes
persists an empty set and destroys the record of which conversations existed
(`dashboard_persistence.py:148-161`).

## Backend facts the front process must respect

Established by reading the Kiro Crew source. Do not rediscover, do not contradict.

- Turn call: `POST /v1/chat/completions` with `{model, messages, id, stream}`
  (`openai_compat.py:154`, routed `routes/sessions.py:78`). Returns one JSON
  completion (`:566`) or an SSE stream (`:470`). `id` is the slot id and is what
  continues a conversation. No WebSocket is required.
- **The stream is OpenAI-format, not ACP.** Measured live: completion chunks
  carrying assistant text, keepalive comments, a terminal sentinel, and an error
  object on failure, with **no event names at all**. The ACP `sessionUpdate`
  vocabulary and the `tool_result` frame that carries tool output verbatim live
  on the owner control stream (`GET /sessions/{id}/stream`), which the front
  process does not serve. An allowlist keyed on event names drops every frame
  and hands the customer an empty turn. Key the projection on frame shape, and
  keep it fail-closed so a kind added later is dropped rather than relayed.
- Auth: send `X-Internal-Secret`. Both `/v1/chat/completions` and `/api/chat`
  are in `_MIXED_INTERNAL_API_PATHS` (`server.py:698`, `:795`), so the grant
  reaches the handler directly (`token_auth.py:2342`).
- **Do not forward the client's `Origin` or any `X-Forwarded-*` header.**
  Loopback with no Origin is trusted by the CSRF check; a forwarded foreign
  Origin trips it (`origin.py`). Confirmed live: a foreign Origin with a valid
  secret is refused, so the strip is load-bearing rather than precautionary.
- **A busy slot returns 409.** Confirmed live. Requests for one slot id must be
  serialized in the front process rather than surfacing 409 to the caller.
- `/v1` has no per-client API key concept and its `usage` block is hardcoded to
  zero. Per-caller identity and accounting belong to the front process.
- The backend runs in dashboard mode. There is no `--no-dashboard` flag;
  dashboard-off is `--slack-only` (`cli.py:539`), and it starts a different,
  much smaller server (`_init_api_server`) that has neither the chat endpoints
  nor the slot registry, and it quiets nothing.

## Launching the backend, verified names only

These were all wrong once in a way that produced no error, so use these and not
the plausible ones.

- **`KIROCREW_BIND=127.0.0.1`.** The published image sets `0.0.0.0`. A
  deployment that does not override it puts the backend on the network and
  removes the only trust boundary this design has, while every local test still
  passes. `KIROCREW_HOST` is read by nothing. Verify the resulting listener
  rather than assuming it.
- **`KIROCREW_TELEMETRY_DISABLED=1`** to silence the beacon (`sections.py:3248`),
  or the config key `telemetry.beacon_enabled=false` (`sections.py:3232`). Either
  gate works. An environment variable named after the config key is read by
  nothing, which is the trap: it looks like it worked.
- **`KIROCREW_HOME` must equal `SMC_DATA_HOME`.** The boot secret is written to
  `config_dir()/run/`, and `config_dir()` IS `KIROCREW_HOME`, so this is what
  makes `common.secret_path()` resolve.
- `--no-crons`: arming the scheduler fires any overdue job immediately
  (`gateway.py:~10262`).
- Supply no channel credentials. Every transport is gated on credentials as well
  as its flag (`gateway.py:1523-1600`), so supplying none keeps them all off
  without depending on getting a flag right.
- The boot update check cannot be disabled by config. Leave it and record it as
  an outbound request the deployment makes.
- **`--approval yolo`**, and the credential. Two things the deployment cannot run
  without:
  - Approval mode is `yolo` (`cli.py:1256`, choices reads|yolo|interactive), and
    it is **refused unless `KIROCREW_HOME` is explicitly non-default**. Nothing
    in the container is there to click Approve, so an interactive prompt is an
    indefinite stall rather than a question. The cost is stated plainly: every
    tool the crew carries is auto-approved for whatever a customer's message
    causes it to do. The narrowing path is `reads`, which auto-approves only
    read-only tools, once a real turn is working.
  - `KIRO_API_KEY` comes from AWS Secrets Manager through the task definition.
    The container never signs in. The environment is inherited wholesale by the
    backend, so the key reaches the kiro-cli child (`loader.py:1167`,
    `runtime.py:1234`) and the sandbox env filter deliberately does not deny it
    (`runtime.py:1251`). Refuse to start when it is absent. **Presence is not
    validity:** an invalid key yields a container that answers its health check
    while every turn fails, so only a real turn establishes the credential works.

**Readiness proves less than it looks like.** Port answering plus the secret file
existing does not mean a turn will succeed: a backend with no model credential
answers and then returns `503 kiro_prerequisite_required` on every turn. Say in
code what the check proves and what it does not.

**The sandbox: Fargate forces a decision, so the deployment states it.**
`agent.sandbox_allow_unsandboxed_exec` (`sections.py:636`) is a **config key with no
env var equivalent**. kiro-cli spawns the model subprocess inside an unprivileged
user namespace, and Fargate does not permit one: the supervisor probes it by forking
and calling `os.unshare(CLONE_NEWUSER)`, and the kernel refuses. On Fargate the
choice is unsandboxed execution or no service.

Found by deploying. The guard READ that key, nothing WROTE it, and the container
refused to boot naming a `config.json` no code here created. Each side had correctly
left the security decision to the other, so neither made it.

The deployment states it now: crew parameter `AllowUnsandboxedExec` to task env
`SMC_ALLOW_UNSANDBOXED_EXEC`, and `apply_sandbox_posture()` merges the key into
`<home>/config.json` before the guard reads it. The container still never assumes it:
the setting defaults to the SAFE value in both the dataclass and the env parser, an
existing config is merged rather than replaced, an unparseable one is refused rather
than overwritten, and an unrecognised value is refused rather than read as "no" (an
unresolved `${AllowUnsandboxedExec}` would otherwise be indistinguishable from a
deliberate choice). `--require-sandbox` demands the sandbox, which on Fargate means
refusing to start, correctly and loudly.

**The cost, recorded rather than buried:** it compounds with `--approval yolo`.
Auto-approved tools running unsandboxed mean a prompt injection carried in a customer
message can execute code with the container's own privileges, and the container holds
`KIRO_API_KEY` and `SMC_CONTROL_SECRET` in its environment. The ECS task is the
boundary that remains: single tenant, one data home, private subnet, no inbound path
except the load balancer. Narrowing approval to `reads` is the mitigation that does
not require leaving Fargate.

**Shutdown: a worker is not in the backend's process group.** Workers spawn with
`start_new_session` (`runtime.py:1321`), so each `setsid`s into its own group and
**escapes a `killpg` on the backend**. They are reaped by the backend's own
SIGTERM handling, which makes the drain window load-bearing rather than a
courtesy. Killing the group and skipping the drain leaves a worker that goes on
to finish its turn.

## Backup facts the sidecar must respect

Three properties of the on-disk format make the obvious implementation silently
wrong. Each needs a test that fails against the naive version.

1. **The transcript is not append-only.** Per-turn writes append
   (`history.py:1859-1861`), but the whole file is atomically replaced,
   sometimes with a shorter one, on rotation, compaction, any metadata-line
   update, and the dashboard slot save (`history_projection.py:812-857`,
   `chat_persistence.py:3177`). An offset-based incremental upload is
   incorrect, not merely inefficient. Upload whole objects.
2. **mtime does not change on rewrite.** `_restore_mtime` restores the original
   mtime after every rewrite (`history_projection.py:855`). A sidecar polling
   mtime sees nothing. Detect change by size plus content hash.
3. **Copying without the lock races a rewrite.** Writes hold a cross-process
   advisory `flock` on a per-session lock file (`history.py:1410-1455`). A copy
   that skips it can capture a half-replaced file.

`artifacts/` is write-once and heavy: upload each object once, do not re-hash
the directory every cycle.

Backup lag is accepted by the design but must be visible. Surface it.

## What every track owes

Unit tests that run with no AWS and no real backend: fake the backend's HTTP
surface, fake S3, use real temporary files for anything touching the filesystem.
Run only your own test files.

State plainly what you could not verify. None of this can be proven from a
laptop, and a claim that outruns the evidence is worse than a gap that is named.
