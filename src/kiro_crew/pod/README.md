# `kirocrew pod` — isolated worktree test instances

Spin up a **throwaway, full-stack KiroCrew gateway** for any feature worktree —
its own port, its own `KIROCREW_HOME` (own DB / sessions / memory), no Slack
tunnel, `--no-crons` (unless you pass `--crons`), resource-capped, and reclaimed
by `pod down`. Test a branch's
backend `/api/*` **and** the SPA bundle it serves, all **without touching your
live gateway or your shared `~/.kiro/crew` data**. `--no-embeddings` additionally
boots it without the embedding model, for load tests that must not pay per-chunk
embed compute.

Think **`kubectl` for local worktree test rigs.** This is the *test line*
(multi-active, burn-on-evict); it is orthogonal to the *live line* (a single
gateway serving real data on the canonical port) and refuses to bind the live port.

Pod separation is **operational and state isolation**, not an adversarial security
boundary against arbitrary processes already running as the same Unix UID. Such a
process can modify user-owned pod storage directly; descriptor pinning prevents
path/symlink substitution from accidentally redirecting an approved operation, but
it does not add per-pod UIDs or mount isolation. Controller v1 runs pod operations
host-side (`main/live Kiro Crew -> target test pod`); a pod does not create or control
a child pod.

## Interface

```bash
kirocrew pod install              # lay down the systemd --user template unit (Linux only; a no-op elsewhere)
kirocrew pod provision <wt>       # build the worktree's venv + SPA dist (the on-ramp)
kirocrew pod up   <wt> [--json]   # bring up an isolated pod → {base_url, token, port}
kirocrew pod up   <wt> --provision# provision (if needed) then bring it up
kirocrew pod up   <wt> --approval reads  # boot its gateway in an approval mode
kirocrew pod up   <wt> --crons          # boot its gateway with the cron scheduler on
kirocrew pod up   <wt> --no-embeddings  # boot without the embedding model (keyword-search fallback)
kirocrew pod up   <wt> --seed minimal  # pre-populate its HOME from a named scenario
kirocrew pod scenarios [--json]        # list named scenarios and their descriptions
kirocrew pod api  <wt> GET sessions    # authenticated request → fixed-key JSON
kirocrew pod ls                   # what's running (≈ kubectl get pods) + orphaned HOMEs (with age)
kirocrew pod prune [--all] [--dry-run]  # bulk-reclaim orphaned HOMEs (default: older than 3d; --all for every age)
kirocrew pod status <wt>          # up/down + health
kirocrew pod token  <wt> [--ttl]  # (re)mint a dashboard token for a running pod
kirocrew pod url    <wt>          # print its base_url
kirocrew pod logs   <wt> [-n N]   # tail its journal
kirocrew pod down   <wt>          # evict → delete its HOME, verified (zero residue)
```

`<wt>` is a friendly worktree name. It is resolved to a checkout **git-natively**:
`kirocrew pod up <name>` matches a linked worktree by its directory basename, its
branch (`<name>` or `feat/<name>`), or an exact path — run it from inside any
KiroCrew checkout (or set `KIROCREW_POD_REPO`). The resolved path is pinned so the
pod's gateway boots without re-consulting git.

## The on-ramp (provisioning)

A worktree must be *built* before it can be podded — an editable
`.venv/bin/kirocrew` and a built SPA bundle (`src/kiro_crew/static/dist`). These
are intrinsic to "a worktree that can run a gateway at all"; pod just surfaces
and collapses them, honoring their very different costs:

| Prereq | Cost | Who builds it |
|---|---|---|
| **venv** | ~1 min, idempotent | `pod up` **auto-builds** it on demand |
| **dist** | minutes (Vite SPA build) | only on **explicit consent** |

So plain `pod up <wt>` builds the cheap venv for you but **fails loud** if the
dist is missing — pointing you at the slow build — while `pod up <wt> --provision`
(or `pod provision <wt>`) runs the full chain: venv + `npm run build` in
`website/` staged into the served `static/dist`.

## Seed the isolated home

