# Dev Containers Module

## Overview

`devcontainer.py` runs a session's `kiro-cli` inside the project's Dev Container,
built by the reference `@devcontainers/cli`. This is VS Code parity: the repo's
`devcontainer.json` is honored in full (image/build, features, lifecycle hooks,
mounts, `runArgs`) after a per-config human trust grant. The gateway does **not**
strip or override security-posture properties — parity, not a sandbox.

The user guide is [docs/devcontainers.md](../../devcontainers.md).

## Architecture

```
gateway (host)                          container (project toolchain)
  AcpRuntime.spawn / AcpClient._spawn
    resolve_for_work_dir() ─────────┐
      config off?           → None  │  trust-gated
      non-Linux?            → None  │  devcontainer up
      no config in workdir? → None  ├──────────────────────►  kiro-cli acp
      docker missing?       → None  │   docker exec -i        (shell + file
      untrusted?            → None  │   -u remoteUser          tools run here)
      up() failed?          → None  │   -w remoteWorkspace
                                    │   -e KIROCREW_*
    containerize_spawn(...) ────────┘
    session/new cwd = info.remote_workspace_folder
```

**Two live spawn paths, one implementation.** `AcpRuntime.spawn()` carries every
chat and subagent session (`AcpProvider.start()` routes all non-claude sessions
through `_start_kiro_runtime()`). `AcpClient._spawn()` is the second live path:
the Knowledge Library worker pool (`knowledge/llm_pool.py`'s `AcpWorker`)
constructs an `AcpClient` on the default kiro backend and calls `ensure_ready()`,
and the dormant claude seam reuses the same client. Because both are reachable,
the security-sensitive parts — the eligibility/trust gate
(`resolve_for_work_dir`), the exec-id mint plus argv construction
(`containerize_spawn`), and the in-container kill (`kill_containerized_tree`) —
live in `devcontainer.py` with both paths as thin callers. Only the inner argv
differs (the runtime pins `--model`; the client forwards session/channel env).
A trust or kill fix applied per-path would otherwise land on one and silently
miss the other.

The agent process must move, not just its tool calls: `kiro-cli` 2.14 executes
shell and file tools in-process and ignores the ACP client's `fs`/`terminal`
capabilities, so there is no interception seam. Verified by spike, not assumed.

## Key Invariants

1. **Fail to host, never fail the spawn.** `resolve_for_work_dir()` returns
   `None` for every negative case (feature off, non-Linux, no config, docker
   missing, untrusted, `up()` raised). Spawn must not block on a human decision;
   the trust prompt is surfaced out of band. Untrusted and failed cases log
   loudly.
2. **No trust, no build.** `DevcontainerManager.up()` raises
   `DevcontainerNotTrusted` before executing anything when the current config
   bytes have no grant. `is_trusted()` is also checked in `resolve_for_work_dir`
   before `up()`; `up()` re-checks to close the edit race between the two.
3. **Trust binds to content, not path.** A grant is valid only while the config's
   SHA-256 matches the recorded digest, so any edit forces a fresh decision.
4. **Container-side cwd over ACP.** The session cwd is
   `info.remote_workspace_folder` for a containerized spawn and the host work dir
   otherwise. The gateway keeps host paths; the agent gets container paths; the
   bind mount makes them the same bytes. `AcpRuntime._session_cwd()` additionally
   REFUSES a session whose cwd is not the runtime's work dir, because one runtime
   multiplexes many sessions but has exactly one container; `AcpClient._acp_cwd()`
   needs no such check (one client, one session).
5. **Mutually exclusive with host isolation wrappers.** The devcontainer branch
   sets `_sandbox_cleanup = None` and skips both `wrap_argv` and
   `cgroup_scope_argv`. Host mechanisms cannot cross the container boundary.
6. **All state is derivable.** The container is re-found after a gateway restart
   by its id-label, so nothing in this module needs persistence except the trust
   store.
7. **In-container kill is explicit.** Killing the host-side `docker exec` client
   only detaches. `kill_exec()` signals the recorded pid through a pidfile.
8. **The exec id is minted in one place.** `containerize_spawn()` generates it
   from `uuid4` and no caller may supply one: `kill_exec()` interpolates it
   unquoted into a shell script, so its injection safety rests entirely on the
   value being gateway-generated hex.
9. **`status()` never requires docker.** Every container probe it makes shells
   out to the `docker` binary, so the whole lookup sits behind
   `docker_available()`. A host with a devcontainer config but no docker reports
   the container as absent; without the guard, `_find_by_label` raises
   `FileNotFoundError` out of a dashboard-polled endpoint as an HTTP 500.

## Trust store

`config_dir()/devcontainers/trust.json`, written atomically via a `.tmp` file plus
`os.replace`. Shape:

```json
{
  "/realpath/of/project": {
    "digest": "<sha256 of the .devcontainer tree>",
    "config_path": "/realpath/of/project/.devcontainer/devcontainer.json",
    "granted_at": 1780000000.0
  }
}
```

Config lookup order is `.devcontainer/devcontainer.json`, then
`.devcontainer.json`.

`_read_config_tree()` reads the entire input set ONCE into an ordered
`[(relpath, bytes)]` list, and both `config_digest()` and `config_preview()`
derive from that single read. This is a correctness requirement, not an
optimization: computing the displayed text and the digest in two separate walks
left a window in which the tree could be swapped between them, binding benign
previewed text to a different tree's digest. Every member is opened through
`_read_config_bytes()` (O_NOFOLLOW, `S_ISREG` fstat, realpath containment,
`is_sensitive_path`), with the project root passed explicitly — inferring it from
a nested path yields that file's own parent and makes containment a tautology.
A bare `read_bytes()` here would be an arbitrary-file read, since the preview
returns these bytes verbatim to the dashboard caller. `rglob` never yields the
parent, so a symlinked `.devcontainer` is refused up front.

`_parse_jsonc()` fails closed: an unparseable config raises rather than
previewing with `name`/`image` as `None`, because the containment check below
cannot enumerate build inputs it cannot read.

`assert_build_inputs_contained()` refuses `build.dockerfile`, `build.context`,
top-level `dockerfile`, and `dockerComposeFile` (string or list) resolving
outside `.devcontainer/` — such an input is never hashed, so a later edit would
execute under a still-valid grant. A root-layout `.devcontainer.json` declaring
any build input is refused, since it hashes one file only.

`write_build_config()` re-verifies the digest (raising `DevcontainerConfigChanged`
on a post-trust swap), strips `initializeCommand` — the only spec hook that runs
on the HOST — and writes a sanitized copy under
`config_dir()/devcontainers/build/<project token>/<digest[:24]>/`, which `up()`
passes as `--override-config`. Experimentally confirmed: `--override-config`
relocates only `devcontainer.json`, and a referenced `build.dockerfile` still
resolves against the workspace. That is why containment is enforced separately
rather than by snapshotting the tree. Residual execution risk is in-container
only, where the agent already has full control.

### Build-artifact reaping

The `<project token>` path component is load-bearing rather than cosmetic. It is
the same `sha256(realpath)[:24]` used for the container's id-label (both derive
from `_project_token()`), and it is what makes a build directory attributable to
a project: with a digest-only path, "this project's superseded configs" is not an
enumerable set, so stale directories could only be reaped by guessing at
unrelated ones. Every trusted config edit produces a new digest, so without
reaping they accumulate for the life of the install.

