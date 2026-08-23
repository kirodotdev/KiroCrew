# Dev Containers

> **Developer preview — off by default and not reachable by config alone.**
> Two locks must both be open: the gateway must run with
> `KIROCREW_DEVCONTAINERS=1` in its environment **and** `agent.devcontainer`
> must be `auto`. With the environment variable unset the feature is completely
> inert: no container is built, and the dashboard shows no trust prompt even for
> a project that ships a `devcontainer.json`.
>
> The second lock is deliberate rather than belt-and-braces. A config key is
> reachable by anyone following these docs, and the preview still has sharp
> edges (`python3` required in the image, host isolation wrappers skipped).
> Enabling it takes a deliberate act by someone who accepts that.
> It also keeps CI, which carries no such variable, on the host path.

Run a session's agent inside the project's own Dev Container, so it builds and
tests against the project's toolchain instead of whatever the gateway host
happens to have installed.

This is **VS Code parity**, not a sandbox. A trust grant is the ceiling for the
local tree you hashed: image or build, in-container lifecycle hooks, mounts, and
`runArgs`. A host mount or run argument in that tree is honored after you
approve it. The trust card says so. The gateway does not rewrite the config to
make it safer.

The gateway refuses only what would make that grant uninformative, plus a named
floor that is not a growing list of docker aliases. `features` are refused
because the CLI fetches their metadata from a registry and merges it at build
time, so the approved text is not what runs. `privileged` and the
capability/namespace grants equivalent to it are refused because they let the
container reach host storage the approved config never named. Runtime sockets
and a short list of kernel control trees are refused as escapes. Sensitive host
paths use the same named predicate as the rest of Kiro Crew. See
[What the grant covers, and what is refused](#what-the-grant-covers-and-what-is-refused).

## What it does

When the feature is on and a session's project directory carries a Dev Container
config that has been trusted, the ACP spawn path replaces the host `kiro-cli`
argv with a `docker exec` into a container built by the reference
[`@devcontainers/cli`](https://github.com/devcontainers/cli) — the same engine
VS Code uses.

The split mirrors VS Code's client/server model:

| Plane | Where it runs |
|---|---|
| Gateway, dashboard, memory, sessions, cron | Host |
| `kiro-cli` and every tool it executes (shell, file edits, builds, tests) | Inside the container |

The agent process itself has to move: `kiro-cli` executes shell and file tools
in-process and ignores the ACP client's `fs`/`terminal` capabilities, so there is
no way to keep the process on the host and route only its tool calls inward.

The workspace is bind-mounted by the devcontainer CLI. The gateway keeps using
the host path, while the `session/new` cwd sent over ACP is the **container-side**
workspace folder (usually `/workspaces/<name>`) so the agent's file tools resolve
against the same bytes through the bind mount.

## Requirements

| Requirement | Detail |
|---|---|
| Linux, or Docker Desktop on macOS / Windows | Native Linux talks to managed MCP over a bind-mounted unix socket. Docker Desktop is a VM, so that path uses TCP to `host.docker.internal` instead. Without a usable Docker the session runs on the host (`docker_unavailable`). The host Seatbelt / Job-object path is skipped on a containerized spawn. |
| Docker | `docker` must be on the gateway's `PATH` from a trusted location. A root-owned system binary is accepted as-is. Docker Desktop on macOS is user-owned and is accepted only when Developer ID Docker Inc still verifies. If Docker is missing or refused, the session runs on the host and a warning is logged. |
| devcontainer CLI | A real `devcontainer` binary is preferred: `npm i -g @devcontainers/cli`. Without one, `npx --yes @devcontainers/cli` is used, which downloads on first use — install it globally for deterministic session startup. |
| `kiro-cli` inside the container | The inner command is resolved against the **container's** `PATH`, not the host's. `kiro-cli` must be in the image or installed by a lifecycle hook such as `postCreateCommand`. A `features` block is refused in this preview, so it cannot be the install path. |
| glibc >= 2.34 in the image | `kiro-cli` is dynamically linked against glibc 2.34 or newer. Debian bookworm (2.36) and Ubuntu 22.04 (2.35) satisfy this; Debian bullseye (2.31) and Alpine (musl) do not. |
| A signed-in `kiro-cli` inside the container | `KIRO_API_KEY` is forwarded on `docker exec`. The host kiro-cli login store (`data.sqlite3`) is **copied** into a gateway-owned bind (not a live mount of Application Support) and exposed as `XDG_DATA_HOME=/tmp/kirocrew-auth`. A login inside the container does not write back to the host. `~/.aws`, `~/.ssh`, `KIROCREW_HOME`, and the rest of `~/.kiro` are never mounted. |
| `python3` in the image | Managed MCP (scheduled jobs, subagents, saved lessons) stays on the host. kiro-cli inside the container talks to a small stdio client that needs `python3` on the image `PATH`. Without it those tools fail to start and the session continues with the project toolchain only. |
| A container-visible agent definition | `kiro-cli` resolves `--agent <name>` by reading a **file**, checking `$PWD/.kiro/agents/` before `~/.kiro/agents/`. The project is bind-mounted, so a **project-scoped** `.kiro/agents/<name>.json` is visible inside the container and works unchanged. If that file is missing, the **selected** host `~/.kiro/agents/<name>.json` is copied into a bound `~/.kiro/agents` — never the whole agents directory, because those definitions can carry MCP credentials in `env`. A session refuses to start only when the host also has no file, rather than falling back to the host spawn. |

## Enabling it

Off by default, behind two locks. Both are required; either one alone leaves the
feature inert.

**1. Developer opt-in** — set in the environment the *gateway* runs in, not in
config, so it is an explicit act rather than a setting someone can stumble into:

```bash
KIROCREW_DEVCONTAINERS=1
```

Anything outside `1` / `true` / `yes` / `on` reads as off, so a stray
`KIROCREW_DEVCONTAINERS=0` means disabled rather than "the name is present,
therefore on".

**2. Config mode** — `agent.devcontainer`:

```bash
kirocrew config set agent.devcontainer auto
```

| Value | Behavior |
|---|---|
| `off` (default) | The agent always runs on the host, as before. |
| `auto` | Per session: containerize when the project qualifies, otherwise fall back to the host. |

Config is read live, so no gateway restart is needed for the mode. The
environment variable is read from the gateway's own process, so changing it does
require restarting the gateway.

Under `auto` **with the opt-in set**, a session containerizes only when **all** of these hold. Any miss
means the session runs on the host instead of failing:

1. The host is Linux, or Docker Desktop is running on macOS or Windows.
2. The session's work directory contains `.devcontainer/devcontainer.json`, or
   `.devcontainer.json` as a fallback. The first wins when both exist.
3. `docker` is on `PATH` from a trusted location (root-owned, or Docker Desktop's
   Developer ID-verified CLI on macOS).
4. The current config bytes carry a valid trust grant.
5. `devcontainer up` succeeds.

Cases 3, 4, and 5 log loudly. Falling back on an untrusted config is also what
VS Code does: no trust, no container.

## Trust

A trust grant binds to the **SHA-256 of the whole `.devcontainer/` tree**, not
to the path and not to `devcontainer.json` alone. A referenced `Dockerfile`,
compose file, or lifecycle script can change what a build executes while the
json stays byte-identical, so every file in the directory is hashed. Any edit —
by you, by a `git pull`, or by an agent — changes the digest, invalidates the
grant, and forces a fresh human decision before the next build or exec.
Granting trust authorizes arbitrary image pulls and lifecycle-hook execution for
that project, which is exactly the decision VS Code gates behind Workspace
Trust.

Grants are stored in `~/.kiro/crew/devcontainers/trust.json`, keyed by the
project directory's realpath, recording the digest, the config path, and the
grant time.

### What is refused outright

Several shapes cannot be made safe under a content-bound grant, so they are refused
with an explanatory error rather than trusted:

| Refused | Why |
|---|---|
| A `features` block | The CLI resolves each Feature from a registry at build time and merges its metadata into the effective config, and that metadata can declare `privileged`, `capAdd`, `mounts` or `containerEnv`. So the approved text passes every screen while the container that gets built carries what those screens exist to refuse, and the digest cannot bind it — a Feature's contents are whatever the registry serves later, not what the human read. This is a real reduction in what the preview accepts, since Features are the usual way to install a toolchain; put it in the image or a lifecycle hook instead. |
| A `.devcontainer` tree with more than 4096 entries | The per-file and total-byte ceilings cannot bound the entry count, because directories are skipped before any byte is accounted — a tree of empty directories weighs nothing they measure while still costing memory per entry to enumerate. Refused rather than truncated: hashing only part of the tree would leave the rest unscreened behind a digest that looks complete. |
| A build input resolving outside `.devcontainer/` — `build.dockerfile`, `build.context`, top-level `dockerfile`, or `dockerComposeFile` pointing at e.g. `../Dockerfile` | The digest covers the `.devcontainer/` tree. An input outside it is never hashed, so editing that file later changes what the build executes under a still-valid grant. Chasing referenced paths recursively does not close this — they can reference further paths in turn — so the containment requirement is the fix. Move the file inside `.devcontainer/`. |
| A symlink anywhere in the tree, including `.devcontainer` itself | A symlink's target can be retargeted, or its content swapped, after the grant without changing the hash. Skipping it would leave it outside the digest while a hook like `bash setup.sh` still ran it. |
| A file with more than one hard link | A hard link is invisible to every path-based check: it is an ordinary regular file with a benign name inside `.devcontainer/`, while the inode is whatever it was linked to. Both the symlink refusal and the sensitive-path screen see only names, so a link to `~/.aws/credentials` passes both and a Dockerfile `COPY` bakes it into an agent-readable image. The link count is the only local signal. |
| A `devcontainer.json` that cannot be parsed (block comments, trailing commas, invalid UTF-8) | The containment check above is only sound if the build inputs can be enumerated, so an unparseable config fails closed instead of skipping the check. |

### `initializeCommand` is never honored

`initializeCommand` is the one lifecycle hook the
[spec](https://containers.dev/implementors/json_reference/) runs on the **host**
rather than in the container. Honoring it would let a project's config execute
outside the container boundary this feature exists to provide, so it is stripped
from the config the build consumes: the sanitized copy is written under
`~/.kiro/crew/devcontainers/build/` and passed to `devcontainer up` via
`--override-config`, so the CLI never sees the hook. A warning is logged when
one is dropped.

Every other hook (`onCreateCommand`, `updateContentCommand`,
`postCreateCommand`, `postStartCommand`, `postAttachCommand`) runs **inside** the
container, where the agent already has full control by design. That is the
residual risk and it is deliberately in-container only: a swapped Dockerfile
executing there is not a privilege escalation, because the agent can already run
commands in that container.

Note that `--override-config` relocates only `devcontainer.json` — a referenced
`build.dockerfile` still resolves against the workspace, verified by experiment.
That is why build-input containment is enforced separately rather than by
snapshotting the tree.

### Granting it in the dashboard

When the active chat slot's project carries a Dev Container config that is not
yet trusted, a **Workspace Trust card** appears above the composer. It names the
config file, shows the first 12 characters of its digest, and says that this is
not a sandbox: trusting runs the setup commands and honors the host mounts and
run arguments the file declares. It can expand to show the raw config text so
you can read what you are about to authorize. Trust it, and the next session
spawn for that project builds and uses the container. Dismiss it, and nothing
is granted — the card returns next session.

Because the grant is bound to the digest, an edit to `devcontainer.json` brings
the card back with a new digest rather than inheriting the earlier decision.

While a container is up for the active project, a **Dev Container** chip appears
in the composer shelf; its tooltip carries the short container id. The chip is a
status readout, not a control.

### Endpoints

Three properties keep an agent from trusting its own config:

- Trust mutations are **dashboard-caller-only**. A session, a subagent, or an
  app calling the endpoint is denied.
- The `project` path is accepted only when it realpath-matches an existing chat
  slot's project directory, so an arbitrary caller cannot probe or trust paths
  no session is scoped to.
- Grant and revoke are recorded in the security event log.

| Endpoint | Purpose |
|---|---|
| `GET /api/devcontainer/status?project=<path>` | Config presence, trust state, container id, running state, container workspace folder. |
| `GET /api/devcontainer/config?project=<path>` | Raw config text (capped at 64 KiB), its digest, and the parsed `name`/`image`, for review before granting. |
| `POST /api/devcontainer/trust` | Body `{"project": "<path>"}`. Grants trust for the config's **current** bytes. |
| `DELETE /api/devcontainer/trust` | Body `{"project": "<path>"}`. Revokes. |
| `POST /api/devcontainer/rebuild` | Body `{"project": "<path>"}`. Trust-gated rebuild; a rebuild of an untrusted config fails rather than silently re-granting. |

`devcontainer.json` may contain `//` comments. The preview strips them only to
extract `name` and `image`; the devcontainer CLI does the real jsonc parse, and
the digest always covers the raw bytes.

## Container lifecycle

One container per project directory, reused by every session scoped to that
directory and across gateway restarts. Identity is an id-label derived from the
project realpath, so `devcontainer up` finds the existing container again instead
of building a second one; nothing about the container needs to be persisted by
the gateway.

- `up` calls for the same project are serialized. Two sessions starting at once
  on one config do not race the image build.
- A cached container is reused only while its recorded config digest still
  matches and the container is actually running. A stale entry is dropped and
  rebuilt.
- A digest change, or an explicit rebuild, removes the existing container first.
- `devcontainer up` is allowed 15 minutes. Image builds and feature installs are
  slow the first time; later starts hit the cache.

Inside the container each agent is launched under `docker exec -i`, as the
config's `remoteUser` when one is set, with the container workspace folder as
cwd. The inner process is started under `setsid` when available and records its
pid to `/tmp/kirocrew-exec/<exec-id>.pid`, because killing the host-side
`docker exec` client only detaches — teardown signals the in-container process
group through that pidfile, escalating `TERM` to `KILL`.

Host-side sandbox and cgroup wrappers are **not** applied to a containerized
session, because those mechanisms cannot cross the container boundary. Namespaces
are isolation, not a substitute for the host sandbox — resource ceilings are
re-applied as container limits, and the closed screen (below) is the rest of
what this preview still refuses. Everything the hashed tree declares that is
not in that screen is the grant.

Namespaces isolate what a process can **see**; they do not cap what it can
**consume**. A fork bomb or memory balloon inside the container still lands on
the shared host kernel, so the cgroup ceilings are re-applied as container
limits, resolved from the same `resource_limits` config the host scope reads:

- image / Dockerfile configs get `--pids-limit`, `--memory` and `--memory-swap`
  in the sanitized `devcontainer.json`;
- Compose services get `pids_limit`, `mem_limit` and `memswap_limit` injected
  into the **frozen** compose copy, since Compose ignores `runArgs` and uses its
  own schema. The project's own file is never rewritten.

Swap is pinned to the memory cap in both shapes: left unset, the kernel grants
swap equal to the cap and the ceiling is effectively doubled. A limit the project
sets explicitly is honored rather than overridden, so the container matches the
config approved at the trust prompt.

## What the grant covers, and what is refused

The grant is the ceiling for what the hashed tree declares. The screen is
**closed by class**, not by docker spelling. A new alias of an already-covered
class is canonicalized into that class. A `runArgs` flag that is not in one of
those classes is yours after you trust the file.

| Class | Why |
|---|---|
| Digest integrity | The effect is not in the hashed local tree (`features`, host-side `initializeCommand`, build inputs / `extends.file` / compose `include` outside `.devcontainer/`, symlinks, extra hard links). |
| Isolation voided | The container can reach host storage the approved config never named (`privileged`, `SYS_ADMIN` / `ALL`, host namespaces including `--net=host`). |
| Named host-control escapes | Runtime sockets and `/proc`, `/sys`, `/dev`, `/run/user`. |
| Sensitive-path floor | The same `is_sensitive_path` predicate the rest of Kiro Crew uses — a named list, not a new class per flag. |
| Gateway hygiene | `PATH` and live credential-env values the CLI inherits from the gateway, which are not in the file. |

The rest of this section is the named members of those classes, not an invitation
to grow a sixth.

A `mounts` entry for `~/.aws` would hand the agent credentials `wrap_argv` would
have denied on the host path. That is the sensitive-path floor, screened with
the same `is_sensitive_path` predicate that gates config reads, across every
shape that can express a host bind:

| Directive | Forms screened |
|---|---|
| `mounts` | `source=…,target=…,type=bind` in any field order, and the object form |
| `workspaceMount` | same string form |
| `runArgs` | `-v`, `--volume=`, `--mount`, `--mount=` — these reach docker directly |
| `runArgs` | `--env-file`, `--label-file`, `--cidfile` (and their `=` spellings) — the daemon reads these host files without any bind appearing in the config |
| compose `volumes` | short and long form, on every service |
| compose `env_file` | string, list and `path:` long form — injects a host file as the service environment, with no bind anywhere |
| compose `build` | `context`, `dockerfile`, and the string shorthand — the daemon reads the context and every `COPY` can reach it, so a context of `$HOME` puts credentials in the image |
| compose top-level `volumes` | `driver_opts.device` — a **named** volume that is really a bind. The service side reads `creds:/root/.aws`, which is correctly treated as a bare name with no host side; the host path exists only in this definition |
| compose top-level `secrets` / `configs` | `file:` — host content the runtime injects |

Two further classes are refused because a path check alone would not see them:

- **Host control interfaces.** `/var/run/docker.sock` (and the podman, containerd
  and cri-o sockets), plus `/proc`, `/sys`, `/dev`, and `/run/user` (the
  rootless-runtime directory). A file whose last component is `docker.sock`,
  `podman.sock`, `containerd.sock` or `crio.sock` is refused wherever it lives —
  including `${localEnv:XDG_RUNTIME_DIR}/docker.sock` after expansion — because
  those sockets are siblings of the rootful paths, not descendants. These are
  not credential paths, so `is_sensitive_path` does not match them — but
  handing over the container runtime lets the agent request a fresh container
  mounting anything at all, which walks around every restriction above.
- **Forwarded credential paths.** The build environment keeps `DOCKER_CONFIG`
  and `DOCKER_CERT_PATH` (and the CA-bundle names) so the daemon can
  authenticate. Those values are not in the fixed sensitive-path list, so a
  bind of `${localEnv:DOCKER_CERT_PATH}` — or of an ancestor that contains it —
  is refused by the live value. `PATH` handed to the CLI is also stripped of
  directories this process can write, so a planted `node`/`docker` shim cannot
  ride along.
- **Relative Compose bind sources.** Compose resolves them against the compose
  file's directory, so `../../../trust.json` climbs out of the project. Bare
  named volumes have no host side and are still accepted.
- **`extends.file`.** This one is refused rather than screened, because the
  problem is not the paths inside it. It pulls a service definition from *another*
  compose file that may sit outside `.devcontainer/`, so its volumes, `env_file`
  and `build` stanzas would take effect while contributing nothing to the digest.
  The grant would be bound to content that does not describe what gets built, and
  editing the extended file afterwards would not invalidate it. Inline the
  definition to use it. `extends` naming only a service in the same file is
  accepted — that file *is* in the hashed tree.

Two surfaces are screened rather than refused, since each is an ordinary host
path once it is parsed at all:

- **`build.additional_contexts`.** An extra named build context is read by the
  daemon exactly like `context` and is reachable from any `COPY --from`. Values
  naming a service, target, image or URL are not host paths and are left alone.
- **`--device` in `runArgs`.** The host side is screened like a bind source; since
  `/dev` is already a refused control tree, parsing the flag is the whole fix.

`--privileged` **is refused**, and so are the capability and namespace spellings of
the same grant (`--cap-add SYS_ADMIN`/`ALL`, `--pid=host`, `--network=host`,
`--net=host`, `--ipc=host`, `--uts=host`, `--userns=host`, `--cgroupns=host`, and
each of their Compose equivalents: `privileged: true`, `cap_add:`,
`network_mode: host`, `pid: host`, `ipc: host`, `uts: host`, `userns_mode: host`,
`cgroup: host`). `--net` is docker's alias for `--network`; both spellings are
canonicalized before the screen runs.

The earlier reasoning here was that the flag "carries no host path", so a human
approving the exact config had seen everything it could reach. That is the part
that does not hold: a privileged container does not need a host path in the config,
because it can mount a host block device from inside at runtime. Every other screen
on this page reads what the config DECLARES, and that is only informative while the
container cannot reach storage it never declared — so a config declaring no
sensitive path becomes indistinguishable from one declaring all of them, and the
trust prompt would be making a promise it cannot keep.

This is stricter than VS Code, which honors the flag. The difference is that VS
Code is not also telling the user which host paths the container can see. Grant the
specific `--device` the container needs instead.

Screening a relative Compose path is only meaningful if the build resolves it the
same way, and freezing **moves** the file: the frozen copy lives in the build
directory under Kiro Crew's own data home, so a surviving relative source would
re-anchor there — `../../../../.env` screens harmlessly against `.devcontainer`
and then lands on the gateway's own environment file. Relative host paths in the
frozen copy are therefore rewritten to absolute, resolved against the **original**
file's directory, which makes the screened path and the built path the same string
by construction rather than two resolutions that must be kept in agreement. They
are corrected rather than refused because `..:/workspace` is how a Compose
devcontainer normally mounts the project. Named volumes are left untouched.

`${localEnv:VAR}` and `${localWorkspaceFolder}` are expanded before screening,
so naming a sensitive path indirectly is not an escape. The refusal applies at
the digest, the preview, and the build, so a refused config never reaches the
trust card to be granted.

The preview still needs both locks and a content-bound grant whose prompt shows
the raw configuration. Named volumes, relative sources, and ordinary absolute
project directories are unaffected. A `runArgs` flag that is not in the table
above is the grant, not a missing screen.

## Telling the user where the session actually ran

Granting trust is not a guarantee. Missing Docker, a failed build, or a config
edited after the grant each fall back to running on the host — correct for the
spawn, but indistinguishable from success unless it is said out loud. A gateway
log line explains it to whoever reads the log, which is not the person who
answered the trust prompt.

So the resolver returns the outcome as well as the decision. `resolve_with_locus`
records an `ExecutionLocus` per work dir, and the session payload reports it as
`execution`:

| Field | Meaning |
|---|---|
| `mode` | `container` when the agent really runs inside the project's container; `host` when a config exists and was not used |
| `reason` | Why the fallback happened: `untrusted`, `build_failed`, `docker_unavailable`, `config_changed`, `unsupported_platform`. Null for `container` |
| `container_name` | The container the session is inside, when known |

A work dir with **no** devcontainer config reports no `execution` at all — there
is no second world to have landed in, and claiming one would invent a
distinction the project does not have.

**The session payload currently withholds this verdict.** It is recorded per work
directory, but several sessions can share a project: a session that fell back to
the host, followed by a later session on the same project that did enter a
container, would read the newer verdict and display "in container". That is the
precise false reassurance the indicator exists to prevent — over-warning would be
tolerable, under-warning is not — so nothing is reported until the verdict is
keyed by the identity of the process that resolved it. The recording side stays
in place and tested, and the UI already renders nothing for an absent value.

The dashboard reads the recorded verdict rather than resolving again: a second
resolve would probe Docker on a UI request and could report a different world
than the session is really in. The `reason` tokens are therefore a published
vocabulary — the frontend maps them to plain language and degrades an unknown
token to generic wording rather than showing a raw identifier.

## Known v1 limitations

- Managed MCP runs on the host and is bridged into the container: unix
  sockets on native Linux, TCP to `host.docker.internal` on Docker Desktop.
  The host listener binds `127.0.0.1` only. The image must have `python3`.
  Foreign MCP servers declared in the project still start inside the
  container as usual. The dashboard is not opened on the LAN.
- `/proc`-based liveness observes the host-side `docker exec` client proxy.
  Death detection still works, because the pipe closes; the wedge heuristics
  degrade.
- Session teardown reaches the exec and every descendant it can still see, but a
  descendant that deliberately double-forks to orphan itself onto container PID 1
  *and* drops its environment marker leaves no local link to follow, and survives
  teardown inside the container until the container itself stops. Teardown finds
  the exec by an environment marker a process cannot rewrite for itself, then
  expands that set over `/proc` parent links — which is what catches the two easy
  evasions, a child started with a scrubbed environment and a child moved out of
  the process group by `setsid`. Closing the orphan case needs a boundary the child
  cannot leave, i.e. a cgroup or a PID namespace per exec, and `docker exec` can
  create neither without the privileged access this feature refuses. Sweeping every
  process in the container would reach it, but a container can serve more than one
  session and killing another session's agent is a worse failure than a survivor.
- The host Seatbelt / Job-object path is skipped on a containerized spawn.
- **One runtime hosts one container.** A kiro-cli runtime can host several ACP
  sessions (session sharing) but is containerized for exactly one project, so a
  session whose cwd is not that runtime's working directory is refused and must
  cold-start its own runtime. In normal operation this does not fire: a
  project-scoped session cannot claim a pooled runtime, so it already gets a
  runtime whose working directory is its own project.
- Warm-pool runtimes follow the same rule as any other. They are pre-spawned
  with the default workspace directory as their working directory, before any
  project is known. A session whose work dir has a trusted config does not
  claim a host warm-pool runtime. Default `session.pool_size` is `0`.

## Example `devcontainer.json`

A Python + Node image on bookworm (glibc 2.36) that installs `kiro-cli` in a
lifecycle hook. Note there is no `features` block: a Feature's metadata is fetched
from a registry and merged at build time, so it can grant privileges the trusted
text never showed, and this preview refuses it. Put the toolchain in the image or
install it in a lifecycle hook, as below.

```jsonc
{
  "name": "my-project",
  // A base image that already carries the toolchains, since a `features` block
  // that would otherwise install Node is refused in this preview.
  "image": "mcr.microsoft.com/devcontainers/python:3.12-bookworm",
  // Runs once, after the container is created. Put the install here rather than
  // postStartCommand so it is not repeated on every reuse.
  "postCreateCommand": "bash .devcontainer/install-kiro-cli.sh",
  "remoteUser": "vscode",
  "containerEnv": {
    "PATH": "${containerEnv:HOME}/.local/bin:${containerEnv:PATH}"
  }
}
```

`.devcontainer/install-kiro-cli.sh`, using the installer command from the
[Kiro CLI docs](https://kiro.dev/docs/cli/):

```bash
#!/usr/bin/env bash
set -euo pipefail

# Install kiro-cli into a PATH directory the remoteUser owns. Substitute the
# installer invocation published in the Kiro CLI docs for your platform.
mkdir -p "$HOME/.local/bin"
# <installer command from https://kiro.dev/docs/cli/>

kiro-cli --version   # fail the build now rather than at first session spawn
```

Baking `kiro-cli` into a prebuilt image is preferable for a team:
`postCreateCommand` runs on every fresh container and adds that time to the
first session start after a rebuild. A `features` block cannot be the install
path in this preview.

After adding or editing the config, review and trust it before the next session
spawns — an edit invalidates any earlier grant.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Session runs on the host with no error | One of the five `auto` preconditions failed. Check the gateway log for the untrusted-config, docker-missing, or `devcontainer up failed` warning. |
| `devcontainer CLI not found` | Neither `devcontainer` nor `npx` is on the gateway's `PATH`. Install with `npm i -g @devcontainers/cli`. |
| `kiro-cli not found` inside the container | The image or its lifecycle hooks do not provide `kiro-cli` on the container's `PATH`, or it is installed somewhere `remoteUser`'s `PATH` does not cover. |
| `kiro-cli` starts but is not logged in | Confirm `KIRO_API_KEY` is set for the gateway, or that the host `data.sqlite3` exists and was copied (gateway log / `/tmp/kirocrew-auth`). A login inside the container does not write back. |
| `--agent` fails after trust | Add `.kiro/agents/<name>.json` to the project, or install that one file under `~/.kiro/agents` on the host so it can be copied in. The whole agents directory is never mounted. |
| Trust prompt returns after a `git pull` | Expected. The pull changed the config bytes and therefore the digest. |
| Managed MCP (cron / subagents / lessons) fails to start | The image has no `python3`, or the host bridge failed to listen. On Docker Desktop the client must reach `host.docker.internal`. Check the gateway log for `devcontainer mcp-bridge`. Foreign project MCP servers still start inside the container. |
| Docker Desktop session still runs on the host | Docker must be on the gateway `PATH`. `unsupported_platform` is only for a host with no usable Docker; a missing binary is `docker_unavailable`. |
| `devcontainer up timed out` | The build exceeded 15 minutes. Prebuild the image, or move heavy work out of `postCreateCommand`. |

## Related

- [Config schema](system-specs/modules/config.md) — where `agent.devcontainer` lives.
- [Module spec](system-specs/modules/devcontainers.md) — the technical contract.
- [Near-production opt-in plan](design/devcontainer-product-readiness.md) — Docker Desktop, dual MCP transport, auth copy, one-file agent inject, and what is still not a prod default.
- [ACP client](system-specs/modules/acp-client.md) — the spawn path this hooks into.
- [Security](system-specs/modules/security.md) — the host sandbox a containerized session is mutually exclusive with. This feature is not a substitute for that sandbox.