```bash
kirocrew pod scenarios
kirocrew pod scenarios --json
kirocrew pod up my-wt --seed minimal
kirocrew pod up my-wt --seed ~/.kiro/crew
```

`pod scenarios` reads the packaged fixture registry and lists names in sorted
order. The default human-readable table shortens each description to the last
complete sentence that fits, falling back to a cut between words with an
ellipsis. `--json`
emits a stable array of `{name, description}` objects containing the complete
description scalar. Literal (`|`) blocks preserve newlines and folded (`>`) blocks
normalize to one paragraph. Extraction uses the fixture manifest's narrow scalar
format and does not require PyYAML at runtime.

A bare name selects a fixture shipped under `kiro_crew/tests_fixtures/<name>/`
and populates the whole isolated home. Anything with a path separator or a
leading `~` or `.` stays the directory form, which contributes only a sanitized
`config.json`. The split is syntactic, so an unknown bare name is refused with
the available names instead of being mistaken for a directory and booting a
blank pod. Spell a bare relative directory as a path, for example
`--seed ./my-state`.

Named fixtures are copied directly into the final home with both fixture and
home traversals pinned by directory descriptors. Config sanitization and
workspace setup run through the same held home descriptor, then the fixture
manifest is copied last as the completion marker. A failed partial copy or
setup therefore stays non-bootable even on systemd's automatic retry, and a
symlink or path-name substitution during the operation cannot redirect writes.
This does not confine an already-open inode against arbitrary same-UID host
processes; that limit is part of the operational-isolation boundary above.
Seeded config forces tunnel/channel enablement off and restores the agent
sandbox floor. A populated home is never overwritten or re-seeded: a
`pod up --seed` request against one refuses before start even when its marker
already matches. Use plain `pod up` to restart that home unchanged. Service
restarts keep the sessions and logs already present. After health succeeds,
`pod up` reads the fixture marker back and fails if the requested scenario did
not land.

## Boot without the embedding model

```bash
kirocrew pod up my-wt --no-embeddings
```

Records `EMBEDDINGS='0'` in the pod's env file, which makes `boot` export
`KIROCREW_SKIP_MODEL_DOWNLOAD=1` into **the pod's env only**. The pod never
downloads the ~610MB GGUF and never computes a vector; memory and knowledge
search answer through the keyword fallback, which is a supported mode rather than
a broken one, so the instance stays usable. Your own home is untouched and keeps
whatever model it already has.

This exists for **load-testing ingestion**. Knowledge ingest embeds per chunk, so
a bundle large enough to exercise a cross-file cost ceiling spends nearly all of
its wall clock inside embedding compute — enough that driving one has hit a
30-minute worker wall. Without the model, chunk count grows while embed compute
does not, so the same bundle is drivable in minutes and any remaining slowness is
attributable to something else.

The setting travels with the pod, so `pod exec` against an embedding-light pod
also sees it and cannot quietly start the download the pod was booted to do
without. It is read once at boot: recording it against a running pod applies on
the next one, and `pod up` says so. It lives as `EMBEDDINGS='0'` in the per-pod
env file — the same hand-editable file that pins `CHECKOUT=` and `PORT=` — so a
pod you keep around for other work can be flipped persistently by editing that
line and bringing the pod up again.

The boot journal names the mode from the env the pod actually runs with, so a
`KIROCREW_SKIP_MODEL_DOWNLOAD=1` the pod merely inherits is announced too (as
"inherited from the boot environment"), and the `pod.up` audit row keys
`embeddings=off` on the merged env file plus that inherited switch rather than on
the flag alone. Any `KIROCREW_EMBED_MODEL_PATH` / `KIROCREW_EMBED_MODEL_URL` in
the inherited environment is dropped from the pod env alongside the switch: the
switch only gates the download, and a custom model path would otherwise load and
embed anyway while the journal says the pod does not.

The switch behind the flag, `KIROCREW_SKIP_MODEL_DOWNLOAD`, is deliberately
subsystem-wide: an embedding-light pod also skips its speech-to-text (whisper)
model download, so do not test or measure voice input in one.