| When | What is removed |
|---|---|
| `write_build_config()`, after the new copy is durably in place | every *other* digest-named dir under that one project's root |
| `down()` | that one project's whole build root (off-loop; nothing reads the config after teardown, so it is reaped even when no container was found) |

Containment rules, all enforced in `_remove_build_entry` /
`_prune_superseded_build_configs`:

* only ONE project's root is ever iterated, so a whole-tree wipe is not
  expressible and another project's artifacts are unreachable;
* only names matching `^[0-9a-f]{24}$` are candidates — anything else under the
  root was not written by this module and is preserved, not guessed at;
* the digest currently in use is always kept;
* `is_symlink()` is tested **before** `is_dir()` (which follows links), and a
  link is unlinked as a link, so a planted symlink cannot redirect the delete
  outside the tree;
* best-effort throughout: a build never fails because its cleanup could not.

## Container identity and lifecycle

| Concern | Mechanism |
|---|---|
| Identity | `--id-label kirocrew.devcontainer=<_project_token(realpath)>` (`sha256[:24]`). Path-charset-safe and short; the same token names the project's build-artifact root. |
| Reuse | One container per project realpath, shared by all sessions on that directory and across gateway restarts (`devcontainer up` is idempotent for an unchanged config). |
| Serialization | Per-project `asyncio.Lock` around `up()`. Image builds are not concurrent-safe on one config. |
| Cache validation | An in-memory `DevcontainerInfo` is reused only when its digest matches and `docker inspect .State.Running` is `true`. |
| Rebuild | `rebuild=True`, or a digest change against a cached info, appends `--remove-existing-container`. |
| Timeout | `_UP_TIMEOUT_SECS` = 900. `_EXEC_PROBE_TIMEOUT_SECS` = 20 for inspect/kill probes. |
| Teardown | `down()` does `docker rm -f`, drops the cache entry, and reaps the project's sanitized build configs. |