`--no-embeddings` is the only supported spelling. Pointing
`KIROCREW_EMBED_MODEL_URL` at an unreachable mirror looks equivalent and is not:
it spends the downloader's whole attempt budget on requests chosen to fail, and a
value that is not `https://` is ignored in favour of the real CDN — so a typo
downloads the model you were avoiding.

## Call the pod API without handling its token

```bash
kirocrew pod api my-wt GET sessions
kirocrew pod api my-wt GET '/api/sessions?limit=20'
kirocrew pod api my-wt POST config --data '{"key":"agent.model"}' --allow-write
```

`pod api` makes one request and prints one JSON document with fixed keys:
`{name, method, path, status, ok, body}`. JSON response bodies are decoded into
`body`; other bodies remain text. A non-2xx response prints the same shape and
exits 1. Response reads are capped at 32 MiB and an oversized or truncated body
fails without buffering indefinitely.

GET and HEAD are allowed by default. POST, PUT, PATCH, and DELETE require
`--allow-write`; v1 deliberately has no route-by-route side-effect catalog, so a
safe-method route that mutates state is a server contract defect to fix at that
route. Caller-supplied `token` query parameters are refused without displaying
their value. The command mints its own dashboard token and sends it using the
same `?token=` query contract as the dashboard middleware, never an
`Authorization` header.

The authenticated request travels over the pod's **private dashboard unix
socket** — `<pod home>/dashboard-<port>.sock`, the same file name the gateway
binds, resolved against the pod's isolated home rather than the host's — and
**never over TCP, with no fallback**. The port is only the `Host` the gateway
sees. A pod's port is ordinary loopback: any local user can bind it the moment
the pod releases it, so a pod that exits between the mint and the send would
otherwise hand a token that is valid as an `mc_token_<port>` cookie to whatever
answered next, replayable against the restarted pod. The socket cannot be
answered by another user, because it sits in a home created owner-only and is
itself `chmod 0600`.

A missing socket therefore **refuses through the envelope** (`status: 0`,
`ok: false`, remediation in `body`) instead of retrying on `127.0.0.1:<port>`,
and it refuses *before* minting, so an undeliverable request never pays for a
credential. The refusal is expected while a pod is starting, after it crashed
without a `down`, and on a checkout whose gateway predates the socket; a
`pod down` plus `pod up` clears all three. Requiring the socket costs no
capability on Linux, where the gateway binds it unconditionally. On **Windows** it
costs the verb: CPython there has no `AF_UNIX`, so `pod api` always refuses
through the envelope. That is deliberate rather than a gap to close with a TCP
fallback — the fallback is exactly what would hand a token to whatever answered
the pod's released port. Use `pod token` plus your own client against the
loopback port when you need an authenticated request on that platform.

Before minting, the control plane reads the gateway PID sidecar from the pod's
isolated home and requires it to equal the service manager's current MainPID.
That agreement is the primary ownership attestation and works on minimal hosts
without `lsof` or `netstat`. Listener attribution is additional corroboration
when available; it is never sufficient by itself. Tokens are scrubbed from
response text and transport failures never include the authenticated URL.

## A pod IS the worktree's gateway (control plane vs payload)

- **Control plane** — the `kirocrew pod` verbs (resolution, port derivation, unit
  management, token mint, boot *prep*). These run from the **stable, globally
  installed** `kirocrew`, so they never break just because a worktree's code is broken.
- **Payload** — the booted pod *is* the worktree's `.venv/bin/kirocrew gateway`. If
  the worktree's gateway can't start (bad import, broken config, unbuilt dist), the
  pod can't come up — **and that is correct**. `pod up` detects the crash fast,
  prints the gateway's own journal, stops the half-started unit, and tells you this
  is the worktree build failing — not the pod tool.

## Mechanism (Linux `systemd --user`)