`up()` runs the CLI with `--log-format json`, which interleaves log records with
the result on stdout. `_parse_up_output()` therefore scans **from the end** for
the last JSON object carrying `outcome`; it does not assume the last line. A
non-zero exit or an `outcome` other than `success` raises `DevcontainerError`
carrying the CLI message or the stderr tail.

## Exec plumbing

`exec_argv()` builds `docker exec -i [-u remoteUser] -w <workdir> -e K=V ... <cid>
sh -c <preamble> sh <inner argv>`. The preamble records `$$` to
`/tmp/kirocrew-exec/<exec_id>.pid`, then `exec setsid "$@"` when `setsid` exists
so the whole in-container tree is one process group, falling back to `exec "$@"`.
`exec` matters: the recorded pid IS the target, with no wrapper shell left behind.

`docker exec` does not inherit the parent environment, so the client forwards
`KIROCREW_SESSION_KEY`, `KIROCREW_CHANNEL_ID`, and the spawned-process marker
explicitly. `DEVCONTAINER_EXEC_ENV` (`KIROCREW_DEVCONTAINER_EXEC`) carries the
exec id inward for kill-file naming and diagnostics. The inner argv is
`kiro-cli acp --agent <name>` unqualified: the host-resolved binary path is
meaningless inside the image.

`kill_exec()` reads the pidfile and issues `kill -TERM -$P` (group) with a
single-pid fallback, sleeps 2s, escalates to `KILL`, and removes the pidfile. It
runs before the normal host-side teardown, which still reaps the `docker exec`
client itself.

## Config

| Key | Values | Default |
|---|---|---|
| `agent.devcontainer` | `auto`, `off` | `off` |

Read per spawn via `KiroCrewConfig.load()`, so the live-reload fingerprint cache
applies and no restart is needed.

## Dashboard API

Registered in `dashboard/server.py`. `project` is accepted only when its realpath
matches an existing chat slot's project directory (the same barrier idea as
`worktree.py`'s `_allowed_repo_roots`), so a caller cannot probe or trust paths no
session is scoped to. Unknown project ⇒ 400.

| Route | Guard | Notes |
|---|---|---|
| `GET /api/devcontainer/status` | owner-only + project barrier | Config presence, trust, container id, running, remote workspace folder. |
| `GET /api/devcontainer/config` | owner-only + project barrier | Raw text capped at 64 KiB, digest, `name`, `image`, `other_inputs` (the other hashed tree files, capped at 64), `trusted`. 404 when no config; refuses an escaping build input, a symlink, or an unparseable config. |
| `POST /api/devcontainer/trust` | `_deny_non_owner` + SEL | Grants for current bytes. |
| `DELETE /api/devcontainer/trust` | `_deny_non_owner` + SEL | Returns `removed`. |
| `POST /api/devcontainer/rebuild` | `_deny_non_owner` + SEL | 409 on any `DevcontainerError`, including `DevcontainerNotTrusted` — a rebuild must not silently re-grant. |

All five routes go through `_deny_non_owner`, which is `deny_non_dashboard_caller`
plus a refusal of `internal_auth`. That claim is granted to any request presenting
a valid `X-Internal-Secret` from loopback -- the path every MCP call arrives on --
and the shared helper permits it because it also guards `suggest_followup`, where
the agent legitimately raises a card. Honoring it here would let the agent read
the digest and grant trust to a configuration it wrote, self-approving the human
decision the trust model exists to require. Nothing inside the gateway needs these
routes: the session and runtime paths call `kiro_crew.devcontainer` directly, and
the only HTTP client is the dashboard's own trust card. Refusals are audited as
SEL `denied` with code `internal_caller_denied`.