`kirocrew pod install` writes a template unit `kirocrew-pod@.service` whose
`ExecStart` re-enters `kirocrew pod _run <wt>` (boot logic lives in
`kiro_crew.pod.runtime.boot`). Before each start, `pod up` writes a per-instance
drop-in that replaces the template's `ExecStart` with the resolved checkout's
own `.venv/bin/kirocrew`; it refuses to fall back to a global install that may
not understand the requested seed. `pod down` removes that drop-in and reloads
systemd as part of its zero-residue guarantee. `MemoryMax`/`CPUQuota` cap a
runaway pod; `Restart=on-failure` self-heals.

The unit has **no `ExecStopPost` teardown hook**, on purpose. systemd runs
`ExecStopPost` *before* the final kill of the unit's cgroup, so a hook that
deleted the pod's HOME raced the pod's own surviving subprocesses — they
recreated the directory by reopening their audit log in append mode — and it also
fired on the stop half of a `Restart=`, bringing the pod back on a home stripped
of its sessions and config. So `kirocrew pod down` owns reclamation on every
platform: it stops the service, waits for the unit's cgroup to drain, deletes the
HOME through `runtime.cleanup_home` (which re-validates the name and refuses
`..`/absolute/empty, since teardown safety must not rely on systemd `%i`
semantics), then VERIFIES the directory is gone and fails loudly if it is not.
The trade is that a pod which goes away without a `down` — a crash, a raw
`systemctl --user stop`, a reboot — leaves its HOME behind; `pod ls` reports
those with their age, `pod down <wt>` reclaims one, and `pod prune` reclaims
them in bulk — by default only HOMEs whose last activity is older than 3 days
(`--all` sweeps every age; each delete still routes through the same
stop-drain-verify path `down` uses, with liveness re-checked per name).

### Port derivation and allocation

`port = base + (cksum(name) % 199) + 1` (base `7810` → `7811..8009`), unless a
`PORT=` is pinned in `~/.kiro/crew/pods/<name>.env`. `pod up` refuses if a derived
port ever resolves to the live port.

Derivation answers "which port does this name PREFER", and it is a **default hint,
not a contract**. Every reader (`pod url`, `pod ls`, `pod exec`, Dev Fleet) calls it
to agree without coordinating, but 199 slots means two names colliding is ordinary,
and the derived port can equally be held by something that is not a pod. It does NOT
check that the port is free.

Whether the port can be had is asked once, by `pod up`, and the answer is **recorded
as a `PORT=` claim on every allocation** — so after a pod's first `up` its port comes
from that claim rather than from the formula. The formula still picks the
first-preference port for any pod that has never come up, which is why the
degradation is graceful: derivation chooses, ownership is explicit, and readers
follow the claim.

- The pod is already running → nothing is allocated, and the port is re-resolved
  under the lock. Its port is busy because it owns it, and moving a live pod would
  strand every reader.
- A hand-pinned `PORT=` that is busy → refused loudly. A deliberate pin is never
  relocated automatically. A pin outside 1–65535 is refused by name.
- Otherwise the first port that is free, not the live plane, and **not claimed by
  another pod** is taken — walking from just above the preferred slot and wrapping,
  deterministically. It is recorded as `PORT=` plus `PORT_AUTO` (which marks the
  claim as machine-made, so it stays relocatable) and a move is reported on stderr.
- Nothing available → refused loudly, naming the band and the `PORT=` escape hatch.
  A pod that cannot get a port must not appear to start.

Reading other pods' recorded claims is what makes concurrent boots safe: a unit is
`Type=simple`, so `start_pod` returns *before* the gateway binds, and until then a
bind probe reports that port free. Claiming is serialized plane-wide (`pod up` holds
a plane lock across choose → start), and the claim is written before the start, so a
colliding name sees it immediately rather than after the bind.

**A collision that still happens is detected, not mistaken for health.** Allocation
prevents the ordinary cases above, but it cannot prevent all of them: two colliding
names started concurrently can still race inside the window between the service
manager accepting the start and the gateway binding. When that happens whoever binds
first wins and the loser's gateway exits "address already in use", its unit
crash-looping behind `Restart=on-failure`. So reachability on a port is never
evidence that THIS pod is up: `health` and the credential mint both go through
`port_owner`, and `pod status` / `pod ls` print `foreign (port held by another
instance)` when the port belongs to somebody else.