`status` is owner-only for the same reason even though it only reads: it reports
whether a project is trusted and which container backs it, so leaving it open
would expose the outcome of the decision to a caller refused everywhere else.

Trust mutations are dashboard-caller-only because a grant authorizes arbitrary
image pulls and lifecycle-hook execution for that project. That is precisely the
decision VS Code gates behind Workspace Trust, so it may not be made by an agent,
a subagent, or an app.

## Frontend surface

`DevcontainerTrustCard` renders above the composer in `FollowUpCard`'s slot and
styling, because it gates the same thing the composer starts: nothing is built or
run until the user answers. It shows the config path and the first 12 digest
characters, and the raw config is rendered as **text children only** (never
`dangerouslySetInnerHTML`) and collapsed by default — untrusted file content that
the user can read, not must scroll past.

`ChatPage` polls `GET /api/devcontainer/status` for the active slot's project and
shows the card while `has_config && !trusted && !dismissed`. Dismissal is keyed
on `project_dir \0 config_path`, so it does not carry across projects, and it does
not persist trust. `api.devcontainerStatus?.()` and `api.devcontainerTrust?.()`
are called optionally because many test suites mock `../api/client` partially.
`ChatInput` renders a static Dev Container chip (a `<span>`, deliberately outside
the shelf's tab order) while a container is running.

## Known v1 limitations

- Kiro Crew's managed MCP servers (`mcp-core`, `mcp-cron`, `mcp-computer`) are not
  reachable from inside the container: their REST callbacks target the gateway's
  host loopback. `kiro-cli` reports `mcp_server_init_failure` and the session
  continues with the project toolchain fully functional.
- `/proc`-based liveness observes the host-side `docker exec` client proxy. Death
  detection works (pipe close); wedge heuristics degrade.
- Linux hosts only. On macOS, Docker Desktop is a VM; the existing Seatbelt
  sandbox path is unchanged.
- **One runtime, one container.** `AcpRuntime` multiplexes many ACP sessions over
  one kiro-cli process, so it resolves its container from its own `work_dir`.
  `_session_cwd()` maps a session's cwd to `remoteWorkspaceFolder` when it
  realpath-equals that `work_dir` and raises `AcpRuntimeError` otherwise —
  mapping a foreign cwd would hand the agent a path that either does not exist in
  the image or belongs to another project. `cwd_blocks_pool` (session.py) already
  keeps project-scoped sessions off shared runtimes, so the guard enforces that
  invariant rather than assuming it.
- Warm-pool runtimes are spawned with `default_project_dir()` as `work_dir`
  before any project is known, so they containerize only if that directory has a
  trusted config; a session for another project cannot claim one.

## Source Files

| File | Purpose |
|---|---|
| `devcontainer.py` | Trust store, `DevcontainerManager` (up/down/status/alive), `exec_argv`, `kill_exec`, module singleton. |
| `acp/runtime.py` | **The active path.** `_maybe_devcontainer_info()` (eligibility), the spawn branch that replaces argv with `docker exec`, `_session_cwd()` (container path + one-container invariant), in-container kill on teardown. Every non-claude session reaches here via `AcpProvider.start()` -> `_start_kiro_runtime()`. |
| `acp/client.py` | The same branch on the legacy client path, which serves the dormant claude backend (`AcpProvider.__init__`'s client is "never spawned — just used for config storage"). Kept so both spawn paths behave alike. |
| `dashboard/handlers/devcontainer.py` | The five endpoints, the slot-project barrier, SEL audit. |
| `dashboard/server.py` | Route registration. |
| `config/loader.py` | The `agent.devcontainer` field. |
| `website/src/components/DevcontainerTrustCard.tsx` | The Workspace Trust prompt. |
| `website/src/pages/ChatPage.tsx` | Status query, card gating, dismissal keying. |
| `website/src/components/ChatInput.tsx` | The Dev Container status chip. |
| `test/test_devcontainer.py` | Config lookup order, digest binding and invalidation, trust-store atomicity, preview capping, `exec_argv` shape, `up` output parsing, the `up` trust gate, and the project-resolution barrier. |