What `port_owner` proves is that the pod's gateway PID sidecar — written into the
isolated home only *after* the bind succeeds — agrees with the service manager's
current `MainPID`. Beside that sidecar the gateway writes its start-time identity in
its own `gateway-<port>.start` file (the pid file itself stays a bare pid, so every
shipped reader keeps parsing it), so a record left behind by a crash cannot attest
once that pid has been recycled onto an unrelated process; a record that cannot prove
its own freshness is refused, and the mint withholds the secret. A record carrying NO
start identity is refused the same way, but it is a different fault with a different
fix: a pod's gateway is its worktree's own venv binary, so a checkout that predates
the sidecar writes no identity and a restart adds none — the mint says so and points
at re-provisioning the worktree rather than at a restart. Listener attribution
(`lsof`) is corroboration on top: a *different* pid holding the `127.0.0.1`
listener is positive proof of a foreign responder and overrides the record, but an
absent, failed, or unattributable lookup is not evidence of anything and leaves the
record's verdict standing. That last case is the norm, not an edge: a minimal Linux
host may ship no `lsof` at all, and an unprivileged caller — which is how `pod api`
runs — cannot see a socket held by a gateway the user's service manager started. `pod up` names the conflict and points at `PORT=` rather than blaming the
worktree build, and pinning a colliding pod's own `PORT=` remains the manual way out.

## Configuration (`PodConfig`, all `KIROCREW_POD_*`-overridable)

| env | default | meaning |
|---|---|---|
| `KIROCREW_POD_REPO` | invoking cwd | repo git is queried from to resolve worktree names |
| `KIROCREW_POD_WORKTREES_ROOT` | (unset) | optional `name→path` fallback root (hermetic planes) |
| `KIROCREW_POD_ROOT` | `~/.kirocrew-pods` | isolated pod HOMEs (reclaimed by `pod down`) |
| `KIROCREW_POD_ENV_DIR` | `~/.kiro/crew/pods` | per-pod `CHECKOUT=`/`PORT=`/`SEED=` files |
| `KIROCREW_POD_BASE_PORT` | `7810` | port derivation base |
| `KIROCREW_POD_LIVE_PORT` | `5476` | the port a pod must never bind |
| `KIROCREW_POD_UNIT_PREFIX` | `kirocrew-pod` | systemd unit prefix |
| `KIROCREW_POD_BIN` | (auto) | the `kirocrew` binary the unit boots |
| `KIROCREW_POD_KIRO_BIN` | (unset) | agent backend pinned into the service definition as `KIROCREW_KIRO_BIN` |

Overriding the prefix + roots + base port yields a fully **hermetic pod plane**
that can't collide with a developer's live pods — used by the test suite.

`KIROCREW_POD_KIRO_BIN` is the offline seam. A booted pod starts from the service
manager's clean environment, so nothing the caller exports reaches its gateway —
including `KIROCREW_KIRO_BIN`, the pin `kiro_cli.py` reads to override the agent
binary. Without a plane-level knob there was no way to give a pod the packaged
fake ACP backend, so an agent turn inside a pod needed a real signed-in
`kiro-cli` and could not run on an offline CI runner. Set it to
`kiro_crew.testing.fake_acp_backend`'s file and both backends pin it: the systemd
unit gets an `Environment=` line, the launchd plist an `EnvironmentVariables`
entry, from the one `environment_vars` selection. Unset, nothing is emitted and a
pod resolves the host's real `kiro-cli` exactly as before.

## Safety

- A pod runs its own `KIROCREW_HOME` and binds `127.0.0.1` only; it never touches
  the shared `~/.kiro/crew` data and refuses the live port.
- Every pod's `config.json` forces `enabled=false` on the tunnel and on every
  channel that carries a config-level enable (`runtime.SEED_DISABLED_SECTIONS`),
  and the booted env scrubs `SLACK_*`, `WECOM_*`, `MICROSOFT_APP_*` and non-AWS
  `*_TOKEN`, so a pod can never grab a live messaging identity — not even a
  seeded one, which is the point: `--seed ~/.kiro/crew` clones the real config.
  Pod HOME is `0700`; `config.json` is `0600`.

## Platform

Three backends, one platform-neutral core. Name validation, port derivation and
allocation, checkout resolution and pinning, env scrubbing, seeding, token
minting, `boot` and the `cleanup_home` teardown check are shared; only the
service-manager mechanics differ.

| | Linux | macOS | Windows |
|---|---|---|---|
| Manager | `systemd --user` | `launchd` | Task Scheduler (`schtasks.exe`) |
| Definition | one template unit + per-pod drop-in | one plist per pod | one task per pod + a generated `.cmd` wrapper |
| Elevation | none | none | none |
| Logs | `journalctl --user` | files | files |
| Restart on crash | `Restart=on-failure` | `KeepAlive` | **none** |
| Memory / CPU ceiling | `MemoryMax` + `CPUQuota` (cgroup) | **not enforced** | memory + process cap (Job object); **no CPU cap** |
| `pod api` / `pod token` | yes | yes | `token` yes, `api` **no** (needs an AF_UNIX dashboard socket) |

On a host with none of the three — a Linux box with no systemd on PATH, or any
other platform — the verbs that touch a service manager **refuse with a single
actionable line** and exit 1. They never raise a traceback, and `pod install`
writes **no** definition when the host cannot load it.

The gate is `runtime.require_backend()`, which dispatches to
`launchd.require_backend()` on darwin, `windows.require_backend()` on win32, and
`require_systemd()` everywhere else. `pod url` is pure port arithmetic and works
anywhere; `pod up` / `provision` fail earlier on their own preconditions
(worktree resolution, venv/dist) before reaching the service manager.

### Windows (Task Scheduler)

A pod is per-user, disposable, and must never need administrator rights.
`sc.exe create` needs `SeCreateServiceNamePrivilege` and installs a machine-wide
LocalSystem service, so it fails that on both counts. `schtasks.exe` creates a
task in the calling user's own namespace with no elevation, which is the same
shape as `systemd --user` and launchd's `gui/<uid>`. So the Windows backend is
Task Scheduler, and `kiro_crew.pod.windows` states the five consequences:

- **No task-level env vars.** A task carries one command line and the user's
  profile environment, so the pod plane the CLI resolved would be lost. The
  task's action is therefore a generated `.cmd` under `KIROCREW_POD_ENV_DIR`
  that sets the plane from the same `config.environment_vars` selection the other
  two backends serialise, then re-enters `kirocrew pod _run <name>`. Boot logic
  stays in Python; the wrapper is data. A pod-plane path containing a double
  quote or a newline is refused at `pod up`, because cmd.exe cannot express it.
- **No restart policy.** A crashed pod stays down, which removes launchd's
  restart-loop hazard entirely (a terminal refusal keeps its honest exit code —
  no exit-0 translation) but means the crash signal has to be recorded: the
  wrapper writes the boot's exit code to `<prefix>.<name>.winresult`, and
  `unit_state` reports `failed` when that code is non-zero and the supervised
  process is gone.
- **No PID from the service manager, and no `exec`.** `schtasks /Query` reports
  no pid at any verbosity, and CPython's `os.execve` on Windows spawns a new
  process and terminates the caller — which would change the pid and orphan the
  gateway while Task Scheduler reported the task finished. So `boot` supervises
  the gateway as the wrapper's child, records its pid plus its creation-time
  identity in `<prefix>.<name>.winpid`, and waits. That restores the invariant
  `port_owner` rests on: the recorded pid IS the process that bound the port.
  It stays an independent fact from the gateway's own PID sidecar (different
  file, different directory, different writer).
- **A restart HANDOFF is a marker naming its writer, and `pod down` waits it out
  rather than refusing on sight.** `supervised_pid` fails closed on a dead pid, so
  between reaping a gateway and recording its successor the pod reads STOPPED while
  a successor may be booting into its HOME. The supervisor covers that window with
  `<prefix>.<name>.handoff`, published as the first statement after the reap — which
  means it is published on EVERY reap, *including the one `pod down` itself causes*.
  Its presence therefore says only "a supervisor has not yet decided whether a
  successor exists", so teardown waits, bounded, for that decision: a retracted
  marker means the supervisor decided and the task is deleted; a pid recorded again
  means a successor is serving and the stop reports THAT; only a still-undecided
  marker at the bound refuses. The marker carries its publisher's pid and
  creation-time token, so a supervisor that `/End` reaped before it could retract is
  recognised as gone rather than waited out — reading such an orphan as a live
  handoff made `pod down` refuse pods it had already stopped and leave their
  scheduled tasks registered. Freshness remains the outer bound, for a supervisor
  that is alive but wedged; a stale marker is treated as "publisher presumed dead"
  and traded for bounded teardown, which is the pre-existing trade this backend
  documents rather than a new one.
- **`schtasks` output is localized, so this backend never parses it.** Both the
  CSV headers and the `Status` values are translated on a non-English Windows, so
  a reader keyed on `Status == "Running"` would report every pod down on a German
  host — the fail-open direction, where teardown deletes a live pod's HOME.
  Liveness, the pid and the last result come from the two files above.
  `schtasks` is used only where the **exit code** is the answer: `/Create`,
  `/Run`, `/End`, `/Delete`, and `/Query` as an existence probe.
- **No cgroups, so the ceiling is a Job object — and it IS enforced when it can be attached, and LOUD when it cannot.**  `supervise_gateway` creates the gateway `CREATE_SUSPENDED`, attaches a Job
  object through `sandbox.apply_windows_resource_ceiling` (the same seam and the
  same `resource_limits` config the agent-subprocess path uses), then resumes it.
  The suspended handshake is what makes it airtight: job membership covers a
  member's future descendants but not ones it already spawned, and a suspended
  child has executed no instructions. Two honest gaps: the process row is looser
  than the cgroup row (`ActiveProcessLimit` counts processes, `TasksMax` counts
  threads), and there is no CPU row, so this is a fork-bomb and memory ceiling
  rather than parity with `MemoryMax` plus `CPUQuota`. A ceiling that cannot be
  installed logs a SECURITY warning and does not fail the boot, matching how an
  unavailable cgroup scope is handled. macOS still has no ceiling at all.
- **No `dir_fd`, so `--seed` is witnessed rather than descriptor-pinned.** The
  Linux and macOS seed copies every fixture entry through a held pod-home
  descriptor, which Windows cannot do at all: `os.open` and `os.mkdir` accept no
  `dir_fd` there, so `pinned_fs.supports_pinned_tree_walk()` is False and a
  destination cannot be addressed relative to a descriptor. `_seed_home_windows`
  is the branch that runs instead, and its guarantee is narrower and stated:
  every component of the home and its ancestors is screened with
  `pinned_fs.is_reparse_point` (which catches a junction, where `os.path.islink`
  does not) before anything is written, the home is created by that call, and a
  handle plus a `pinned_fs.fd_real_path` witness — `GetFinalPathNameByHandleW`,
  the kernel's own name for the inode already open — is taken on it. Each fixture
  file is copied from a pinned source descriptor (`copy_file_pinned`'s `src_fd`
  form, the only pinned source form on this platform) into an
  `O_CREAT | O_EXCL` destination under that witnessed home, so the branch can
  only add entries it created and can never overwrite one. The witness is re-read
  before the completion manifest is published, so a home that changed identity
  mid-seed is refused rather than booted, and the manifest stays the last write.
  The home handle comes from `platform_compat.pin_directory`, which opens it
  through `CreateFileW` without `FILE_SHARE_DELETE`: while the seed runs, the
  home and every directory above it can be neither renamed nor deleted, so an
  ancestor swap is refused by the kernel rather than detected afterwards.
  **Residual, stated rather than claimed closed:** the home's CONTENTS are still
  reached by name under that pinned handle, so a process running as this same
  user could plant a reparse point at a not-yet-written child name; `O_EXCL`
  refuses a planted leaf and the subdirectory screen covers the two directories
  the seed creates, but Windows offers no `dir_fd` to close the rest of that
  window. A pod home lives under a plane root only this user can write, so that
  residual is the same trust domain the OS already grants that user, and the
  same operational-isolation boundary this document records above. Every OTHER
  host without a pinned tree walk keeps the outright refusal; the relaxation is
  win32-only by construction.

`require_backend()` on Windows has three stages: this is win32, `schtasks.exe`
resolves through `platform_compat.trusted_system_bin`, and **the current user can
really create a task**. The third is a create-and-delete probe of a throwaway
task rather than an inspection, because there is nothing to inspect — Group
Policy, a disabled `Schedule` service and a principal without `TASK_CREATE` all
refuse invisibly from the client side, and without the probe each surfaces as a
failed `pod up` blaming the worktree build. The probe result is cached per
process, since the gate sits on the chokepoint every `schtasks` call funnels
through.

Teardown is `stop`'s job here as on the other two. There is no cgroup to drain,
so `windows.stop` proves the pod gone by watching the **supervised pid** die
(`/End` is asynchronous and reaches only the task's own process), escalates to
`platform_compat.kill_process_tree_pinned` if it will not, and refuses to delete
the task or let the HOME be reclaimed while that pid is still alive.

`pod api` does not work on Windows, and that is a fail-closed refusal rather than
a gap in this backend: the authenticated request travels over the pod's private
AF_UNIX dashboard socket with no TCP fallback (see above for why), and CPython on
Windows has no `AF_UNIX`. A missing socket refuses through the envelope
(`status: 0`, `ok: false`) before minting, so no credential is ever paid for.
`pod token`, `up`, `down`, `ls`, `status`, `url`, `logs`, `prune`, `provision`
and `scenarios` all work.

The per-pod `.cmd` wrapper is written in the console's OEM code page, because
that is what `cmd.exe` reads a batch file with (never UTF-8), and the encode is
strict: a plane path with a character that page cannot represent is refused at
`pod up` with the offending text named, rather than handed to `cmd.exe` as
different bytes. Keep `KIROCREW_HOME` and the `KIROCREW_POD_*` roots on paths
the console code page can spell.

The backend is exercised on a real windows-latest runner by the CI job **Pod
Boot Canary (Windows)** (`test/test_pod_windows_boot.py`), which proves task
creation is permitted for the runner's user, round-trips a trivial task through
create, run and delete, then registers a real task on a per-run plane, boots the
pod, mints `pod token` against it, and asserts `pod down` leaves no task, home
or env file behind. The module is opt-in: it skips unless
`KIROCREW_E2E_POD_WINDOWS=1` is set, because a bare `pytest` on a developer's
Windows box must not register scheduled tasks. It is also one of the two entries
on the root `conftest.py` host-service allowlist, whose guard otherwise refuses
any test that spawns `kirocrew pod up`, `down`, `install`, `prune` or `restart`
as a child process, or `schtasks` with a `/Create`, `/Delete`, `/Run`, `/End` or
`/Change` switch.

### Session bus (Linux only)

`systemctl --user` locates the per-user systemd instance through
`XDG_RUNTIME_DIR` + `DBUS_SESSION_BUS_ADDRESS`. A process descended from a
systemd **system** unit — which is how `kirocrew service install` runs the
gateway — inherits no login-session environment and therefore neither variable,
so pods used to fail with a bare `Failed to connect to bus: No medium found`.

`runtime._systemctl_env()` backfills both when the socket
(`$XDG_RUNTIME_DIR/bus`, else `/run/user/<uid>/bus`) actually exists; an
explicitly-set value always wins. When the socket is genuinely absent — no login
session and `Linger=no` — `require_systemd()` refuses with the fix
(`loginctl enable-linger <user>`) instead of letting systemctl emit a message
that names neither cause nor remedy. `kirocrew doctor` reports the same three
states (present / absent / present-but-no-linger).
